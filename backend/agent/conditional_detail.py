"""Admission and reply facts for a two-plan, one-window CTR condition."""

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from operator import eq, ge, gt, le, lt
from threading import Lock

from pydantic import ValidationError

from backend.agent.team_tools import MetricsArgs
from backend.observability import add_trace_step


METRICS = "get_plan_metrics_by_date"
DETAIL = "get_plan_detail"
CONDITIONAL_READS = frozenset({METRICS, DETAIL})
_CTR = r"(?:\bCTR\b|点击率)"
_DETAIL_WORDS = {"plan_name": "名称|名字", "status": "状态", "daily_budget": "日预算|每日预算"}
_DETAIL_INTENT = r"详情|明细|名称|名字|状态|预算|编号"
_CARDINALITY = r"(?:恰好|正好|刚好|只有|仅有)\s*(?:一|1)\s*(?:条|个)"
_OPERATORS = {
    "达到": (">=", ge), "至少": (">=", ge), "不低于": (">=", ge), "大于等于": (">=", ge),
    ">=": (">=", ge), "≥": (">=", ge), "超过": (">", gt), "高于": (">", gt),
    "大于": (">", gt), ">": (">", gt), "低于": ("<", lt), "小于": ("<", lt), "<": ("<", lt),
    "不超过": ("<=", le), "不高于": ("<=", le), "至多": ("<=", le),
    "小于等于": ("<=", le), "<=": ("<=", le), "≤": ("<=", le), "等于": ("=", eq), "=": ("=", eq),
}
_COMPARATORS = "|".join(re.escape(word) for word in sorted(_OPERATORS, key=len, reverse=True))
_CLAUSE = re.compile(
    rf"(?:仅在|仅当|只在|只有在|如果|若)\s*(?:(?:能|可以)\s*)?(?:确认\s*)?{_CARDINALITY}"
    rf"\s*(?:广告)?计划(?:的)?\s*{_CTR}\s*(?P<op>{_COMPARATORS})\s*"
    r"(?P<threshold>[0-9]{1,3}(?:\.[0-9]{1,6})?)\s*[%％]\s*"
    r"(?:时\s*)?[，,]?\s*(?:才|再|就)\s*(?P<action>[^。！？；\n]+)",
    re.I,
)
_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_WINDOW = re.compile(rf"({_DATE})\s*(?:至|到|~|～|—|–)\s*({_DATE})")
_GAPS = {
    "conditional_metrics_pending": "本轮尚无符合日期窗的已核验指标",
    "conditional_metrics_conflict": "本轮指标回执不一致",
    "conditional_source_mismatch": "两条计划的指标来源或版本不一致",
    "conditional_zero_impressions": "零曝光，CTR 无法计算",
    "plan_history_not_mapped": "历史数据缺项，未绑定历史来源",
    "plan_history_missing_dates": "历史数据缺项",
    "plan_not_found": "未找到可读取的计划",
    "source_version_mismatch": "历史来源版本不匹配",
    "plan_history_not_configured": "未配置历史来源",
}


@dataclass(frozen=True)
class DetailCondition:
    plan_ids: tuple[int, int]
    start_date: str
    end_date: str
    operator: str
    threshold: Decimal
    fields: tuple[str, ...]

    def trace_fields(self):
        return {"plan_ids": self.plan_ids, "start_date": self.start_date, "end_date": self.end_date,
                "operator": self.operator, "threshold_pct": str(self.threshold)}


def parse_detail_condition(request):
    """Recognize the conditional intent separately from the supported request grammar."""
    if not (re.search(_CTR, request, re.I) and re.search(_DETAIL_INTENT, request)
            and re.search(r"恰好|正好|刚好|唯一|只有|仅有|仅在|仅当|只在|才\s*(?:去|能)?\s*"
                          r"(?:查|读|看|给|告|返回)|(?:如果|若).{0,80}(?:再|就|方可)", request)):
        return False, None
    clauses = list(_CLAUSE.finditer(request))
    ids = re.findall(r"#([0-9]+)", request)
    dates = re.findall(_DATE, request)
    windows = _WINDOW.findall(request)
    if len(clauses) != 1 or len(ids) != 2 or len(set(ids)) != 2 or len(dates) != 2 or len(windows) != 1:
        return True, None
    clause = clauses[0]
    action = clause["action"]
    if (not re.search(_DETAIL_INTENT, action) or not re.search(r"查|读|看|给|告|返回|列|显示|提供", action)
            or re.search(r"如果|除非|或者|否则|并且|同时满足|无需|其他|另一|两条|所有|不(?:查|看|读|给|告|返回)",
                         action)):
        return True, None
    if (len(re.findall(r"[%％]", request)) != 1
            or re.search(r"(?:不是|并非|取消)\s*$", request[:clause.start()])
            or re.search(r"另外|除非|或是|或者|并且|无需|不(?:查|看|读|给|告|返回)", request)):
        return True, None
    threshold = Decimal(clause["threshold"])
    if threshold > 100 or any(len(plan_id) > 19 for plan_id in ids):
        return True, None
    start, end = windows[0]
    plan_ids = tuple(map(int, ids))
    try:
        for plan_id in plan_ids:
            MetricsArgs(plan_id=plan_id, start_date=start, end_date=end)
    except ValidationError:
        return True, None
    if plan_ids[0] == plan_ids[1]:
        return True, None
    fields = tuple(key for key, words in _DETAIL_WORDS.items() if re.search(words, action))
    if not fields and re.search(r"详情|明细", action):
        fields = tuple(_DETAIL_WORDS)
    return True, DetailCondition(plan_ids, start, end, clause["op"], threshold, fields)


class DetailAdmission:
    tool_names = CONDITIONAL_READS
    evidence_sources = frozenset({METRICS})
    reuse_detail_receipt = True

    def __init__(self, goal, board, user_id):
        self.goal = goal
        self._board = board
        self._user_id = user_id
        self._lock = Lock()
        self._attempted = set()

    def model_basis(self):
        return self.decision()

    def rule_error(self, query, result=None):
        return None

    def target_error(self, plan_id, basis, decision):
        if basis and basis["target_plan_id"] != decision["target_plan_id"]:
            return "conditional_evidence_changed"
        if plan_id != decision["target_plan_id"]:
            return "conditional_wrong_target"

    def admit_detail(self, plan_id, basis, reserve, *, worker_id=None, runtime_call_id=None):
        with self._lock:
            decision = basis if basis and basis["error_code"] else self.decision()
            error = decision["error_code"] or self.target_error(plan_id, basis, decision)
            if not error and plan_id in self._attempted:
                error = "conditional_detail_already_attempted"
            if not error:
                if reserve("tool"):
                    self._attempted.add(plan_id)
                else:
                    error = "budget_exhausted_or_cancelled"
            return {**decision, "error_code": error, "requested_plan_id": plan_id, "executed": error is None}


class ConditionalDetail(DetailAdmission):

    def prompt(self):
        return ("本轮详情受服务端条件约束。先获取以下两个同窗历史指标，再选择直查或委派；"
                "两条均可判定且恰好一条达标时，才读取该计划详情。"
                "委派详情时引用本轮已核验的指标产物。条件及详情按实际回执输出。\n"
                + json.dumps(self.goal.trace_fields(), ensure_ascii=False))

    def metric_error(self, args, result):
        if args["plan_id"] not in self.goal.plan_ids or (args["start_date"], args["end_date"]) != (
                self.goal.start_date, self.goal.end_date):
            return "conditional_metric_out_of_scope"
        if result is not None and (type(result.get("user_id")) is not int or result["user_id"] != self._user_id):
            return "invalid_metrics_receipt"
        return None

    def decision(self):
        rows = []
        receipts = self._board.tool_receipts(METRICS)
        operator, compare = _OPERATORS[self.goal.operator]
        for plan_id in self.goal.plan_ids:
            args = {"plan_id": plan_id, "start_date": self.goal.start_date, "end_date": self.goal.end_date}
            matches = [row for row in receipts if row.get("arguments") == args]
            valid = [row for row in matches if row["status"] == "succeeded"]
            receipt = (valid or matches or [None])[0]
            facts = {"plan_id": plan_id, "artifact_id": receipt["artifact_id"] if receipt else None,
                     "ctr_pct": None, "qualifies": None, "missing_dates": []}
            rows.append(facts)
            if not valid:
                facts["gap"] = receipt["error_code"] if receipt else "conditional_metrics_pending"
                if receipt and receipt["result"]:
                    facts["missing_dates"] = receipt["result"]["coverage"]["missing_dates"]
                continue
            result = receipt["result"]
            keys = ("source_id", "data_version", "source_plan_id", "impressions", "clicks")
            if any(any(row["result"][key] != result[key] for key in keys) for row in valid[1:]):
                facts["gap"] = "conditional_metrics_conflict"
                continue
            facts.update({key: result[key] for key in keys})
            if result["impressions"] == 0:
                facts["gap"] = "conditional_zero_impressions"
                continue
            ctr = Fraction(100 * result["clicks"], result["impressions"])
            facts.update(ctr_pct=float(ctr), qualifies=compare(ctr, self.goal.threshold), gap=None)
        gaps = {row["gap"] for row in rows if row["gap"]}
        targets = [row["plan_id"] for row in rows if row["qualifies"]]
        target = None
        if gaps - {"conditional_metrics_pending", "conditional_zero_impressions"}:
            code = "conditional_missing_metrics"
        elif "conditional_metrics_pending" in gaps:
            code = "conditional_metrics_pending"
        elif gaps:
            code = "conditional_zero_impressions"
        elif (rows[0]["source_id"], rows[0]["data_version"]) != (rows[1]["source_id"], rows[1]["data_version"]):
            code = "conditional_source_mismatch"
        elif len(targets) != 1:
            code = "conditional_no_match" if not targets else "conditional_multiple_matches"
        else:
            code, target = None, targets[0]
        return {**self.goal.trace_fields(), "operator": operator, "rows": rows, "error_code": code,
                "target_plan_id": target, "evidence_artifact_ids": [row["artifact_id"] for row in rows
                                                                  if row["artifact_id"]]}

    def finish(self, trace_id, *, json_only):
        facts = self.decision()
        target = facts["target_plan_id"]
        detail = None
        artifact_id = None
        for receipt in self._board.tool_receipts(DETAIL):
            if receipt["status"] == "succeeded" and receipt["arguments"]["plan_id"] == target:
                detail = {key: receipt["result"][key] for key in self.goal.fields if key in receipt["result"]}
                detail["id"] = target
                artifact_id = receipt["artifact_id"]
        add_trace_step(trace_id, "conditional_detail", "reply", role="orchestrator",
                       evidence={**facts, "detail_artifact_id": artifact_id, "executed": bool(self._attempted)})
        if json_only:
            return json.dumps({**facts, "detail": detail}, ensure_ascii=False, allow_nan=False)
        return self._markdown(facts, detail)

    def _markdown(self, facts, detail):
        lines = [f"日期窗 {self.goal.start_date} 至 {self.goal.end_date}，"
                 f"CTR 条件为 {facts['operator']} {self.goal.threshold}%。", ""]
        for row in facts["rows"]:
            if row["gap"]:
                text = _GAPS.get(row["gap"], "本轮未取得可核验的历史指标")
                if row["missing_dates"]:
                    text += "（" + "、".join(row["missing_dates"]) + "）"
                lines.append(f"- #{row['plan_id']}：{text}。")
            else:
                lines.append(f"- #{row['plan_id']}：CTR **{row['ctr_pct']:.2f}%**"
                             f"（{row['clicks']:,} 次点击 / {row['impressions']:,} 次曝光）。")
        messages = {
            "conditional_no_match": "两条计划均未达标，条件详情未读取。",
            "conditional_multiple_matches": "两条计划均达标，条件详情未读取。",
            "conditional_source_mismatch": "两条指标的来源版本不一致，暂缓唯一性判断和详情读取。",
        }
        lines.append("")
        if code := facts["error_code"]:
            lines.append(messages.get(code, "目前无法确认恰好一条计划达标，暂缓比较、唯一性判断和详情读取。"))
        else:
            lines.append(f"仅 #{facts['target_plan_id']} 达标。")
            if detail is None:
                lines.append("本轮尚未取得已核验的计划详情。")
            else:
                for key, label in (("plan_name", "名称"), ("status", "当前状态"), ("daily_budget", "日预算")):
                    if key in detail:
                        lines.append(f"- {label}：{detail[key]}。")
        return "\n".join(lines)
