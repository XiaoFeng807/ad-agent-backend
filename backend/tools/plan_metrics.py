"""Owned local plan -> trusted public campaign mapping -> read-only artifact."""

from contextlib import closing
from datetime import date, timedelta
from itertools import pairwise
import json
from pathlib import Path
import sqlite3
from backend.database.mysql_diagnosis import is_mysql, plan_key

from backend.config.config import settings


def metric_source_version():
    """Read the configured artifact's identity without loading metric rows."""
    if not settings.PLAN_METRICS_DB_PATH:
        return None
    try:
        path = Path(settings.PLAN_METRICS_DB_PATH).resolve()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
            return manifest["source_id"], manifest["data_version"]
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return None


def common_metric_dates(candidates, through):
    """Return dates present for every bound plan within the complete artifact period."""
    if not settings.PLAN_METRICS_DB_PATH:
        return [], "plan_history_not_configured"
    path = Path(settings.PLAN_METRICS_DB_PATH).resolve()
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
            dates = []
            for candidate in candidates:
                if (candidate["source_id"], candidate["data_version"]) != (
                    manifest["source_id"], manifest["data_version"],
                ):
                    return [], "source_version_mismatch"
                dates.append({date.fromisoformat(row[0]) for row in conn.execute(
                    """SELECT metric_date FROM plan_daily_metrics
                       WHERE source_plan_id=? AND metric_date BETWEEN ? AND ?""",
                    (candidate["source_plan_id"], manifest["complete_start_date"],
                     min(through, manifest["complete_end_date"])),
                )})
        return sorted(set.intersection(*dates)), None
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return [], "plan_history_unavailable"


def latest_comparable_dates(dates, preferred_days):
    """Choose the longest equal adjacent windows up to the preference, then the latest."""
    if len(dates) < 2:
        return None
    one_day = timedelta(days=1)
    lengths = [1]
    for left, right in pairwise(dates):
        lengths.append(lengths[-1] + 1 if right - left == one_day else 1)
    days, end = max((min(length // 2, preferred_days), day) for length, day in zip(lengths, dates, strict=True))
    if not days:
        return None
    return ((end - (2 * days - 1) * one_day, end - days * one_day),
            (end - (days - 1) * one_day, end))


def plan_metric_coverage(candidates):
    """Read date availability without returning any window totals."""
    if not settings.PLAN_METRICS_DB_PATH:
        return None, "plan_history_not_configured"
    path = Path(settings.PLAN_METRICS_DB_PATH).resolve()
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
            coverage = []
            for candidate in candidates:
                entry = {"plan_id": candidate["plan_id"]}
                if candidate["source_plan_id"] is None:
                    entry["error_code"] = "plan_history_not_mapped"
                elif (candidate["source_id"], candidate["data_version"]) != (
                    manifest["source_id"], manifest["data_version"],
                ):
                    entry["error_code"] = "source_version_mismatch"
                else:
                    start, end = conn.execute(
                        """SELECT min(metric_date), max(metric_date) FROM plan_daily_metrics
                           WHERE source_plan_id=? AND metric_date BETWEEN ? AND ?""",
                        (candidate["source_plan_id"], manifest["complete_start_date"], manifest["complete_end_date"]),
                    ).fetchone()
                    entry.update(start_date=start, end_date=end)
                    if start is None:
                        entry["error_code"] = "plan_history_missing_dates"
                coverage.append(entry)
        return coverage, None
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return None, "plan_history_unavailable"


def owned_metric_binding(conn, plan_id, user_id):
    """Return the current binding for an owned plan, including an unmapped plan."""
    lookup_id = plan_key(plan_id) if is_mysql(conn) else plan_id
    return conn.execute(
        """SELECT b.source_id, b.data_version, b.source_plan_id FROM ad_plans p
           LEFT JOIN plan_metric_bindings b ON b.plan_id=p.id WHERE p.id=? AND p.user_id=?""",
        (lookup_id, user_id),
    ).fetchone()


def query_plan_metrics(plan_id, start_date, end_date, user_id, connect):
    if type(user_id) is not int or user_id <= 0:
        return {"success": False, "error_code": "authentication_required"}
    if type(plan_id) not in (int, str):
        return {"success": False, "error_code": "invalid_plan_id"}
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start.isoformat() != start_date or end.isoformat() != end_date or not 0 <= (end-start).days <= 90:
            raise ValueError()
    except (ValueError, TypeError):
        return {"success": False, "error_code": "invalid_date_window"}
    # Ownership precedes artifact access, even if no binding or file is configured.
    with closing(connect()) as conn:
        binding = owned_metric_binding(conn, plan_id, user_id)
    if binding is None:
        return {"success": False, "error_code": "plan_not_found"}
    if binding["source_plan_id"] is None:
        return {"success": False, "error_code": "plan_history_not_mapped"}
    if not settings.PLAN_METRICS_DB_PATH:
        return {"success": False, "error_code": "plan_history_not_configured"}
    path = Path(settings.PLAN_METRICS_DB_PATH).resolve()
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
            if any(binding[key] != manifest[key] for key in ("source_id", "data_version")):
                return {"success": False, "error_code": "source_version_mismatch"}
            rows = [dict(row) for row in conn.execute("""SELECT metric_date, impressions, clicks
                FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date BETWEEN ? AND ? ORDER BY metric_date""",
                (binding["source_plan_id"], start_date, end_date))]
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return {"success": False, "error_code": "plan_history_unavailable"}
    expected = [(start + timedelta(days=day)).isoformat() for day in range((end-start).days+1)]
    observed = {row["metric_date"] for row in rows}
    missing = [day for day in expected if day not in observed or not manifest["complete_start_date"] <= day <= manifest["complete_end_date"]]
    return {
        "success": True, "plan_id": plan_id, "user_id": user_id,
        "source_plan_id": binding["source_plan_id"], "scope": "plan",
        "source_id": manifest["source_id"], "data_version": manifest["data_version"],
        "source": {key: manifest[key] for key in ("kind", "source_url", "license", "compressed_sha256", "tsv_sha256", "sampling", "time_mapping")},
        "start_date": start_date, "end_date": end_date,
        "coverage": {"complete": not missing, "expected_days": len(expected), "observed_days": len(rows), "missing_dates": missing},
        "impressions": sum(row["impressions"] for row in rows), "clicks": sum(row["clicks"] for row in rows),
        "daily": rows,
    }
