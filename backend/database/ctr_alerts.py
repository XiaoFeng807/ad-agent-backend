"""Manual plan CTR checks and their owner-scoped SQLite alert writes."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json

from backend.agent.diagnosis import (
    CTR_REVIEW_RULE, calculate_ctr, ctr_policy_thresholds, valid_plan_metrics,
)
from backend.database import alert_review, database
from backend.tools.plan_metrics import (
    common_metric_dates, latest_comparable_dates, metric_source_version, query_plan_metrics,
)


BINDING_FIELDS = ("source_id", "data_version", "source_plan_id")


class CheckRejected(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _owned_binding(conn, plan_id, token, validate_token):
    session = validate_token(token, connection=conn)
    if session is None or conn.execute("SELECT 1 FROM users WHERE id=?", (session["user_id"],)).fetchone() is None:
        raise CheckRejected(401, "Token 无效或已过期，请重新登录")
    user_id = session["user_id"]
    row = conn.execute(
        """SELECT b.source_id, b.data_version, b.source_plan_id FROM ad_plans p
           LEFT JOIN plan_metric_bindings b ON b.plan_id=p.id WHERE p.id=? AND p.user_id=?""",
        (plan_id, user_id),
    ).fetchone()
    if row is None:
        raise CheckRejected(404, "计划不存在")
    return user_id, dict(row)


def _measure(plan_id, user_id, binding, through, evidence):
    if binding["source_plan_id"] is None:
        return "plan_history_not_mapped"
    days, error = common_metric_dates([binding], through)
    if error:
        return error
    windows = latest_comparable_dates(days, preferred_days=7)
    if not windows:
        return "plan_history_insufficient_windows"
    for label, (start, end) in zip(("baseline", "current"), windows, strict=True):
        args = {"plan_id": plan_id, "start_date": start.isoformat(), "end_date": end.isoformat()}
        result = query_plan_metrics(**args, user_id=user_id, connect=lambda: database.get_db(read_only=True))
        if not result["success"]:
            return result["error_code"]
        if any(result[key] != binding[key] for key in BINDING_FIELDS):
            return "plan_history_changed"
        if not valid_plan_metrics(result, args) or not result["coverage"]["complete"]:
            return "plan_history_incomplete"
        window = {key: result[key] for key in ("start_date", "end_date", "coverage", "impressions", "clicks")}
        window["ctr_pct"] = 100 * window["clicks"] / window["impressions"] if window["impressions"] else None
        evidence[label] = window
    baseline, current = evidence["baseline"], evidence["current"]
    if min(baseline["impressions"], current["impressions"]) < evidence["policy"]["min_impressions"]:
        return "insufficient_impressions"
    comparison, relative = calculate_ctr(baseline, current)
    evidence["comparison"] = comparison
    if relative is None:
        return "undefined_relative_decline"
    if relative < evidence["policy"]["min_relative_decline_pct"]:
        return "below_threshold"


def _save(conn, user_id, plan_id, evidence):
    key = json.dumps({
        "binding": evidence["binding"], "rule": evidence["rule"],
        **{label: [evidence[label]["start_date"], evidence[label]["end_date"]] for label in ("baseline", "current")},
    }, sort_keys=True, separators=(",", ":"))
    inserted = conn.execute(
        """INSERT INTO alerts(type, message, level, user_id, plan_id, created_at, ctr_check_key, ctr_check_evidence)
           VALUES ('ctr_review', ?, 'critical', ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, plan_id, ctr_check_key) DO NOTHING""",
        (f"计划 #{plan_id} 的历史 CTR 相对下降达到严重告警阈值。", user_id, plan_id,
         datetime.fromisoformat(evidence["detected_at"]).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
         key, json.dumps(evidence, ensure_ascii=False)),
    ).rowcount
    row = conn.execute(
        "SELECT id, ctr_check_evidence FROM alerts WHERE user_id=? AND plan_id=? AND ctr_check_key=?",
        (user_id, plan_id, key),
    ).fetchone()
    return {**json.loads(row["ctr_check_evidence"]), "alert_id": row["id"],
            "status": "created" if inserted else "existing"}


def check_ctr_alert(plan_id, token, validate_token):
    with closing(database.get_db(read_only=True)) as conn, conn:
        conn.execute("BEGIN")
        user_id, binding = _owned_binding(conn, plan_id, token, validate_token)
    now = alert_review.now()
    through = (datetime.fromisoformat(alert_review.week_period(now)["end"]).date() - timedelta(days=1)).isoformat()
    evidence = {"plan_id": plan_id, "detected_at": now.astimezone(timezone.utc).isoformat(),
                "binding": binding, "rule": CTR_REVIEW_RULE, "policy": ctr_policy_thresholds(CTR_REVIEW_RULE)}
    reason = _measure(plan_id, user_id, binding, through, evidence)
    with closing(database.get_db(read_only=reason is not None)) as conn, conn:
        conn.execute("BEGIN" if reason else "BEGIN IMMEDIATE")
        _, current_binding = _owned_binding(conn, plan_id, token, validate_token)
        if current_binding != binding or reason in ("plan_not_found", "plan_history_changed"):
            raise CheckRejected(409, "计划历史绑定已变化，请重新检查")
        if reason:
            status = "below_threshold" if reason == "below_threshold" else "insufficient_evidence"
            return {**evidence, "status": status, "reason": reason, "alert_id": None}
        if metric_source_version() != (binding["source_id"], binding["data_version"]):
            raise CheckRejected(409, "计划历史来源版本已变化，请重新检查")
        return _save(conn, user_id, plan_id, evidence)
