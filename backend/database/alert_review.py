"""Owner-checked alert associations for a SQLite work-group review."""

from contextlib import closing
from datetime import datetime, time, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from backend.database import database


def now():
    return datetime.now(timezone.utc)


def week_period(requested_at):
    shanghai = ZoneInfo("Asia/Shanghai")
    end = requested_at.astimezone(shanghai)
    start = datetime.combine(end.date() - timedelta(days=end.weekday()), time.min, shanghai)
    return {"timezone": shanghai.key, "start": start.isoformat(), "end": end.isoformat()}


def _sqlite_utc(iso):
    return datetime.fromisoformat(iso).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def _rows(conn, user_id, period):
    return conn.execute(
        """SELECT a.id AS alert_id, a.created_at, a.plan_id IS NULL AS unlinked, a.ctr_check_evidence,
                  p.id AS plan_id, b.source_id, b.data_version, b.source_plan_id
           FROM alerts a
           LEFT JOIN ad_plans p ON p.id=a.plan_id AND p.user_id=a.user_id
           LEFT JOIN plan_metric_bindings b ON b.plan_id=p.id
           WHERE a.user_id=? AND a.type='ctr_review' AND a.level='critical'
             AND a.created_at>=? AND a.created_at<=?
           ORDER BY a.created_at, a.id""",
        (user_id, _sqlite_utc(period["start"]), _sqlite_utc(period["end"])),
    )


def _association(row):
    association = {key: row[key] for key in ("alert_id", "created_at", "plan_id")}
    if row["ctr_check_evidence"] is not None:
        snapshot = json.loads(row["ctr_check_evidence"])
        association["ctr_check"] = {key: snapshot[key] for key in (
            "detected_at", "baseline", "current", "comparison", "rule", "policy",
        )}
    return association


def _binding(row):
    return {key: row[key] for key in ("plan_id", "source_id", "data_version", "source_plan_id")}


def _changed_check_binding(row):
    if row["ctr_check_evidence"] is None:
        return False
    binding = json.loads(row["ctr_check_evidence"])["binding"]
    return any(row[key] != value for key, value in binding.items())


def read_weekly_alerts(user_id, period):
    """Only project checked IDs. Free text and invalid foreign IDs stay in storage."""
    with closing(database.get_db(read_only=True)) as conn:
        rows = _rows(conn, user_id, period).fetchall()
    associations, gaps, bindings = [], [], {}
    for row in rows:
        if row["plan_id"] is None:
            gaps.append({"alert_id": row["alert_id"], "created_at": row["created_at"],
                         "error_code": "alert_plan_missing" if row["unlinked"] else "alert_plan_unverified"})
        elif _changed_check_binding(row):
            gaps.append({"alert_id": row["alert_id"], "created_at": row["created_at"],
                         "error_code": "alert_history_changed"})
        else:
            associations.append(_association(row))
            bindings.setdefault(row["plan_id"], _binding(row))
    return associations, gaps, bindings


def current_associations(conn, user_id, period, associations, bindings=None):
    """Recheck the captured associations and bindings against the current owner view."""
    rows = {row["alert_id"]: row for row in _rows(conn, user_id, period)}
    verified, gaps = [], []
    for item in associations:
        row = rows.get(item["alert_id"])
        if row is None or _association(row) != item:
            gaps.append({"alert_id": item["alert_id"], "created_at": item["created_at"],
                         "error_code": "alert_plan_unverified"})
        elif _changed_check_binding(row) or (bindings is not None and bindings[item["plan_id"]] != _binding(row)):
            gaps.append({"alert_id": item["alert_id"], "created_at": item["created_at"],
                         "error_code": "alert_history_changed"})
        else:
            verified.append(item)
    return verified, gaps
