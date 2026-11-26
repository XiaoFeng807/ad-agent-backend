"""Current account access for saved cross-window review replies."""

import json
import re
import sqlite3

from backend.database import database, mysql_diagnosis


UNAVAILABLE = "当前无法查看该轮广告复核结果。"
_RECEIPT_FIELDS = {"metrics", "details", "rule", "missing_detail_plan_ids", "failed_detail_plan_ids"}
_LEGACY_JSON_FIELDS = {"rows", "target_plan_ids", "evidence_artifact_ids", "list_pending"}
_PLAN_HEADING = re.compile(r"(?m)^\*\*计划 #[0-9]+\*\*$")


def _legacy_review(content):
    if not content:
        return False
    if (_PLAN_HEADING.search(content) and "\n- 基准窗 " in content and "\n- 当前窗 " in content
            and ("ctr-review-" in content or "**已核验的入选计划详情**" in content)):
        return True
    if content.startswith("**告警筛选时段（") and ("复核清单" in content or "无严重 CTR 告警。" in content):
        return True
    if not content.lstrip().startswith("{"):
        return False
    try:
        value = json.loads(content)
    except ValueError:
        return False
    return isinstance(value, dict) and (_LEGACY_JSON_FIELDS <= value.keys()
                                        or "alert_review" in value and _RECEIPT_FIELDS <= value.keys())


def _valid_plan(plan_id):
    return type(plan_id) is int and 0 < plan_id <= 2**63 - 1


def _plan_ids(values):
    if isinstance(values, (list, tuple)) and all(_valid_plan(value) for value in values):
        return set(values)
    return None


def _references(rows, key):
    if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
        return _plan_ids([row.get(key) for row in rows])
    return None


def _review_plans(receipt):
    """Resolve the whole comparison, including non-selected and alert candidates."""
    scope = receipt.get("scope")
    if not isinstance(scope, dict):
        return None
    alert = receipt.get("alert_review")
    candidates = _plan_ids(scope.get("plan_ids"))
    if alert is not None:
        if not isinstance(alert, dict):
            return None
        associated = _references(alert.get("associations"), "plan_id")
        if associated is None or associated != _plan_ids(alert.get("candidate_plan_ids")):
            return None
        if alert.get("status") in ("incomplete", "no_alerts"):
            if (alert.get("review_scope") is None and "plan_ids" not in scope
                    and all(receipt.get(key) == [] for key in _RECEIPT_FIELDS - {"rule"})):
                return associated
            return None
        if alert.get("status") != "ready" or alert.get("review_scope") != scope or candidates != associated:
            return None
    if not candidates or _references(receipt.get("metrics"), "plan_id") != candidates:
        return None
    for ids in (_references(receipt.get("details"), "id"), _plan_ids(receipt.get("missing_detail_plan_ids")),
                _plan_ids(receipt.get("failed_detail_plan_ids"))):
        if ids is None or not ids <= candidates:
            return None
    if scope.get("needs_alert"):
        alerts = _plan_ids(receipt.get("alert_plan_ids"))
        if alerts is None:
            return None
        candidates |= alerts
    return candidates


def visible(conn, user_id, content, receipt=None, *, bound=False):
    if not (bound or (isinstance(receipt, dict) and ("alert_review" in receipt
            or _RECEIPT_FIELDS <= receipt.keys()))):
        return not _legacy_review(content)
    if not isinstance(receipt, dict) or (plans := _review_plans(receipt)) is None:
        return False
    try:
        return database.owns_plans(conn, plans, user_id)
    except (sqlite3.Error, mysql_diagnosis.MySQLStorageError):
        return False


def ordinary_visible(conn, user_id, content, stored_scope):
    if stored_scope is None:
        return visible(conn, user_id, content)
    try:
        receipt = json.loads(stored_scope)
    except ValueError:
        return False
    return visible(conn, user_id, content, receipt, bound=True)
