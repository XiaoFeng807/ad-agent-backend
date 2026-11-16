"""Discover and freeze the scope of a weekly critical CTR alert review."""

import json
import re
from contextlib import closing
from datetime import datetime, timedelta

from backend.agent.diagnosis import CTR_REVIEW_RULE as REVIEW_RULE, DateWindow
from backend.agent.multi_window_review import MultiWindowReview, ReviewGoal
from backend.config.config import settings
from backend.database import alert_review, database
from backend.observability import add_trace_step
from backend.tools.plan_metrics import common_metric_dates, latest_comparable_dates


WEEKLY_ALERTS = "get_weekly_critical_ctr_alerts"
ALERT_TOOL = {"type": "function", "function": {
    "name": WEEKLY_ALERTS,
    "description": "读取当前工作群用户本周严重 CTR 告警，核验计划关联并返回本轮候选、比较窗口、规则和缺项。",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}
_GAPS = {"alert_plan_missing": "缺少结构化计划关联", "alert_plan_unverified": "计划关联无法核验",
         "alert_history_changed": "计划历史绑定已变化"}


def weekly_review_requested(message):
    return bool(re.fullmatch(r"(?:请\s*)?复核\s*(?:这周|本周)的?\s*严重\s*(?:CTR|点击率)\s*告警[。！!\s]*",
                             message.strip(), re.I))


class AlertReview(MultiWindowReview):
    def __init__(self, requested_at, board, user_id, conversation_id):
        super().__init__(None, board, user_id, conversation_id)
        self.period = alert_review.week_period(requested_at)
        self.receipt = None
        self.bindings = {}
        self._finishing_receipt = None

    def model_basis(self):
        return {"alert_scope_ready": self.scope_error() is None}

    def prompt(self):
        return ("先调用 get_weekly_critical_ctr_alerts。计划、指标比较窗口和规则均以本轮工具回执为准。"
                "回执含缺项时，结束并报告具体缺项，暂缓复核清单。"
                "范围完整时用 delegate_tasks 分派取证和独立复核。你可选择同批分派或先取证再追加。"
                "第一份任务单须包括每条候选两窗指标的取证任务，并另列一个仅使用 search_knowledge 的规则取证任务，"
                "查询回执中的规则引用。指标任务只查指标。"
                "同批复核任务只使用 get_plan_detail，依赖任务单中的全部指标和规则取证任务。"
                "分批时，收到 review_required 后将全部 artifact_refs 交给复核任务。"
                "目标写明回执中的计划号、两窗日期或规则。由复核 Worker 根据回执决定入选计划。")

    def discover(self, call_id):
        if self.receipt is not None:
            return self.current_receipt()
        associations, gaps, scope_gaps = [], [], []
        if settings.DIAGNOSIS_MYSQL_ENABLED:
            scope_gaps.append("本周告警复核需要 SQLite 工作群。")
        else:
            associations, gaps, self.bindings = alert_review.read_weekly_alerts(self._user_id, self.period)
        if associations and not gaps:
            for plan, binding in self.bindings.items():
                if binding["source_plan_id"] is None:
                    scope_gaps.append(f"计划 #{plan} 缺少历史指标绑定。")
            if not scope_gaps:
                through = datetime.fromisoformat(self.period["end"]).date() - timedelta(days=1)
                days, error = common_metric_dates(self.bindings.values(), through.isoformat())
                windows = latest_comparable_dates(days, preferred_days=7)
                if error:
                    scope_gaps.append(f"无法核验候选的共同指标覆盖（{error}）。")
                elif not windows:
                    scope_gaps.append("候选共同指标覆盖不足，无法选取两个等长连续窗口。")
                else:
                    self.goal = ReviewGoal(tuple(self.bindings), *(
                        DateWindow(start_date=start.isoformat(), end_date=end.isoformat()) for start, end in windows
                    ), REVIEW_RULE, needs_alert=False, independent=True)
        self.receipt = {
            "runtime_call_id": call_id, "alert_period": self.period, "associations": associations,
            "association_gaps": gaps, "scope_gaps": scope_gaps, "candidate_plan_ids": list(self.bindings),
            "status": "ready" if self.goal else "incomplete" if gaps or scope_gaps else "no_alerts",
            "review_scope": self.goal.trace_fields() if self.goal else None,
        }
        return self.receipt

    def current_receipt(self, conn=None):
        receipt = self.receipt
        if receipt is None:
            return {"alert_period": self.period, "associations": [], "association_gaps": [],
                    "scope_gaps": ["尚未取得本周严重 CTR 告警回执。"], "candidate_plan_ids": [],
                    "status": "incomplete", "review_scope": None}
        if not receipt["associations"]:
            return receipt
        if conn is None:
            with closing(database.get_db(read_only=True)) as conn:
                return self.current_receipt(conn)
        return recheck_receipt(conn, self._user_id, receipt, self.bindings)

    def scope_error(self):
        if not self.goal or (self._finishing_receipt or self.current_receipt())["status"] != "ready":
            return "alert_review_scope_incomplete"

    def metric_error(self, args, result):
        if error := self.scope_error() or super().metric_error(args, result):
            return error
        if result is not None and any(result.get(key) != value for key, value in self.bindings[args["plan_id"]].items()):
            return "alert_history_changed"

    def rule_error(self, query, result=None):
        return self.scope_error() or super().rule_error(query, result)

    def _evaluate(self, receipts=None):
        if error := self.scope_error():
            return {"error_code": error, "target_plan_ids": []}, [], None
        return super()._evaluate(receipts)

    def handoff_status(self):
        decision = self.decision()
        if not decision["error_code"] and not decision["target_plan_ids"]:
            return {"status": "completed"}
        return super().handoff_status()

    def finish(self, trace_id, *, json_only, evidence_sink=None):
        alert = self.current_receipt()
        if alert["status"] == "ready":
            def capture(evidence):
                if evidence_sink:
                    evidence_sink({**evidence, "alert_review": alert})

            self._finishing_receipt = alert
            try:
                answer = super().finish(trace_id, json_only=json_only, evidence_sink=capture)
            finally:
                self._finishing_receipt = None
            if json_only:
                return json.dumps({**json.loads(answer), "alert_review": alert}, ensure_ascii=False)
            return alert_summary(alert) + "\n\n" + answer
        return finish_without_scope(alert, trace_id, json_only=json_only, evidence_sink=evidence_sink)


def recheck_receipt(conn, user_id, receipt, bindings=None):
    associations, gaps = alert_review.current_associations(
        conn, user_id, receipt["alert_period"], receipt["associations"], bindings,
    )
    if not gaps:
        return receipt
    return {**receipt, "associations": associations, "association_gaps": receipt["association_gaps"] + gaps,
            "scope_gaps": [], "candidate_plan_ids": list(dict.fromkeys(row["plan_id"] for row in associations)),
            "status": "incomplete", "review_scope": None}


def alert_summary(receipt):
    period = receipt["alert_period"]
    lines = [f"**告警筛选时段（{period['timezone']}）**　{period['start']} 至 {period['end']}"]
    lines.extend(f"- 告警 #{item['alert_id']} → 计划 #{item['plan_id']}"
                 for item in receipt["associations"])
    lines.extend(f"- 告警 #{item['alert_id']}，{_GAPS[item['error_code']]}。"
                 for item in receipt["association_gaps"])
    lines.extend(f"- {gap}" for gap in receipt["scope_gaps"])
    return "\n".join(lines)


def finish_without_scope(alert, trace_id, *, json_only=False, evidence_sink=None):
    empty = alert["status"] == "no_alerts"
    gap = None if empty else "告警复核范围存在缺项"
    evidence = {"scope": {"rule": REVIEW_RULE}, "metrics": [], "rule": None, "details": [],
                "gap": None if empty else "alert_review_scope_incomplete", "gap_text": gap,
                "rule_gap": None, "rule_gap_text": None, "missing_detail_plan_ids": [],
                "failed_detail_plan_ids": [], "review_status": None, "alert_review": alert}
    add_trace_step(trace_id, "alert_review", "reply", role="orchestrator", evidence=evidence)
    if evidence_sink:
        evidence_sink(evidence)
    if json_only:
        return json.dumps({**evidence, "list_pending": not empty}, ensure_ascii=False)
    return alert_summary(alert) + ("\n\n本周截至请求时刻无严重 CTR 告警。" if empty else "\n\n暂缓出具复核清单。")
