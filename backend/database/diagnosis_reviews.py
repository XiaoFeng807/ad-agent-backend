import json
from contextlib import closing
from sqlite3 import IntegrityError
from uuid import uuid4

from backend.config.config import settings
from backend.database import database
from backend.database.diagnosis_tasks import OWNED_ACTIVE_SCOPE, require_candidate_revision
from backend.database.mysql_diagnosis import is_mysql, plan_key


RECEIPT_COLUMNS = "review_id, action_id, turn_id, snapshot, created_at"


def _receipt(row, *, mysql=False):
    if row is None:
        return None
    data = dict(row)
    if mysql:
        data["created_at"] = data["created_at"].isoformat(timespec="milliseconds") + "Z"
    return {**json.loads(data.pop("snapshot")), **data}


def confirm_review(user_id, action_id):
    with closing(database.get_diagnosis_db()) as conn, conn:
        mysql = is_mysql(conn)
        if mysql:
            conn.begin()
            # Lock the proposal before establishing the consistent read view.
            # A later confirmer then sees the preceding committed receipt.
            if conn.execute(
                "SELECT action_id FROM diagnosis_review_proposals WHERE action_id=? FOR UPDATE", (action_id,),
            ).fetchone() is None:
                return None
        else:
            conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            f"SELECT {RECEIPT_COLUMNS} FROM diagnosis_reviews WHERE action_id=? AND user_id=?",
            (action_id, user_id),
        ).fetchone()
        if existing is not None:
            return _receipt(existing, mysql=mysql)

        ownership_join = "" if mysql else (
            "JOIN ad_plans plan ON plan.id=json_extract(p.snapshot, '$.plan_id') AND plan.user_id=task.user_id"
        )
        lock = " FOR SHARE" if mysql else ""
        proposal = conn.execute(
            f"""SELECT p.turn_id, p.snapshot, task.task_id, task.plan_id, task.rule_id, task.mode
               FROM diagnosis_review_proposals p
               JOIN diagnosis_turns turn ON turn.id=p.turn_id
               JOIN diagnosis_tasks task ON task.user_id=turn.user_id AND task.task_id=turn.task_id
               {ownership_join}
               WHERE p.action_id=? AND turn.user_id=? AND turn.reply IS NOT NULL{lock}""",
            (action_id, user_id),
        ).fetchone()
        if proposal is None:
            return None
        if mysql:
            proposal["plan_id"] = json.loads(proposal["plan_id"])
        snapshot = json.loads(proposal["snapshot"])
        if snapshot.get("task_id") != proposal["task_id"]:
            return None
        if proposal["mode"] != "natural" and any(snapshot[field] != proposal[field] for field in ("plan_id", "rule_id")):
            return None
        if mysql:
            plan_id = plan_key(snapshot.get("plan_id"))
            if plan_id is None or conn.execute(
                "SELECT id FROM ad_plans WHERE id=? AND user_id=? FOR SHARE", (plan_id, user_id),
            ).fetchone() is None:
                return None
        if snapshot.get("candidate_scope", {}).get("kind") == OWNED_ACTIVE_SCOPE:
            revision = [
                {"plan_id": item["plan_id"], **{
                    key: item["metrics"]["baseline"][key] for key in ("source_id", "data_version", "source_plan_id")
                }}
                for item in sorted(snapshot["comparison"]["candidates"], key=lambda item: item["plan_id"])
            ]
            require_candidate_revision(conn, user_id, revision)

        review_id = uuid4().hex
        cursor = conn.execute(
            "INSERT INTO diagnosis_reviews (review_id, action_id, user_id, turn_id, snapshot) VALUES (?, ?, ?, ?, ?)",
            (review_id, action_id, user_id, proposal["turn_id"], proposal["snapshot"]),
        )
        if cursor.rowcount != 1:
            raise IntegrityError("复核单未写入")
        return _read_review(conn, user_id, review_id)


def _read_review(conn, user_id, review_id):
    return _receipt(conn.execute(
        f"SELECT {RECEIPT_COLUMNS} FROM diagnosis_reviews WHERE review_id=? AND user_id=?",
        (review_id, user_id),
    ).fetchone(), mysql=is_mysql(conn))


def read_review(user_id, review_id):
    with closing(database.get_diagnosis_db(read_only=settings.DIAGNOSIS_MYSQL_ENABLED)) as conn:
        return _read_review(conn, user_id, review_id)
