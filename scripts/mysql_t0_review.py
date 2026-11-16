"""MySQL-T0 confirmation and readback, scoped to the validation database."""

import json
import re
from contextlib import closing
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from backend.database.diagnosis_reviews import RECEIPT_COLUMNS, _receipt
from backend.database.diagnosis_tasks import CandidateRevisionChanged, OWNED_ACTIVE_SCOPE
from backend.tools.plan_metrics import plan_metric_coverage


def _plan_key(value):
    # Structured proposals can carry a numeric string. Avoid MySQL's implicit
    # string-to-number comparison, which can accept a numeric prefix or round IDs.
    if isinstance(value, str) and re.fullmatch(
        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value.strip()
    ):
        try:
            value = Decimal(value.strip())
        except InvalidOperation:
            return None
    if type(value) not in (int, Decimal) or not -(2**63) <= value < 2**63:
        return None
    return int(value) if value == int(value) else None


def _require_candidate_revision(cursor, user_id, snapshot):
    revision = [
        {"plan_id": item["plan_id"], **{
            key: item["metrics"]["baseline"][key] for key in ("source_id", "data_version", "source_plan_id")
        }}
        for item in sorted(snapshot["comparison"]["candidates"], key=lambda item: item["plan_id"])
    ]
    cursor.execute(
        """SELECT p.id AS plan_id, b.source_id, b.data_version, b.source_plan_id
           FROM ad_plans p FORCE INDEX (owner_status_plan)
           LEFT JOIN plan_metric_bindings b ON b.plan_id=p.id
           WHERE p.user_id=%s AND p.status='active' ORDER BY p.id LIMIT %s FOR SHARE""",
        (user_id, len(revision) + 1),
    )
    if list(cursor.fetchall()) != revision:
        raise CandidateRevisionChanged()
    coverage, error = plan_metric_coverage(revision)
    if error or any("error_code" in item for item in coverage):
        raise CandidateRevisionChanged()


def _as_receipt(row):
    receipt = _receipt(row)
    if receipt is not None:
        timestamp = receipt["created_at"].isoformat(timespec="milliseconds")
        receipt["created_at"] = timestamp + "Z"
    return receipt


def _read_review(cursor, user_id, review_id):
    cursor.execute(
        f"SELECT {RECEIPT_COLUMNS} FROM diagnosis_reviews WHERE review_id=%s AND user_id=%s",
        (review_id, user_id),
    )
    return _as_receipt(cursor.fetchone())


def read_review(connect, user_id, review_id):
    """Read a committed receipt using a fresh connection to mysql_t0."""
    with closing(connect()) as conn, conn.cursor() as cursor:
        return _read_review(cursor, user_id, review_id)


def _confirm(cursor, user_id, action_id, after_insert):
    cursor.execute(
        "SELECT turn_id, snapshot FROM diagnosis_review_proposals WHERE action_id=%s FOR UPDATE",
        (action_id,),
    )
    proposal = cursor.fetchone()
    if proposal is None:
        return None

    # Establish the receipt read view only after the preceding confirmer releases
    # the proposal lock. A nonlocking lookup avoids locking a missing receipt's gap.
    cursor.execute(
        f"SELECT {RECEIPT_COLUMNS} FROM diagnosis_reviews WHERE action_id=%s AND user_id=%s",
        (action_id, user_id),
    )
    existing = _as_receipt(cursor.fetchone())
    if existing is not None:
        return existing

    cursor.execute(
        """SELECT task.task_id, task.plan_id, task.rule_id, task.mode
           FROM diagnosis_turns AS t JOIN diagnosis_tasks AS task
             ON task.user_id=t.user_id AND task.task_id=t.task_id
           WHERE t.id=%s AND t.user_id=%s AND t.reply IS NOT NULL FOR SHARE""",
        (proposal["turn_id"], user_id),
    )
    task = cursor.fetchone()
    if task is None:
        return None
    snapshot = json.loads(proposal["snapshot"])
    if snapshot.get("task_id") != task["task_id"]:
        return None
    if task["mode"] != "natural" and (
        snapshot.get("plan_id") != json.loads(task["plan_id"]) or snapshot.get("rule_id") != task["rule_id"]
    ):
        return None
    plan_id = _plan_key(snapshot.get("plan_id"))
    if plan_id is None:
        return None
    cursor.execute("SELECT id FROM ad_plans WHERE id=%s AND user_id=%s FOR SHARE", (plan_id, user_id))
    if cursor.fetchone() is None:
        return None
    if snapshot.get("candidate_scope", {}).get("kind") == OWNED_ACTIVE_SCOPE:
        _require_candidate_revision(cursor, user_id, snapshot)

    review_id = uuid4().hex
    cursor.execute(
        "INSERT INTO diagnosis_reviews (review_id, action_id, user_id, turn_id, snapshot) VALUES (%s, %s, %s, %s, %s)",
        (review_id, action_id, user_id, proposal["turn_id"], proposal["snapshot"]),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("review_insert_count")
    if after_insert is not None:
        after_insert(cursor, review_id)
    return _read_review(cursor, user_id, review_id)


def confirm_review(connect, user_id, action_id, *, after_insert=None):
    """Confirm in one transaction, returning a receipt only after commit."""
    with closing(connect()) as conn, conn.cursor() as cursor:
        conn.begin()
        try:
            receipt = _confirm(cursor, user_id, action_id, after_insert)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return receipt
