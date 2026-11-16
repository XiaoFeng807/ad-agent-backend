import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
import hashlib
from threading import Lock
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from backend.agent.request_budget import tool_call_fields
from backend.observability import add_trace_step, trace_tool_call
from backend.rag.rule_reference import parse_rule_reference


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
PlanId = Identifier | Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
DateString = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]


class DateWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_date: DateString
    end_date: DateString

    @model_validator(mode="after")
    def validate_range(self):
        days = (date.fromisoformat(self.end_date) - date.fromisoformat(self.start_date)).days
        if not 0 <= days <= 90:
            raise ValueError("结束日期不能早于开始日期，两者最多相差 90 天")
        return self


class DiagnosisWindows(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline: DateWindow
    current: DateWindow


class CreateDiagnosis(DiagnosisWindows):
    plan_id: PlanId
    rule_id: Identifier


class ContinueDiagnosis(DiagnosisWindows):
    task_id: Identifier


class NaturalDiagnosisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["natural"]
    task_id: Identifier | None = None
    source_turn_id: int | None = Field(default=None, strict=True, gt=0, le=2**63 - 1)

    @model_validator(mode="after")
    def paired_source(self):
        if type(self) is NaturalDiagnosisRequest and (self.task_id is None) != (self.source_turn_id is None):
            raise ValueError("task_id 和 source_turn_id 必须同时提供")
        return self


class NaturalDiagnosis(NaturalDiagnosisRequest):
    task_id: Identifier
    inherited_rule_id: Identifier | None = None
    comparison_target: Literal["largest_ctr_decline"] = "largest_ctr_decline"
    candidate_scope_definition: Literal["owned_active_plans"] = "owned_active_plans"
    inherited_fields: tuple[str, ...] = ()
    overridden_fields: tuple[str, ...] = ()


DIAGNOSIS_REQUEST = TypeAdapter(CreateDiagnosis | ContinueDiagnosis | NaturalDiagnosisRequest)
DIAGNOSIS_TOOLS = {"get_plan_detail", "get_daily_report_by_date", "get_plan_metrics_by_date", "search_knowledge"}
DIAGNOSIS_CALL_LIMIT = 12
CONTROL_FIELDS = {
    "query_data": {"query"},
    "analyze_data": {"query"},
    "query_knowledge_base": {"question"},
    "call_peer": {"peer_name", "question"},
}


class DiagnosisSnapshot(CreateDiagnosis):
    task_id: Identifier

    def prompt(self, role="orchestrator"):
        state = self.model_dump_json(exclude={"task_id"})
        common = (
            f"【本轮诊断参数】{state}\n"
            "本轮查询以这些参数为准，每轮重新取证。日期报表是用户汇总，"
            "不能作为单计划的日期指标，也不能据此推断该计划历史 CTR。"
            "按工具实际回执说明数据来源、证据和缺项。\n"
        )
        assignments = {
            "orchestrator": (
                "你是本轮诊断的总指挥。首次请求同时分派 query_data 和 query_knowledge_base，"
                "每个助手只分派一次。query_data 的一条 query 要合并计划详情及两个日期窗的全部查询，"
                "不能按单个工具或日期窗重复分派数据助手。query_knowledge_base 的 question 传 rule_id。"
                "数据助手负责三个数据查询，知识助手只负责规则检索；本轮无需 analyze_data。"
                "服务端负责接收实际回执、单项补查与缺项判定。"
            ),
            "数据助手": (
                "你是本轮数据取证助手。当前请求一次完成三个工具调用："
                "get_plan_detail 只传 plan_id；get_plan_metrics_by_date 分别传 plan_id 及 baseline 和 current 的日期字段。"
                "三个查询各执行一次，然后返回实际结果和数据口径。"
                "必须查询计划历史指标，按总点击/总曝光计算 CTR，分别给出百分点差和相对降幅。"
                "规则检索已由总指挥单独分派给知识助手，无需 call_peer 重复委派。"
            ),
            "知识助手": (
                "你是本轮规则取证助手。当前请求只需调用一次 search_knowledge，"
                "只传 query，值为原样的 rule_id，然后返回检索所得原文、引用标识和版本。"
                "计划及日期数据已由总指挥单独分派给数据助手，无需 call_peer 再查数据。"
                "完成检索即可交回结果，无需为生成优化建议补齐其他角色的数据。"
            ),
        }
        return common + assignments.get(role, "按本轮参数和已有回执说明证据缺项，避免重复取证。")

    def permits_tool(self, name, args):
        if not isinstance(args, dict):
            return False
        if name == "get_daily_report_by_date":
            return args in (self.baseline.model_dump(), self.current.model_dump())
        if name == "get_plan_metrics_by_date":
            return (type(args.get("plan_id")) in (str, int)
                    and str(args["plan_id"]) == str(self.plan_id)
                    and {k: v for k, v in args.items() if k != "plan_id"}
                    in (self.baseline.model_dump(), self.current.model_dump()))
        if name == "get_plan_detail":
            plan_id = args.get("plan_id")
            return args.keys() == {"plan_id"} and type(plan_id) in (str, int) and str(plan_id) == str(self.plan_id)
        if name == "search_knowledge":
            return args == {"query": self.rule_id}
        return False


def _unique_json_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate JSON key")
    return obj


def _invalid_json_constant(_value):
    raise ValueError("invalid JSON constant")


def diagnosis_tool_error(error):
    sqlite_code = getattr(error, "sqlite_errorcode", 0) or 0
    if isinstance(error, (TimeoutError, ConnectionError)) or (
        isinstance(error, sqlite3.OperationalError)
        and sqlite_code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    ):
        return "temporary_tool_failure"
    return "tool_exception"


@dataclass(frozen=True)
class DiagnosisReceipt:
    runtime_call_id: str
    tool_call_id: str | None
    parent_call_id: str | None
    dispatch_call_id: str | None
    tool: str
    arguments: dict
    scope: str
    status: str
    error_code: str | None
    citations: list
    result: dict | None
    retry_of: str | None = None

    def handoff(self):
        return {
            "runtime_call_id": self.runtime_call_id, "tool_call_id": self.tool_call_id,
            "parent_call_id": self.parent_call_id,
            "tool": self.tool, "arguments": self.arguments, "scope": self.scope,
            "status": self.status, "error_code": self.error_code, "retry_of": self.retry_of,
            "chunk_ids": [citation["chunk_id"] for citation in self.citations], "result": self.result,
        }


def valid_plan_metrics(result, args):
    """Reject malformed totals, scope, dates and coverage before numeric use."""
    try:
        if result["scope"] != "plan" or str(result["plan_id"]) != str(args["plan_id"]):
            return False
        if any(result[key] != args[key] for key in ("start_date", "end_date")):
            return False
        if not all(isinstance(result[key], str) and result[key] for key in ("source_id", "data_version", "source_plan_id")):
            return False
        source = result["source"]
        if source["kind"] not in ("public", "synthetic"):
            return False
        if not all(isinstance(source[key], str) and source[key] for key in
                   ("source_url", "license", "compressed_sha256", "tsv_sha256", "sampling")):
            return False
        if date.fromisoformat(source["time_mapping"]["anchor_date"]).isoformat() != source["time_mapping"]["anchor_date"]:
            return False
        rows = result["daily"]
        if not isinstance(rows, list):
            return False
        days = [row["metric_date"] for row in rows]
        if len(set(days)) != len(days) or any(not args["start_date"] <= d <= args["end_date"] for d in days):
            return False
        if any(date.fromisoformat(day).isoformat() != day for day in days):
            return False
        for row in [result, *rows]:
            if any(type(row[key]) is not int for key in ("impressions", "clicks")):
                return False
            if not 0 <= row["clicks"] <= row["impressions"]:
                return False
        if any(result[key] != sum(row[key] for row in rows) for key in ("impressions", "clicks")):
            return False
        coverage = result["coverage"]
        if not isinstance(coverage["missing_dates"], list) or any(
            not args["start_date"] <= day <= args["end_date"] for day in coverage["missing_dates"]
        ):
            return False
        expected = (date.fromisoformat(args["end_date"]) - date.fromisoformat(args["start_date"])).days + 1
        return (type(coverage["complete"]) is bool and coverage["expected_days"] == expected
                and coverage["observed_days"] == len(rows)
                and all(type(coverage[key]) is int for key in ("expected_days", "observed_days"))
                and (coverage["complete"] or bool(coverage["missing_dates"]))
                and (not coverage["complete"] or (len(rows) == expected and not coverage["missing_dates"])))
    except (KeyError, TypeError, ValueError):
        return False


def retrieved_ctr_policy(reference, receipt):
    """Executable semantics are registered per exact version AND retrieved original."""
    if receipt is None or receipt.status != "ok":
        return None
    return verified_ctr_policy(reference, receipt.result.get("evidence", []))


CTR_REVIEW_RULE = "ctr-review-v1#low-ctr"
_CTR_THRESHOLDS = {("ctr-review#low-ctr", "v1"): (1000, 20), ("ctr-review#low-ctr", "v2"): (2000, 15)}


def ctr_policy_thresholds(reference):
    minimum, decline = _CTR_THRESHOLDS[parse_rule_reference(reference)]
    return {"min_impressions": minimum, "min_relative_decline_pct": decline}


def verified_ctr_policy(reference, evidence):
    from backend.rag.review_rule_demo import DEMO_RULES

    identity = parse_rule_reference(reference)
    if identity not in _CTR_THRESHOLDS:
        return None
    expected = next(rule for rule in DEMO_RULES if rule[1:3] == identity)
    for hit in evidence:
        if not isinstance(hit, dict):
            continue
        if (hit.get("rule_id"), hit.get("version"), hit.get("content")) == (*identity, expected[4]):
            return {"rule_id": reference, **ctr_policy_thresholds(reference),
                    "content_sha256": hashlib.sha256(expected[4].encode()).hexdigest(),
                    "chunk_id": hit["chunk_id"], "original": expected[4]}
    return None


def calculate_ctr(baseline, current):
    base_ctr = Fraction(baseline["clicks"], baseline["impressions"])
    current_ctr = Fraction(current["clicks"], current["impressions"])
    relative = (base_ctr - current_ctr) / base_ctr * 100 if base_ctr else None
    return {
        "formula": "sum(clicks) / sum(impressions)",
        "baseline_ctr_pct": float(base_ctr * 100), "current_ctr_pct": float(current_ctr * 100),
        "decline_pp": float((base_ctr - current_ctr) * 100),
        "relative_decline_pct": float(relative) if relative is not None else None,
    }, relative


class DiagnosisExecution:
    """One turn's shared budget and receipts handed from workers to the orchestrator."""

    def __init__(self, snapshot, trace_id):
        self.snapshot = snapshot
        self.trace_id = trace_id
        self._rule_identity = parse_rule_reference(snapshot.rule_id)
        self._remaining = DIAGNOSIS_CALL_LIMIT
        self._call_sequence = 0
        self._pending = []
        self._receipts = []
        self._lock = Lock()

    def next_runtime_call_id(self):
        with self._lock:
            self._call_sequence += 1
            return f"call-{self._call_sequence}"

    def reserve_call(self):
        with self._lock:
            if self._remaining == 0:
                return False
            self._remaining -= 1
            return True

    def admit(self, call, role, allowed_tools, *, peers=(), parent_call_id=None):
        def reject(code, action=None):
            return None, self._reject(call, role, code, action=action, parent_call_id=parent_call_id)

        _, name, arguments = tool_call_fields(call)
        if not isinstance(name, str) or name not in allowed_tools:
            return reject("unknown_tool", action="unknown_tool")
        if not isinstance(arguments, str):
            return reject("invalid_tool_arguments")
        try:
            args = json.loads(arguments, object_pairs_hook=_unique_json_object,
                              parse_constant=_invalid_json_constant)
        except (ValueError, TypeError, RecursionError):
            return reject("invalid_tool_arguments")
        if not isinstance(args, dict):
            return reject("invalid_tool_arguments")
        if "user_id" in args:
            return reject("forbidden_user_id")
        if name in DIAGNOSIS_TOOLS:
            if not self.snapshot.permits_tool(name, args):
                return reject("diagnosis_parameters_mismatch")
        else:
            if args.keys() != CONTROL_FIELDS[name] or any(
                not isinstance(value, str) or not value.strip() for value in args.values()
            ):
                return reject("invalid_tool_arguments")
            if name == "call_peer" and args["peer_name"] not in peers:
                return reject("unknown_peer")
        if not self.reserve_call():
            return reject("diagnosis_call_limit")
        return args, None

    def _reject(self, call, role, error_code, *, action=None, parent_call_id=None):
        call_id, name, _ = tool_call_fields(call)
        with trace_tool_call(self.trace_id, role, action or name, call_id,
                             runtime_call_id=self.next_runtime_call_id(),
                             parent_call_id=parent_call_id) as step:
            step["error_code"] = error_code
        return {"success": False, "error_code": error_code}

    def record_receipt(self, name, args, result, *, runtime_call_id, tool_call_id,
                       parent_call_id, dispatch_call_id, retry_of=None):
        if name == "search_knowledge" and isinstance(result, str):
            try:
                result = json.loads(result)
            except (ValueError, RecursionError):
                result = None
        citations = []
        error_code = None
        status = "ok"
        if not isinstance(result, dict) or not result or result.get("success") is False:
            reported_code = result.get("error_code") if isinstance(result, dict) else None
            known_codes = ("temporary_tool_failure", "tool_exception", "plan_history_not_mapped",
                           "plan_history_not_configured", "source_version_mismatch", "plan_history_unavailable", "plan_not_found")
            error_code = (reported_code if reported_code in known_codes
                          else "tool_reported_failure")
            status = "temporary_failure" if error_code == "temporary_tool_failure" else "failed"
            result = None
        elif name == "get_daily_report_by_date":
            if not isinstance(result.get("reports"), list) or not result["reports"]:
                status = "empty"
        elif name == "get_plan_metrics_by_date":
            if not valid_plan_metrics(result, args):
                status, error_code, result = "failed", "invalid_metrics_receipt", None
        elif name == "search_knowledge":
            citations = self._exact_citations(result.get("evidence"))
            if not citations:
                status = "rule_not_found"
        scope = {"get_plan_detail": "plan", "get_daily_report_by_date": "user",
                 "get_plan_metrics_by_date": "plan",
                 "search_knowledge": "rule"}[name]
        receipt = DiagnosisReceipt(
            runtime_call_id, tool_call_id, parent_call_id, dispatch_call_id, name, args, scope, status, error_code,
            citations, result, retry_of,
        )
        with self._lock:
            self._pending.append(receipt)
        return error_code

    def _exact_citations(self, evidence):
        citations = []
        if self._rule_identity is None or not isinstance(evidence, list):
            return citations
        for hit in evidence:
            if not isinstance(hit, dict) or (hit.get("rule_id"), hit.get("version")) != self._rule_identity:
                continue
            chunk_id, title = hit.get("chunk_id"), hit.get("title")
            if not isinstance(chunk_id, str) or not chunk_id.strip() or not isinstance(title, str) or not title.strip():
                continue
            citations.append({
                "chunk_id": chunk_id, "rule_id": hit["rule_id"], "version": hit["version"], "title": title,
                "url": f"/api/knowledge/chunks/{quote(chunk_id, safe='')}",
            })
        return citations

    def receive(self):
        with self._lock:
            receipts, self._pending = self._pending, []
        self._receipts.extend(receipts)
        for receipt in receipts:
            add_trace_step(
                self.trace_id, "evidence_received", receipt.tool, detail=receipt.status,
                role="orchestrator", runtime_call_id=receipt.runtime_call_id,
                tool_call_id=receipt.tool_call_id, parent_call_id=receipt.parent_call_id,
                retry_of=receipt.retry_of, error_code=receipt.error_code,
                status="ok" if receipt.status == "ok" else "error",
            )
        return receipts

    @staticmethod
    def _receipt_key(name, args):
        return name, tuple(sorted((key, str(value)) for key, value in args.items()))

    def _evidence(self):
        plan_metrics = any(r.tool == "get_plan_metrics_by_date" for r in self._receipts)
        metric_tool = "get_plan_metrics_by_date" if plan_metrics else "get_daily_report_by_date"
        metric_scope = {"plan_id": self.snapshot.plan_id} if plan_metrics else {}
        required = (
            ("计划详情", "get_plan_detail", {"plan_id": self.snapshot.plan_id}),
            ("基准窗口计划指标" if plan_metrics else "基准窗口用户日报", metric_tool, {**metric_scope, **self.snapshot.baseline.model_dump()}),
            ("当前窗口计划指标" if plan_metrics else "当前窗口用户日报", metric_tool, {**metric_scope, **self.snapshot.current.model_dump()}),
            ("指定规则查询", "search_knowledge", {"query": self.snapshot.rule_id}),
        )
        by_scope = {}
        for receipt in self._receipts:
            key = self._receipt_key(receipt.tool, receipt.arguments)
            previous = by_scope.get(key)
            if (previous is None or receipt.status == "ok"
                    or previous.status == "temporary_failure"):
                by_scope[key] = receipt
        return [(label, by_scope.get(self._receipt_key(name, args))) for label, name, args in required]

    def retry_candidate(self):
        missing = [receipt for _, receipt in self._evidence() if receipt is None or receipt.status != "ok"]
        if missing and missing[0] is not None and all(receipt is missing[0] for receipt in missing):
            receipt = missing[0]
            if receipt.status == "temporary_failure" and receipt.retry_of is None:
                return receipt
        return None

    def can_fallback_after_handoff(self):
        # Preserve the model fallback when an initial dispatch left tools uncalled.
        evidence = [receipt for _, receipt in self._evidence()]
        return None in evidence and all(receipt is None or receipt.status == "ok" for receipt in evidence)

    def _plan_reply_event(self):
        evidence = self._evidence()
        gaps, metrics = [], {}
        for index, (label, receipt) in enumerate(evidence):
            if receipt is None or receipt.status != "ok":
                detail = "未找到指定规则的精确版本证据" if receipt and receipt.status == "rule_not_found" else "缺少成功回执：" + label
                if receipt and receipt.error_code:
                    detail += "（" + receipt.error_code + "）"
                gaps.append(detail)
            elif index in (1, 2):
                window = "baseline" if index == 1 else "current"
                metrics[window] = receipt.result
                if not receipt.result["coverage"]["complete"]:
                    gaps.append(label + "缺窗/覆盖不全：" + "、".join(receipt.result["coverage"]["missing_dates"]))
                if not receipt.result["impressions"]:
                    gaps.append(label + "零曝光，CTR 无法计算")
        policy = retrieved_ctr_policy(self.snapshot.rule_id, evidence[3][1])
        if evidence[3][1] and evidence[3][1].status == "ok" and policy is None:
            gaps.append("指定规则版本缺少已验证的可计算条文，不能默认套用 v1")
        comparable = False
        if len(metrics) == 2:
            base, current = metrics["baseline"], metrics["current"]
            identity_fields = ("source_id", "data_version", "source_plan_id", "source")
            if any(base[key] != current[key] for key in identity_fields):
                gaps.append("两窗来源、版本或来源计划不一致")
            if base["coverage"]["expected_days"] != current["coverage"]["expected_days"]:
                gaps.append("两窗天数不一致，缺少可比窗口")
            comparable = (all(base[key] == current[key] for key in identity_fields)
                          and base["coverage"]["expected_days"] == current["coverage"]["expected_days"]
                          and base["coverage"]["complete"] and current["coverage"]["complete"])
        if policy:
            for window, result in metrics.items():
                if result["impressions"] < policy["min_impressions"]:
                    gaps.append(f"{window} 样本不足：曝光 {result['impressions']} < {policy['min_impressions']}")
        calculation, evaluation = None, None
        if comparable and all(r["impressions"] for r in metrics.values()):
            calculation, relative = calculate_ctr(base, current)
            if relative is None:
                gaps.append("基准 CTR 为零，相对降幅无定义")
            if not gaps and policy:
                evaluation = {**policy, "matched": relative >= policy["min_relative_decline_pct"]}
        citations = {c["chunk_id"]: c for _, r in evidence if r for c in r.citations}
        content = "证据不足。" if gaps else "计划历史诊断完成。"
        for window, result in metrics.items():
            mapping = result["source"]["time_mapping"]
            content += (f"\n{window} {result['start_date']} 至 {result['end_date']}：曝光 {result['impressions']}，点击 {result['clicks']}。"
                        f"来源 {result['source_id']} / {result['source_plan_id']}，版本 {result['data_version']}，{result['source']['source_url']}。"
                        f"\n{'公开数据' if result['source']['kind'] == 'public' else '合成样本'}，许可 {result['source']['license']}；"
                        f"时间映射：相对时间 0 对应演示日期 {mapping['anchor_date']}，每 86400 秒为一天，非真实日历日期。"
                        f"覆盖 {result['coverage']['observed_days']}/{result['coverage']['expected_days']} 天，"
                        + ("完整。" if result['coverage']['complete'] else "不完整。"))
        if calculation:
            content += (f"\nCTR（总点击/总曝光）：基准 {calculation['baseline_ctr_pct']:.6f}%，当前 {calculation['current_ctr_pct']:.6f}%；"
                        f"百分点下降 {calculation['decline_pp']:.6f}，相对降幅 "
                        + (f"{calculation['relative_decline_pct']:.6f}%。" if calculation['relative_decline_pct'] is not None else "无定义。"))
        if evaluation:
            content += f"\n按本轮检索规则 {self.snapshot.rule_id}：" + ("达到阈值，建议人工复核。" if evaluation["matched"] else "未达到复核阈值。")
        if gaps:
            content += "\n本轮缺项：" + "；".join(gaps) + "。已停止查询。"
            if self._remaining == 0:
                content += "本轮工具执行额度已耗尽。"
        for citation in citations.values():
            content += f"\n本轮规则引用 {self.snapshot.rule_id}，原文 {citation['url']}"
        stop_reason = ("diagnosis_call_limit" if self._remaining == 0 else "missing_evidence") if gaps else "receipts_complete"
        add_trace_step(self.trace_id, "evidence_stop", stop_reason, role="orchestrator")
        add_trace_step(self.trace_id, "guardrail", "diagnosis_evidence", status="error" if gaps else "ok",
                       error_code="insufficient_evidence" if gaps else None)
        return {"type": "text", "content": content, "citations": list(citations.values()),
                "evidence_gaps": gaps, "metrics": metrics, "calculation": calculation, "rule_evaluation": evaluation}

    def reply_event(self):
        if any(r.tool == "get_plan_metrics_by_date" for r in self._receipts):
            return self._plan_reply_event()
        evidence = self._evidence()
        missing = [label for label, receipt in evidence
                   if receipt is None or receipt.status in ("failed", "temporary_failure")]
        unavailable = [f"{label}无数据" for label, receipt in evidence if receipt and receipt.status == "empty"]
        if any(receipt and receipt.status == "rule_not_found" for _, receipt in evidence):
            unavailable.append("未找到指定规则的精确版本证据")
        citations = {citation["chunk_id"]: citation
                     for receipt in self._receipts for citation in receipt.citations}
        stop_reason = "receipts_complete"
        if missing or unavailable:
            stop_reason = "diagnosis_call_limit" if self._remaining == 0 else "missing_evidence"
        add_trace_step(self.trace_id, "evidence_stop", stop_reason, role="orchestrator")
        add_trace_step(self.trace_id, "guardrail", "diagnosis_evidence", status="error",
                       error_code="insufficient_evidence")
        historical_gap = "缺少该计划在基准窗口和当前窗口的历史 CTR"
        reply = f"证据不足。日期报表按用户和日期汇总，{historical_gap}。"
        if missing:
            reply += "本轮缺少成功回执（" + "、".join(missing) + "）。"
        if unavailable:
            reply += "本轮缺项（" + "、".join(unavailable) + "）。"
        if stop_reason == "diagnosis_call_limit":
            reply += "本轮工具执行额度已耗尽，已停止查询。"
        elif any(receipt and receipt.status == "temporary_failure" and receipt.retry_of
                 for _, receipt in evidence):
            reply += "补查后仍遇到临时故障，已停止查询。"
        elif missing or unavailable:
            reply += "已停止查询。"
        for citation in citations.values():
            reply += f"\n本轮规则引用 {self.snapshot.rule_id}，原文 {citation['url']}"
        return {
            "type": "text", "content": reply, "citations": list(citations.values()),
            "evidence_gaps": [historical_gap, *(f"缺少成功回执：{label}" for label in missing), *unavailable],
        }
