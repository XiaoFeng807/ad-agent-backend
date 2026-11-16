"""Dispatch read-only evidence work for a natural homepage diagnosis."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import date, timedelta
import json
import re
from threading import Lock
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.agent.diagnosis import (
    DIAGNOSIS_CALL_LIMIT, DateWindow, DiagnosisReceipt, calculate_ctr, diagnosis_tool_error,
    valid_plan_metrics, verified_ctr_policy, _invalid_json_constant, _unique_json_object,
)
from backend.agent.llm_provider import create_completion
from backend.agent.request_budget import tool_call_fields
from backend.database import database
from backend.database.diagnosis_pauses import PAUSE_RULE_ID
from backend.database.diagnosis_tasks import OWNED_ACTIVE_SCOPE, active_analysis_candidates
from backend.observability import add_trace_step, trace_tool_call
from backend.rag.rule_reference import parse_rule_reference
from backend.tools.plan_metrics import plan_metric_coverage, query_plan_metrics


MAX_CANDIDATES = 4
MAX_WINDOW_DAYS = 14
PREFERRED_WINDOW_DAYS = 7
MAX_DISPATCHES = 2
MAX_MODEL_CALLS_PER_DISPATCH = 3
METRICS = "get_plan_metrics_by_date"
KNOWLEDGE = "search_knowledge"


class PlanWindow(DateWindow):
    plan_id: int = Field(strict=True, gt=0, le=2**63 - 1)


class RuleQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=128)


TOOLS = {
    METRICS: (PlanWindow, "查询一个候选在指定日期窗的计划级曝光、点击、日期覆盖和来源。"),
    KNOWLEDGE: (RuleQuery, "按指定完整版本 ID 检索 CTR 复核规则原文和分块。"),
}
SYSTEMS = {
    METRICS: "你是数据 Agent。按任务清单逐项调用计划指标工具。每组参数对应一次工具调用，"
             "首次尽量在同一回复并行发出全部查询。后续只调用从未发出的项。"
             "已发出但失败的项由主编排处理。",
    KNOWLEDGE: "你是知识 Agent。调用规则检索工具，query 使用任务指定的完整版本 ID。"
               "检索失败由主编排处理。",
}


def parse_request(message):
    """Accept only an unambiguous supported comparison, without inferring extra scope."""
    text = re.sub(r"\s+", "", message or "").rstrip("。.!！")
    temporal = re.compile(r"(?:最近|当前)([一二三四五六七八九十\d]+)天(?:和|与|、|对比|相比)(?:之前|此前|前|上一)([一二三四五六七八九十\d]+)天")
    matches = list(temporal.finditer(text))
    days = None
    if matches:
        if len(matches) != 1:
            return None
        digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
                  "八": 8, "九": 9, "十": 10}
        def number(value):
            return int(value) if value.isascii() and value.isdigit() else digits.get(value)
        left, right = map(number, matches[0].groups())
        if left != right or left is None or not 1 <= left <= MAX_WINDOW_DAYS:
            return None
        days = left
        text = text[:matches[0].start()] + "最近" + text[matches[0].end():]
    pattern = (r"(?:请|帮我|请帮我)?(?:找出|查找|分析|诊断|比较)"
               r"(?:最近)?(?:点击率|CTR|ctr)(?:下滑|下降)(?:最明显|最多|最大)"
               r"(?:、|，|,)?(?:值得暂停复核的|需要复核的|的)?"
               r"(?:一条|一项)?(?:投放计划|广告计划|计划)")
    if not re.fullmatch(pattern, text):
        return None
    return {"window_days": days}


def evidence_key(args):
    return tuple(sorted(args.items()))


class NaturalDiagnosisExecution:
    def __init__(self, user_id, trace_id, snapshot=None):
        self.user_id = user_id
        self.trace_id = trace_id
        self.snapshot = snapshot
        self.rule_id = (snapshot.inherited_rule_id if snapshot else None) or PAUSE_RULE_ID
        self.candidates = []
        self.candidate_scope = None
        self.discovery = None
        self.metrics = {}
        self.rule = None
        self.policy = None
        self.outcome = None
        self.gaps = []
        self.calls = 0
        self.sequence = 0
        self.lock = Lock()

    def next_id(self):
        with self.lock:
            self.sequence += 1
            return f"call-{self.sequence}"

    def reserve_tool(self):
        with self.lock:
            if self.calls >= DIAGNOSIS_CALL_LIMIT:
                return False
            self.calls += 1
            return True

    def discover(self, call_id):
        if self.discovery is not None:
            return self.discovery
        with closing(database.get_diagnosis_db(read_only=True)) as conn:
            self.candidates = active_analysis_candidates(conn, self.user_id, limit=MAX_CANDIDATES + 1)
        count = len(self.candidates)
        self.candidate_scope = {"kind": OWNED_ACTIVE_SCOPE, "max_candidates": MAX_CANDIDATES}
        if count > MAX_CANDIDATES:
            return {"error_code": "candidate_limit", "candidate_scope": self.candidate_scope, "gaps": [
                f"本人 active 计划超过 {MAX_CANDIDATES} 条，本轮未比较。请缩小分析范围，或在手动入口指定单条计划复核。",
            ]}
        self.candidate_scope["plan_ids"] = [item["plan_id"] for item in self.candidates]
        coverage, error = plan_metric_coverage(self.candidates) if count else ([], None)
        if error:
            return {"error_code": error, "gaps": [f"无法读取计划历史覆盖（{error}）。"]}
        starts = [item["start_date"] for item in coverage if "error_code" not in item]
        ends = [item["end_date"] for item in coverage if "error_code" not in item]
        first, latest = (max(starts), min(ends)) if count and len(starts) == count else (None, None)
        gaps = [f"计划 #{item['plan_id']} 缺少可用历史（{item['error_code']}）。"
                for item in coverage if "error_code" in item]
        if count < 2:
            gaps.append("比较至少需要两条本人 active 计划。")
        if first and (date.fromisoformat(latest) - date.fromisoformat(first)).days < 1:
            gaps.append("候选共同覆盖不足两天，无法选取两个可比窗口。")
        self.discovery = {"candidates": coverage, "candidate_scope": self.candidate_scope,
                          "common_start_date": first, "latest_common_date": latest,
                          "metrics_included": False, "runtime_call_id": call_id, "gaps": gaps}
        return self.discovery

    def get_metrics(self, args):
        if args["plan_id"] not in {item["plan_id"] for item in self.discovery["candidates"]}:
            return {"error_code": "candidate_out_of_scope"}
        start, end = date.fromisoformat(args["start_date"]), date.fromisoformat(args["end_date"])
        if (end - start).days >= MAX_WINDOW_DAYS:
            return {"error_code": "window_too_long"}
        result = query_plan_metrics(**args, user_id=self.user_id, connect=lambda: database.get_diagnosis_db(read_only=True))
        if not result.get("success"):
            return result
        if not valid_plan_metrics(result, args):
            return {"error_code": "invalid_metrics_receipt"}
        binding = next(item for item in self.candidates if item["plan_id"] == args["plan_id"])
        if any(result[key] != binding[key] for key in ("source_id", "data_version", "source_plan_id")):
            return {"error_code": "candidate_source_changed"}
        return result

    def search_rule(self, query):
        from backend.rag.rag_knowledge import get_rag_knowledge

        rag = get_rag_knowledge()
        result = rag.generate(query) or {}
        if result.get("error_code"):
            return {"error_code": result["error_code"]}
        identity = parse_rule_reference(self.rule_id)
        version_hits = [hit for hit in result.get("evidence", []) if isinstance(hit, dict)
                        and (hit.get("rule_id"), hit.get("version")) == identity]
        evidence = [hit for hit in version_hits
                    if isinstance(hit.get("chunk_id"), str) and isinstance(hit.get("title"), str)
                    and isinstance(hit.get("content"), str) and 0 < len(hit["content"]) <= 1800]
        policy = verified_ctr_policy(self.rule_id, evidence)
        if policy is None:
            return {"error_code": "rule_version_unverified"}
        if any(hit.get("content") != policy["original"] for hit in version_hits):
            return {"error_code": "rule_version_conflict"}
        hit = next(item for item in evidence if item["chunk_id"] == policy["chunk_id"])
        if not hit["chunk_id"] or not hit["title"] or rag.get_chunk(hit["chunk_id"]) != hit:
            return {"error_code": "rule_version_unverified"}
        return {"evidence": [hit], "policy": policy}

    def compare(self, windows, call_id):
        gaps = []
        ranking = []
        campaigns = set()
        for candidate in self.candidates:
            plan = candidate["plan_id"]
            metrics, metric_calls = {}, {}
            for name, window in windows.items():
                receipt = self.metrics.get((plan, window["start_date"], window["end_date"]))
                if receipt is None:
                    gaps.append(f"计划 #{plan} 缺少 {name} 窗口指标回执。")
                    continue
                result, metric_calls[name] = receipt
                metrics[name] = result
                if not result["coverage"]["complete"]:
                    gaps.append(f"计划 #{plan} {name} 缺报：" + "、".join(result["coverage"]["missing_dates"]))
                if self.policy and result["impressions"] < self.policy["min_impressions"]:
                    gaps.append(f"计划 #{plan} {name} 曝光 {result['impressions']}，低于规则要求 {self.policy['min_impressions']}。")
            if len(metrics) != 2:
                continue
            base, recent = metrics["baseline"], metrics["current"]
            if any(base[key] != recent[key] for key in ("source_id", "data_version", "source_plan_id", "source")):
                gaps.append(f"计划 #{plan} 两窗指标来源或版本不一致。")
            campaign = (base["source_id"], base["data_version"], base["source_plan_id"])
            if campaign in campaigns:
                gaps.append("候选映射到同一来源计划，缺少独立的比较样本。")
            campaigns.add(campaign)
            if not (base["clicks"] and recent["impressions"]):
                gaps.append(f"计划 #{plan} 基准 CTR 为零或当前窗零曝光，相对降幅无法计算。")
                continue
            calculation, decline = calculate_ctr(base, recent)
            ranking.append((decline, plan, metrics, calculation, metric_calls))
        if gaps:
            return {"error_code": "insufficient_evidence", "gaps": gaps}
        ranking.sort(key=lambda item: item[0], reverse=True)
        decline, plan, metrics, calculation, _ = ranking[0]
        if ranking[1][0] == decline:
            return {"error_code": "tied_candidates", "gaps": ["最高相对降幅并列，无法确定唯一复核计划。"]}
        comparison = {
            "latest_common_date": self.discovery["latest_common_date"],
            "common_start_date": self.discovery["common_start_date"],
            "ranking_metric": "relative_ctr_decline",
            "candidates": [{
                "plan_id": item_plan, "calculation": item_calculation, "metric_calls": calls,
                "metrics": {name: {key: value for key, value in result.items() if key != "daily"}
                            for name, result in item_metrics.items()},
            } for _, item_plan, item_metrics, item_calculation, calls in ranking],
            "discovery_call_id": self.discovery["runtime_call_id"], "submission_call_id": call_id,
        }
        evaluation = {**self.policy, "matched": decline >= self.policy["min_relative_decline_pct"]}
        self.outcome = {
            "plan_id": plan, "rule_id": self.rule_id, **windows, "comparison": comparison,
            "metrics": metrics, "calculation": calculation, "rule_evaluation": evaluation,
            "citations": [self.rule], "candidate_revision": self.candidates,
        }
        return {"plan_id": plan, "matched": evaluation["matched"], "calculation": calculation}

    def trace_receipt(self, receipt):
        add_trace_step(
            self.trace_id, "evidence_received", receipt.tool, detail=receipt.status,
            role="orchestrator", runtime_call_id=receipt.runtime_call_id,
            tool_call_id=receipt.tool_call_id, parent_call_id=receipt.dispatch_call_id,
            retry_of=receipt.retry_of, error_code=receipt.error_code,
            status="ok" if receipt.status == "ok" else "error",
        )

    def read_evidence(self, role, tool, args, dispatch_id, *, sdk_id=None, retry_of=None):
        call_id = self.next_id()
        if not self.reserve_tool():
            add_trace_step(self.trace_id, "guardrail", tool, role=role, runtime_call_id=call_id,
                           parent_call_id=dispatch_id, tool_call_id=sdk_id,
                           status="error", error_code="diagnosis_call_limit")
            result = {"error_code": "diagnosis_call_limit"}
        else:
            with trace_tool_call(self.trace_id, role, tool, sdk_id, runtime_call_id=call_id,
                                 parent_call_id=dispatch_id, retry_of=retry_of) as step:
                try:
                    result = self.get_metrics(args) if tool == METRICS else self.search_rule(args["query"])
                except Exception as error:
                    result = {"error_code": diagnosis_tool_error(error)}
                step["error_code"] = result.get("error_code")
        code = result.get("error_code")
        status = "failed" if code else "ok"
        if code == "temporary_tool_failure":
            status = "temporary_failure"
        if tool == METRICS and not code and not result["coverage"]["complete"]:
            status, code = "missing_coverage", "missing_plan_dates"
        citations = []
        if tool == KNOWLEDGE and not code:
            hit = result["evidence"][0]
            citations = [{
                **{key: hit[key] for key in ("chunk_id", "rule_id", "version", "title")},
                "url": "/api/knowledge/chunks/" + quote(hit["chunk_id"], safe=""),
                "runtime_call_id": call_id,
            }]
        return DiagnosisReceipt(
            call_id, sdk_id, dispatch_id, dispatch_id, tool, args,
            "plan" if tool == METRICS else "rule", status, code, citations, result, retry_of,
        )

    def run_role(self, provider, model, budget, role, tool, tasks, dispatch_id):
        schema, description = TOOLS[tool]
        tools = [{"type": "function", "function": {
            "name": tool, "description": description, "parameters": schema.model_json_schema(),
        }}]
        wire = [{"role": "system", "content": SYSTEMS[tool]},
                {"role": "user", "content": json.dumps({"required_queries": tasks}, ensure_ascii=False)}]
        pending = {evidence_key(args): args for args in tasks}
        receipts = []
        stop = None
        for index in range(MAX_MODEL_CALLS_PER_DISPATCH):
            if not pending:
                break
            add_trace_step(self.trace_id, "model_call", "natural_evidence", role=role,
                           runtime_call_id=self.next_id(), parent_call_id=dispatch_id,
                           detail="select_queries")
            message = create_completion(
                provider, budget, role=role, step="natural_evidence", model=model,
                messages=wire, tools=tools, tool_choice="auto",
                temperature=0, max_tokens=1800, sdk_max_retries=0,
            ).choices[0].message
            if not message.tool_calls:
                stop = "dispatch_queries_incomplete"
                break
            wire.append({"role": "assistant", "content": message.content, "tool_calls": message.tool_calls})
            for call in message.tool_calls:
                sdk_id, name, raw_args = tool_call_fields(call)
                error = "unknown_tool" if name != tool else None
                if not error:
                    try:
                        args = schema.model_validate(json.loads(
                            raw_args, object_pairs_hook=_unique_json_object, parse_constant=_invalid_json_constant,
                        )).model_dump()
                    except (ValidationError, ValueError, TypeError, RecursionError):
                        error = "invalid_tool_arguments"
                if not error:
                    key = evidence_key(args)
                    if key not in pending:
                        error = "evidence_already_requested" if any(
                            args == receipt.arguments for receipt in receipts
                        ) else ("candidate_out_of_scope" if tool == METRICS and
                                args["plan_id"] not in self.candidate_scope["plan_ids"]
                                else "dispatch_parameters_mismatch")
                if error:
                    add_trace_step(self.trace_id, "guardrail", tool, role=role, status="error",
                                   error_code=error, tool_call_id=sdk_id, parent_call_id=dispatch_id)
                    payload = {"error_code": error}
                else:
                    receipt = self.read_evidence(role, tool, pending.pop(key), dispatch_id, sdk_id=sdk_id)
                    receipts.append(receipt)
                    payload = receipt.handoff()
                    payload["result"] = {k: v for k, v in receipt.result.items()
                                         if k not in ("daily", "source", "policy")}
                wire.append({"role": "tool", "tool_call_id": sdk_id,
                             "content": json.dumps(payload, ensure_ascii=False)})
        if pending and not stop:
            stop = "diagnosis_model_call_limit"
        if stop:
            add_trace_step(self.trace_id, "guardrail", "natural_evidence", role=role, status="error",
                           parent_call_id=dispatch_id, error_code=stop)
        return receipts, stop

    def collect(self, provider, model, budget, jobs):
        collected = {}
        stops = {}
        dispatches = {}
        with ThreadPoolExecutor(max_workers=MAX_DISPATCHES) as executor:
            futures = {}
            for role, tool, tasks in jobs:
                dispatch_id = self.next_id()
                dispatches[tool] = (role, dispatch_id)
                add_trace_step(self.trace_id, "dispatch", tool, detail=f"{len(tasks)} queries",
                               role=role, runtime_call_id=dispatch_id,
                               parent_call_id=self.discovery["runtime_call_id"])
                future = executor.submit(self.run_role, provider, model, budget, role, tool, tasks, dispatch_id)
                futures[future] = (tool, dispatch_id)
            for future in as_completed(futures):
                tool, dispatch_id = futures[future]
                receipts, stops[tool] = future.result()
                for receipt in receipts:
                    if (receipt.tool, receipt.dispatch_call_id) == (tool, dispatch_id):
                        self.trace_receipt(receipt)
                        collected[tool, evidence_key(receipt.arguments)] = receipt

        required = [(tool, args) for _, tool, tasks in jobs for args in tasks]
        missing = [(tool, args) for tool, args in required
                   if (receipt := collected.get((tool, evidence_key(args)))) is None or receipt.status != "ok"]
        if len(missing) == 1:
            tool, args = missing[0]
            key = tool, evidence_key(args)
            if (failed := collected.get(key)) and failed.status == "temporary_failure":
                role, dispatch_id = dispatches[tool]
                add_trace_step(self.trace_id, "retry", tool, role="orchestrator",
                               parent_call_id=dispatch_id, retry_of=failed.runtime_call_id)
                retried = self.read_evidence(role, tool, failed.arguments, dispatch_id,
                                             retry_of=failed.runtime_call_id)
                self.trace_receipt(retried)
                collected[key] = retried
        return collected, stops

    def reply(self):
        scope = {"candidate_scope": self.candidate_scope} if self.candidate_scope else {}
        plan_ids = (self.candidate_scope or {}).get("plan_ids", [])
        plans = "、".join(f"#{plan}" for plan in plan_ids)
        if self.outcome is None:
            gaps = list(self.gaps)
            if self.discovery is None and not gaps:
                gaps.append("缺少成功的候选和历史覆盖查询。")
            for (plan, start, end), (result, _) in self.metrics.items():
                if not result["coverage"]["complete"]:
                    gap = f"计划 #{plan} 在 {start} 至 {end} 缺报：" + "、".join(result["coverage"]["missing_dates"])
                    gaps.append(gap)
            if not gaps:
                gaps.append("缺少对所有候选的两窗指标及通过校验的比较结论。")
            content = f"发现本人 active 计划 {plans}，本轮未完成比较。\n" if plans else ""
            return {"type": "text", "content": content + "证据不足。\n" + "\n".join(dict.fromkeys(gaps)),
                    "evidence_gaps": list(dict.fromkeys(gaps)), **scope}
        outcome = self.outcome
        comparison = outcome["comparison"]
        content = (f"比较了 {len(plan_ids)} 条本人 active 计划（{plans}）。"
                   f"“最近”以指标覆盖的最新共同日期 {comparison['latest_common_date']} 为准。"
                   f"基准 {outcome['baseline']['start_date']} 至 {outcome['baseline']['end_date']}，"
                   f"当前 {outcome['current']['start_date']} 至 {outcome['current']['end_date']}。"
                   "按两窗总点击除以总曝光计算 CTR，再按相对降幅排序。")
        matched = outcome["rule_evaluation"]["matched"]
        content += (f"\n计划 #{outcome['plan_id']} 的相对降幅居首。按 {outcome['rule_id']}，"
                    + ("达到复核阈值，可确认创建复核单。" if matched else "未达到复核阈值。"))
        return {"type": "text", "content": content, "evidence_gaps": [], **scope, **outcome}

    def run(self, provider, model, budget, message=""):
        def finish(code=None):
            add_trace_step(self.trace_id, "guardrail", "natural_diagnosis_evidence", role="orchestrator",
                           status="error" if code else "ok", error_code=code,
                           detail=("review_threshold_met" if self.outcome["rule_evaluation"]["matched"]
                                   else "below_review_threshold") if self.outcome else "")
            return self.reply()

        source_turn = self.snapshot.source_turn_id if self.snapshot else None
        if source_turn:
            followup = re.fullmatch(r"\s*继续这次比较[，,]\s*改成(.+?)\s*[。.!！]?\s*", message)
            params = parse_request("找出" + followup.group(1) + "点击率下降最明显的计划") if followup else None
            if params and params["window_days"] is None:
                params = None
            add_trace_step(self.trace_id, "context", "natural_continuation", role="orchestrator",
                           detail=json.dumps({"source_turn_id": source_turn, "inherited": ["comparison_target", "candidate_scope_definition", "rule_id"], "overridden": ["window"]}, ensure_ascii=False))
            add_trace_step(self.trace_id, "context", "role_fields", role="data_agent", detail="plan_id,start_date,end_date")
            add_trace_step(self.trace_id, "context", "role_fields", role="knowledge_agent", detail="query")
        else:
            params = parse_request(message)
        if params is None:
            self.gaps = ["无法确定受支持的 CTR 下滑比较需求或日期窗；请说明比较最近与之前等长的天数，以及要找出下滑最明显的计划。"]
            return finish("unsupported_natural_request")
        discovery_id = self.next_id()
        if not self.reserve_tool():
            self.gaps = ["本轮业务工具调用已达上限。"]
            return finish("diagnosis_call_limit")
        with trace_tool_call(self.trace_id, "orchestrator", "discover_analysis_candidates",
                             runtime_call_id=discovery_id) as step:
            discovery = self.discover(discovery_id)
            step["error_code"] = discovery.get("error_code")
        self.gaps = discovery["gaps"]
        if self.gaps:
            return finish(discovery.get("error_code", "candidate_coverage_insufficient"))

        first = date.fromisoformat(discovery["common_start_date"])
        latest = date.fromisoformat(discovery["latest_common_date"])
        available = (latest - first).days + 1
        days = params["window_days"] or min(PREFERRED_WINDOW_DAYS, MAX_WINDOW_DAYS, available // 2)
        if available < 2 * days:
            self.gaps.append(f"共同覆盖仅 {available} 天，不足两个相邻的 {days} 天窗口（需要 {2 * days} 天）。")
            return finish("candidate_coverage_insufficient")
        windows = {
            "baseline": {"start_date": str(latest - timedelta(days=2 * days - 1)),
                         "end_date": str(latest - timedelta(days=days))},
            "current": {"start_date": str(latest - timedelta(days=days - 1)), "end_date": str(latest)},
        }
        add_trace_step(self.trace_id, "decision", "select_common_windows", detail=json.dumps(windows),
                       role="orchestrator", parent_call_id=discovery_id)
        metric_tasks = [{"plan_id": plan, **window} for plan in self.candidate_scope["plan_ids"]
                        for window in windows.values()]
        rule_args = {"query": self.rule_id}
        jobs = (("data_agent", METRICS, metric_tasks), ("knowledge_agent", KNOWLEDGE, [rule_args]))
        if len(jobs) > MAX_DISPATCHES:
            self.gaps.append("本轮取证分派已达上限。")
            return finish("diagnosis_dispatch_limit")
        receipts, stops = self.collect(provider, model, budget, jobs)
        errors = set()
        for args in metric_tasks:
            plan, start, end = args["plan_id"], args["start_date"], args["end_date"]
            receipt = receipts.get((METRICS, evidence_key(args)))
            if receipt and receipt.status in ("ok", "missing_coverage"):
                self.metrics[plan, start, end] = receipt.result, receipt.runtime_call_id
            if not receipt or receipt.status != "ok":
                code = receipt.error_code if receipt else stops[METRICS] or "dispatch_queries_incomplete"
                errors.add(code)
                if code != "missing_plan_dates":
                    self.gaps.append(f"计划 #{plan} 在 {start} 至 {end} 缺少成功的指标回执（{code}）。")
        rule = receipts.get((KNOWLEDGE, evidence_key(rule_args)))
        if rule and rule.status == "ok":
            self.rule, = rule.citations
            self.policy = rule.result["policy"]
        else:
            code = rule.error_code if rule else stops[KNOWLEDGE] or "dispatch_queries_incomplete"
            errors.add(code)
            self.gaps.append(f"缺少指定版本 {self.rule_id} 可回查的规则原文（{code}）。")
        if errors:
            reason = "diagnosis_call_limit" if "diagnosis_call_limit" in errors else ",".join(sorted(errors))
            return finish(reason)
        if not self.reserve_tool():
            self.gaps.append("本轮业务工具调用已达上限，未提交比较。")
            return finish("diagnosis_call_limit")

        call_id = self.next_id()
        with trace_tool_call(self.trace_id, "orchestrator", "submit_comparison", runtime_call_id=call_id,
                             parent_call_id=discovery_id) as step:
            result = self.compare(windows, call_id)
            step["error_code"] = result.get("error_code")
        self.gaps = result.get("gaps", [])
        return finish(result.get("error_code"))
