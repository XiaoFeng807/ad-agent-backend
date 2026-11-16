"""Freeze the explicit two-plan review requested in this work-group turn."""

import json
import re
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from backend.agent.conditional_detail import CONDITIONAL_READS, DETAIL, METRICS, DetailAdmission
from backend.agent.diagnosis import DateWindow, calculate_ctr, verified_ctr_policy
from backend.agent.team_tools import MetricsArgs, PERMANENT_READ_GAPS
from backend.observability import add_trace_step
from backend.rag.rule_reference import canonical_rule_query, parse_rule_reference, rule_query_scope


RULE = "search_knowledge"
ALERTS = "get_alerts"
_SECTION = "ctr-review#low-ctr"
_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_RANGE = rf"(?:为|是|[:：])?\s*({_DATE})\s*(?:至|到|~|～|—|–)\s*({_DATE})"
_VERSION = r"v[0-9]+(?:\.[0-9]+)*"
_DETAIL_GATE = (
    r"(?:清单确定|确定清单|取证完成|核验完成|完成核验|"
    r"证据齐全且确认达到复核条件|(?:入选)?(?:名单|清单)(?:确认|确定))"
    r"(?:之|以)?后[，,\s]*(?:才|仅|只|再)[^。；\n]*入选[^。；\n]*(?:详情|明细)"
)
_METRIC_KEYS = ("source_id", "data_version", "source_plan_id", "impressions", "clicks")
_FIELDS = {"plan_name": "名称", "status": "当前状态", "daily_budget": "日预算"}
_GAPS = {
    "review_metrics_pending": "尚无本轮窗口指标回执",
    "review_metrics_conflict": "窗口指标回执冲突",
    "review_source_mismatch": "窗口的历史来源或版本不一致",
    "review_comparison_undefined": "曝光或基准 CTR 为零，无法计算相对变化",
    "review_rule_missing": "本轮缺少指定版本的可核验条文",
    "review_rule_conflict": "指定规则回执冲突",
    "review_alert_not_confirmed": "尚未核实未读严重 CTR 告警",
    "plan_history_not_mapped": "未绑定历史来源",
    "plan_history_missing_dates": "历史数据缺报",
    "source_version_mismatch": "历史来源版本不匹配",
}


@dataclass(frozen=True)
class ReviewGoal:
    plan_ids: tuple[int, ...]
    baseline: DateWindow
    current: DateWindow
    rule: str
    needs_alert: bool
    independent: bool = False

    @property
    def windows(self):
        return self.baseline, self.current

    def trace_fields(self):
        return {"plan_ids": self.plan_ids, "baseline": self.baseline.model_dump(),
                "current": self.current.model_dump(), "rule": self.rule, "needs_alert": self.needs_alert,
                **({"independent_review": True} if self.independent else {})}


def parse_review_goal(request):
    """Recognize the gated list intent before validating its explicit parameters."""
    if not (re.search(r"\bCTR\b|点击率", request, re.I) and re.search(r"复核|入选", request)
            and re.search(r"清单|入选", request) and re.search(r"详情|明细|名称|状态|预算", request)):
        return False, None
    plans = list(dict.fromkeys(re.findall(r"#([0-9]+)", request)))
    base = re.findall(rf"基准(?:窗口|窗)\s*{_RANGE}", request)
    current = re.findall(rf"当前(?:窗口|窗)\s*{_RANGE}", request)
    versions = set(re.findall(rf"(?<![\w.])({_VERSION})(?![\w.])", request.lower(), re.ASCII))
    rules = set(re.findall(rf"(?:《?点击率下降复核》?\s*({_VERSION})|ctr-review-({_VERSION})#low-ctr)",
                           request.lower()))
    rule_versions = {title or reference for title, reference in rules}
    if (len(plans) != 2 or any(len(plan) > 19 for plan in plans) or len(base) != 1 or len(current) != 1
            or len(re.findall(_DATE, request)) != 4 or versions != rule_versions or len(rule_versions) != 1
            or not re.search(_DETAIL_GATE, request)):
        return True, None
    version, = rule_versions
    ids = tuple(map(int, plans))
    try:
        baseline, recent = (DateWindow(start_date=start, end_date=end) for start, end in (*base, *current))
        for plan in ids:
            MetricsArgs(plan_id=plan, **baseline.model_dump())
    except ValidationError:
        return True, None
    lengths = [date.fromisoformat(window.end_date) - date.fromisoformat(window.start_date)
               for window in (baseline, recent)]
    if ids[0] == ids[1] or baseline.end_date >= recent.start_date or lengths[0] != lengths[1]:
        return True, None
    independent = any(
        re.search(r"独立复核", clause) and not re.search(
            r"(?:无需|不必|不需要|不用|取消|不要|不要求|无需安排).{0,28}独立复核", clause)
        for clause in re.split(r"[。；;\n]", request)
    )
    return True, ReviewGoal(ids, baseline, recent, f"ctr-review-{version}#low-ctr",
                            bool(re.search(r"(?:若|如果).{0,20}严重\s*(?:CTR|点击率)", request, re.I)), independent)


class MultiWindowReview(DetailAdmission):
    tool_names = CONDITIONAL_READS | {RULE, ALERTS}
    evidence_sources = frozenset({METRICS, RULE})
    reuse_detail_receipt = False

    def __init__(self, goal, board, user_id, conversation_id=None):
        super().__init__(goal, board, user_id)
        if not goal or not goal.needs_alert:
            self.tool_names = self.tool_names - {ALERTS}
        self._critical_alerts = None
        self._conversation_id = conversation_id
        self._admissions = {}

    def prompt(self):
        alert = "先用 get_alerts(unread_only=true) 核查未读严重 CTR 告警，再按题面条件继续。" if self.goal.needs_alert else ""
        if self.goal.independent:
            return (
                "用户要求独立复核。用 delegate_tasks 分派取证和另一位复核 Worker，主 Agent 负责调度。"
                f"{alert}"
                "取证 Worker 用 get_plan_metrics_by_date 取得每条候选的两窗指标，用 search_knowledge 查指定 rule。"
                "规则任务只负责核对条文。每份指标或规则只分给一个取证任务，取证任务之间无需依赖。"
                "取证可拆分。整个比较只安排一个复核任务，只需 get_plan_detail 能力，由它根据证据判断入选目标。"
                "交付目标已是独立复核清单时，可同批提交取证和复核。复核任务的 dependencies "
                "须列出全部取证任务的 task_id，运行时等待并交付回执。"
                "用户先要判断是否具备复核条件时，可以先只安排取证，取得回执后再决定是否追加。"
                "计划号、窗口日期和规则写在 objective。先取证再追加时，收到 review_required 后，"
                "把 independent_review.artifact_refs 整组复制到复核任务的 artifact_refs。"
                "任务单被拒绝时，根据 task_sheet_feedback 修正后重提。发现缺项时保留已有证据并报告缺项。"
                "最终清单及完成状态由服务端依据复核 Worker 的实际回执生成。\n"
                + json.dumps(self.goal.trace_fields(), ensure_ascii=False)
            )
        return ("本轮复核范围已固定。核验两条计划的四份窗口指标和指定版本条文后，只读取入选计划的详情。"
                f"{alert}可直接取证或委派。委派详情时声明证据依赖并引用产物。"
                "最终清单由服务端按本轮回执生成。\n" + json.dumps(self.goal.trace_fields(), ensure_ascii=False))

    def model_basis(self):
        # Other workers may publish evidence while this caller is in its model round.
        return None

    def review_inputs(self, receipts, worker_id):
        """Select exactly the verified receipts that can be sent to this reviewer."""
        receipts = [row for row in receipts if row["run_id"] == self._board.run_id
                    and row.get("worker_id") and row["worker_id"] != worker_id]
        decision, metrics, rule = self._evaluate(receipts)
        if decision["error_code"]:
            return decision, []
        indexed = {row["artifact_id"]: row for row in receipts}
        return decision, [indexed[artifact_id] for artifact_id in (*metrics, rule)]

    def review_scope(self, worker_id):
        return {"worker_id": worker_id, "run_id": self._board.run_id,
                "conversation_id": self._conversation_id}

    def admit_detail(self, plan_id, basis, reserve, *, worker_id=None, runtime_call_id=None):
        if plan_id not in self.goal.plan_ids:
            return {**self.decision(), "error_code": "review_wrong_target",
                    "requested_plan_id": plan_id, "executed": False}
        if not self.goal.independent:
            return super().admit_detail(plan_id, basis, reserve)
        if (not worker_id or not basis or not basis["model_call_id"]
                or any(basis.get(key) != value for key, value in self.review_scope(worker_id).items())):
            decision = self.decision()
            error = "review_worker_required" if not worker_id else "review_handoff_required"
            return {**decision, "error_code": error, "requested_plan_id": plan_id, "executed": False}
        decision = super().admit_detail(plan_id, basis, reserve)
        proof = {key: basis[key] for key in (
            "worker_id", "run_id", "conversation_id", "model_call_id", "action_model_call_id", "input_artifact_ids")}
        if decision["executed"]:
            with self._lock:
                self._admissions[plan_id] = (runtime_call_id, proof)
        return {**decision, "review_input": proof}

    def _confirmed_detail(self, row):
        if not self.goal.independent:
            return True
        call_id, proof = self._admissions.get(row["arguments"]["plan_id"], (None, {}))
        return call_id == row["tool_call_id"] and proof.get("worker_id") == row.get("worker_id")

    def handoff_status(self):
        decision = self.decision()
        if error := decision["error_code"]:
            return {"status": "evidence_pending", "error_code": error}
        targets = set(decision["target_plan_ids"])
        confirmed = {row["arguments"]["plan_id"] for row in self._board.tool_receipts(DETAIL)
                     if row["status"] == "succeeded" and self._confirmed_detail(row)}
        if targets and targets <= confirmed:
            return {"status": "completed"}
        snapshot = self._board.snapshot()
        receipts = [row for row in snapshot["receipts"] if row["status"] == "succeeded"
                    and snapshot["states"][row["task_id"]] == "succeeded"]
        worker_decision, evidence = self.review_inputs(receipts, None)
        if error := worker_decision["error_code"]:
            return {"status": "worker_evidence_pending", "error_code": error}
        if targets and targets <= self._attempted:
            return {"status": "review_incomplete"}
        return {"status": "review_required",
                "dependencies": list(dict.fromkeys(row["task_id"] for row in evidence)),
                "artifact_refs": [{key: row[key] for key in ("task_id", "artifact_id")} for row in evidence]}

    def record_alerts(self, rows):
        if isinstance(rows, list):
            with self._lock:
                self._critical_alerts = sum(
                    row.get("user_id") == self._user_id and row.get("is_read") == 0
                    and row.get("level") == "critical"
                    and (row.get("type") == "ctr_review" or re.search(r"\bCTR\b|点击率", row.get("message") or "", re.I))
                    for row in rows
                )

    def metric_error(self, args, result):
        if (args["plan_id"] not in self.goal.plan_ids or not any(
                (args["start_date"], args["end_date"]) == (window.start_date, window.end_date)
                for window in self.goal.windows)):
            return "review_metric_out_of_scope"
        if result is not None and (type(result.get("user_id")) is not int or result["user_id"] != self._user_id):
            return "invalid_metrics_receipt"

    def rule_error(self, query, result=None):
        scope = rule_query_scope(canonical_rule_query(query))
        if not scope or _SECTION not in scope[0] or scope[1] != parse_rule_reference(self.goal.rule)[1]:
            return "review_rule_out_of_scope"
        if result is not None:
            for hit in result["evidence"]:
                if (hit.get("rule_id") not in scope[0] or hit.get("version") != scope[1]
                        or (hit["rule_id"] == _SECTION and not verified_ctr_policy(self.goal.rule, [hit]))):
                    return "review_rule_conflict"

    def target_error(self, plan_id, basis, decision):
        if plan_id not in decision["target_plan_ids"] or (basis and plan_id not in basis["target_plan_ids"]):
            return "review_wrong_target"

    def pending_detail_decision(self):
        with self._lock:
            decision, metric_ids, rule_artifact = self._evaluate()
            decision["target_plan_ids"] = [plan_id for plan_id in decision["target_plan_ids"]
                                           if plan_id not in self._attempted]
            if not decision["target_plan_ids"]:
                return decision, None
            policy = decision["policy"]
            rule_id, version = parse_rule_reference(policy["rule_id"])
            return decision, {
                "target_plan_ids": decision["target_plan_ids"],
                "metric_artifact_ids": metric_ids,
                "verified_rule": {"rule_id": rule_id, "version": version, "chunk_id": policy["chunk_id"],
                                  "artifact_id": rule_artifact},
            }

    def _window(self, receipts, plan_id, window):
        args = {"plan_id": plan_id, **window.model_dump()}
        matches = [row for row in receipts if row["source"] == METRICS and row.get("arguments") == args]
        valid = [row for row in matches if row["status"] == "succeeded"]
        failed = next((row for row in matches if row.get("error_code") in
                       PERMANENT_READ_GAPS | {"invalid_metrics_receipt"}), None)
        row = failed or next(iter(valid or matches), None)
        facts = {**args, "artifact_ids": [item["artifact_id"] for item in matches]}
        if not row:
            return {**facts, "gap": "review_metrics_pending"}, None
        result = row["result"] or {}
        if row["status"] == "failed":
            return {**facts, "gap": row["error_code"], "coverage": result.get("coverage")}, None
        if any(any(item["result"][key] != result[key] for key in _METRIC_KEYS) for item in valid[1:]):
            return {**facts, "gap": "review_metrics_conflict"}, None
        return ({**facts, **{key: result[key] for key in (*_METRIC_KEYS, "coverage", "ctr_pct")}, "gap": None},
                row["artifact_id"])

    def _rule(self, receipts):
        policy, adopted, artifacts, conflict = None, None, [], False
        for row in receipts:
            if row["source"] != RULE or self.rule_error(row["arguments"]["query"]):
                continue
            artifacts.append(row["artifact_id"])
            if row["status"] != "succeeded":
                conflict |= row["error_code"] == "review_rule_conflict"
                continue
            scope = rule_query_scope(canonical_rule_query(row["arguments"]["query"]))
            for hit in row["result"]["evidence"]:
                if hit.get("rule_id") not in scope[0] or hit.get("version") != scope[1]:
                    conflict = True
                elif hit["rule_id"] == _SECTION:
                    verified = verified_ctr_policy(self.goal.rule, [{**hit, "content": hit["excerpt"]}])
                    if hit["truncated"] or not verified or (policy and policy != verified):
                        conflict = True
                    elif policy is None:
                        policy = verified
                        adopted = row["artifact_id"]
        return policy, adopted, list(dict.fromkeys(artifacts)), conflict

    def has_verified_rule(self, task_id, worker_id):
        receipts = [row for row in self._board.tool_receipts(RULE)
                    if row.get("task_id") == task_id and row.get("worker_id") == worker_id]
        policy, _, _, conflict = self._rule(receipts)
        return policy is not None and not conflict

    def decision(self):
        return self._evaluate()[0]

    def _evaluate(self, receipts=None):
        if receipts is None:
            receipts = self._board.tool_receipts(METRICS, RULE)
        policy, rule_artifact, rule_artifacts, conflict = self._rule(receipts)
        rule_gap = "review_rule_conflict" if conflict else None if policy else "review_rule_missing"
        rows, targets, metric_artifacts = [], [], []
        for plan_id in self.goal.plan_ids:
            (base, base_artifact), (current, current_artifact) = (
                self._window(receipts, plan_id, window) for window in self.goal.windows)
            metric_artifacts.extend((base_artifact, current_artifact))
            gap = base["gap"] or current["gap"]
            comparison = None
            if not gap:
                if any(base[key] != current[key] for key in _METRIC_KEYS[:3]):
                    gap = "review_source_mismatch"
                elif not base["impressions"] or not current["impressions"] or not base["clicks"]:
                    gap = "review_comparison_undefined"
                else:
                    comparison, relative = calculate_ctr(base, current)
                    if not rule_gap and min(base["impressions"], current["impressions"]) >= policy["min_impressions"]:
                        if relative >= policy["min_relative_decline_pct"]:
                            targets.append(plan_id)
            rows.append({"plan_id": plan_id, "windows": [base, current], "comparison": comparison, "gap": gap})
        windows = [window for row in rows for window in row["windows"]]
        versions = {(window["source_id"], window["data_version"]) for window in windows if not window["gap"]}
        gap = rule_gap if conflict else next((row["gap"] for row in rows if row["gap"]), rule_gap)
        if not gap and len(versions) != 1:
            gap = "review_source_mismatch"
        if not gap and self.goal.needs_alert and not self._critical_alerts:
            gap = "review_alert_not_confirmed"
        decision = {**self.goal.trace_fields(), "rows": rows, "target_plan_ids": [] if gap else targets,
                    "error_code": gap, "rule_gap": rule_gap, "policy": None if conflict else policy,
                    "critical_ctr_alerts": self._critical_alerts,
                    "evidence_artifact_ids": [item for window in windows for item in window["artifact_ids"]] + rule_artifacts}
        return decision, metric_artifacts, rule_artifact

    def finish(self, trace_id, *, json_only, evidence_sink=None):
        receipts = self._board.tool_receipts(METRICS, RULE)
        facts, metric_ids, rule_id = self._evaluate(receipts)
        details, detail_receipts = [], []
        for row in self._board.tool_receipts(DETAIL):
            if (row["status"] == "succeeded" and row["arguments"]["plan_id"] in facts["target_plan_ids"]
                    and self._confirmed_detail(row)):
                details.append({key: row["result"][key] for key in ("id", *_FIELDS) if key in row["result"]})
                detail_receipts.append(row)
        pending = bool(facts["error_code"] or len(details) != len(facts["target_plan_ids"]))
        failed = (self._attempted.intersection(facts["target_plan_ids"]) - {row["id"] for row in details})
        if failed:
            facts["detail_failed_plan_ids"] = sorted(failed)
        if self.goal.independent:
            facts["independent_review_status"] = self.handoff_status()["status"]
            pending |= facts["independent_review_status"] != "completed"
        add_trace_step(trace_id, "conditional_detail", "reply", role="orchestrator",
                       evidence={**facts, "detail_artifact_ids": [row["artifact_id"] for row in detail_receipts],
                                 "attempted_plan_ids": sorted(self._attempted)})
        if evidence_sink:
            rule_receipt = next((row for row in receipts if row["artifact_id"] == rule_id), None)
            evidence_sink(self._reply_evidence(facts, metric_ids, rule_receipt, details, detail_receipts))
        if json_only:
            return json.dumps({**facts, "details": details, "list_pending": pending}, ensure_ascii=False)
        return self._markdown(facts, details, pending)

    def _reply_evidence(self, facts, metric_ids, rule_receipt, details, detail_receipts):
        metrics = []
        for window, artifact_id in zip((window for row in facts["rows"] for window in row["windows"]), metric_ids):
            metrics.append({**{key: value for key, value in window.items() if key != "artifact_ids"},
                            "artifact_id": artifact_id, "gap_text": _GAPS.get(window["gap"])})
        rule = None
        if policy := facts["policy"]:
            hit = next(hit for hit in rule_receipt["result"]["evidence"] if hit["chunk_id"] == policy["chunk_id"])
            rule = {**hit, "artifact_id": rule_receipt["artifact_id"], "runtime_call_id": rule_receipt["tool_call_id"]}
        admitted = []
        for detail, receipt in zip(details, detail_receipts):
            _, proof = self._admissions.get(detail["id"], (None, None))
            admitted.append({**detail, "artifact_id": receipt["artifact_id"],
                             "runtime_call_id": receipt["tool_call_id"], "review_input": proof})
        missing_details = sorted(set(facts["target_plan_ids"]) - {row["id"] for row in details})
        return {"scope": self.goal.trace_fields(), "metrics": metrics, "rule": rule, "details": admitted,
                "gap": facts["error_code"], "gap_text": _GAPS.get(facts["error_code"]),
                "rule_gap": facts["rule_gap"], "rule_gap_text": _GAPS.get(facts["rule_gap"]),
                "missing_detail_plan_ids": missing_details, "failed_detail_plan_ids": facts.get("detail_failed_plan_ids", []),
                "review_status": facts.get("independent_review_status")}

    @staticmethod
    def _markdown(facts, details, pending):
        lines = []
        if facts["critical_ctr_alerts"] is not None:
            lines.append(f"已读取未读告警，其中严重 CTR 告警 {facts['critical_ctr_alerts']} 条。")
        for row in facts["rows"]:
            lines.append(f"\n**计划 #{row['plan_id']}**")
            for label, window in zip(("基准窗", "当前窗"), row["windows"]):
                line = f"- {label} {window['start_date']} 至 {window['end_date']}"
                if coverage := window.get("coverage"):
                    line += f"，覆盖 {coverage['observed_days']}/{coverage['expected_days']} 天"
                    if coverage["missing_dates"]:
                        line += "，缺报 " + "、".join(coverage["missing_dates"])
                if window["gap"]:
                    line += "，" + _GAPS.get(window["gap"], "本轮未取得可核验的指标")
                else:
                    ctr = "无定义" if window["ctr_pct"] is None else f"{window['ctr_pct']:.2f}%"
                    line += f"，曝光 {window['impressions']:,}，点击 {window['clicks']:,}，CTR {ctr}"
                lines.append(line + "。")
            if row["comparison"]:
                lines.append(f"- CTR 相对降幅 {row['comparison']['relative_decline_pct']:.2f}%。")
        if policy := facts["policy"]:
            lines.append(f"\n**规则 {policy['chunk_id']}**　{policy['original']}")
        elif facts["rule_gap"]:
            lines.append(f"\n指定规则 {facts['rule']}，{_GAPS[facts['rule_gap']]}。")
        if pending:
            reason = _GAPS.get(facts["error_code"], "本轮尚未取得完整的入选计划详情")
            if facts.get("detail_failed_plan_ids"):
                reason = "入选计划详情读取失败，本轮尚未取得完整详情"
            lines.append(f"\n{reason}，暂缓出具复核清单。")
        elif not details:
            lines.append("\n本轮候选均未达到复核门槛，复核清单为空。")
        if details:
            lines.append("\n**已核验的入选计划详情**")
            for detail in details:
                fields = [f"{label} {detail[key]}" for key, label in _FIELDS.items() if key in detail]
                lines.append(f"- #{detail['id']}，" + "，".join(fields) + "。")
        if status := facts.get("independent_review_status"):
            if status == "completed":
                lines.append("\n独立复核已完成。")
            else:
                reason = {
                    "review_required": "仍需复核 Worker 接收证据并完成获准的详情查询",
                    "worker_evidence_pending": "尚缺可交接给独立复核 Worker 的本轮取证产物",
                    "evidence_pending": "本轮核验有缺项，详情暂缓",
                    "review_incomplete": "复核 Worker 尚未交回完整的获准详情",
                }[status]
                lines.append(f"\n独立复核未完成，{reason}。")
        return "\n".join(lines)
