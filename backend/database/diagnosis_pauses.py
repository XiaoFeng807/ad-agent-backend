"""Explicitly registered, single-plan pause actions in the isolated application database."""

import argparse
from contextlib import closing
import json
from sqlite3 import IntegrityError
from uuid import uuid4

from backend.database import database, mysql_diagnosis


PAUSE_RULE_ID = "ctr-review-v1#low-ctr"


class PauseRejected(Exception):
    pass


def pause_enabled():
    if database.settings.DIAGNOSIS_MYSQL_ENABLED:
        return mysql_diagnosis.pause_enabled()
    return bool(database.sandbox_db_path())


def register_sandbox_target(user_id, plan_id):
    if not pause_enabled():
        raise ValueError("请先启用所选数据库的暂停沙箱")
    with closing(database.get_diagnosis_db()) as conn, conn:
        mysql = mysql_diagnosis.is_mysql(conn)
        conn.execute("BEGIN" if mysql else "BEGIN IMMEDIATE")
        plan = conn.execute(
            "SELECT p.status FROM ad_plans p JOIN users u ON u.id=p.user_id WHERE p.id=? AND p.user_id=?"
            + (" FOR UPDATE OF p" if mysql else ""),
            (plan_id, user_id),
        ).fetchone()
        if plan is None or plan["status"] != "active":
            raise ValueError("仅可登记同属主的 active 沙箱计划")
        target = conn.execute(
            "SELECT user_id FROM diagnosis_sandbox_targets WHERE plan_id=?" + (" FOR SHARE" if mysql else ""),
            (plan_id,),
        ).fetchone()
        if target is None:
            conn.execute(
                "INSERT INTO diagnosis_sandbox_targets (plan_id, user_id, registration_id) VALUES (?, ?, ?)",
                (plan_id, user_id, uuid4().hex),
            )
        elif target["user_id"] != user_id:
            raise ValueError("计划已有其他属主的登记")


def create_pause_proposal(conn, user_id, review):
    # The review snapshot comes from the server's validated tool receipts for this turn.
    evaluation = review["rule_evaluation"]
    if (not pause_enabled() or review["rule_id"] != PAUSE_RULE_ID or review["evidence_gaps"]
            or review["metrics"].keys() != {"baseline", "current"} or review["calculation"] is None
            or not evaluation or evaluation["rule_id"] != PAUSE_RULE_ID or evaluation["matched"] is not True
            or not any(c["chunk_id"] == evaluation["chunk_id"] for c in review["citations"])):
        return None
    target = _eligible_target(conn, user_id, review["plan_id"])
    if target is None:
        return None
    action_id = uuid4().hex
    cursor = conn.execute(
        """INSERT INTO diagnosis_pause_proposals
           (action_id, review_action_id, plan_id, registration_id, plan_name, expected_status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (action_id, review["action_id"], target["id"], target["registration_id"], target["plan_name"], target["status"]),
    )
    if cursor.rowcount != 1:
        raise IntegrityError("暂停提案未写入")
    return {**review, "action_id": action_id, "target": {
        "plan_id": target["id"], "plan_name": target["plan_name"], "expected_status": target["status"],
    }}


def _eligible_target(conn, user_id, plan_id):
    mysql = mysql_diagnosis.is_mysql(conn)
    plan_id = mysql_diagnosis.plan_key(plan_id) if mysql else plan_id
    target = conn.execute(
        """SELECT p.id, p.plan_name, p.status, t.registration_id
           FROM diagnosis_sandbox_targets t
           JOIN ad_plans p ON p.id=t.plan_id AND p.user_id=t.user_id
           JOIN users u ON u.id=t.user_id
           WHERE t.plan_id=? AND t.user_id=? AND p.status='active'"""
        + (" FOR SHARE OF t" if mysql else ""),
        (plan_id, user_id),
    ).fetchone()
    return target


def _load_action(conn, action_id, user_id, *, lock_target=False):
    mysql = mysql_diagnosis.is_mysql(conn)
    task_plan_check = "" if mysql else " AND task.plan_id=p.plan_id"
    action = conn.execute(
        f"""SELECT p.action_id, p.plan_id, p.plan_name, p.expected_status, p.registration_id,
                  task.plan_id AS task_plan_id,
                  review.turn_id, review.snapshot, r.receipt_id, r.execution_trace_id, r.committed_status, r.created_at
           FROM diagnosis_pause_proposals p
           JOIN diagnosis_review_proposals review ON review.action_id=p.review_action_id
           JOIN diagnosis_turns turn ON turn.id=review.turn_id
           JOIN diagnosis_tasks task ON task.user_id=turn.user_id AND task.task_id=turn.task_id{task_plan_check}
           LEFT JOIN diagnosis_pause_receipts r ON r.action_id=p.action_id
           WHERE p.action_id=? AND turn.user_id=? AND turn.reply IS NOT NULL""",
        (action_id, user_id),
    ).fetchone()
    if action is None or (mysql and mysql_diagnosis.plan_key(json.loads(action["task_plan_id"])) != action["plan_id"]):
        raise PauseRejected("pause_not_found")
    return _load_target_status(conn, action, user_id, lock_target=lock_target)


def _load_target_status(conn, action, user_id, *, lock_target=False):
    # Approval takes current locks in the same order as plan transfers and target registration.
    plan = conn.execute(
        "SELECT p.status FROM ad_plans p JOIN users u ON u.id=p.user_id WHERE p.id=? AND p.user_id=?"
        + (" FOR UPDATE OF p" if lock_target else ""),
        (action["plan_id"], user_id),
    ).fetchone()
    if plan is None:
        raise PauseRejected("pause_not_found")
    target = conn.execute(
        "SELECT registration_id FROM diagnosis_sandbox_targets WHERE plan_id=? AND user_id=?"
        + (" FOR SHARE" if lock_target else ""),
        (action["plan_id"], user_id),
    ).fetchone()
    if target is None or target["registration_id"] != action["registration_id"]:
        raise PauseRejected("pause_not_found")
    return {**action, "actual_status": plan["status"]}


def _receipt(action):
    created_at = action["created_at"]
    if not isinstance(created_at, str):
        created_at = created_at.isoformat(timespec="milliseconds") + "Z"
    return {
        **json.loads(action["snapshot"]),
        **{key: action[key] for key in ("action_id", "receipt_id", "execution_trace_id", "committed_status")},
        **({"turn_id": action["turn_id"]} if "turn_id" in action.keys() else {
            key: action[key] for key in ("approved_by_user_id", "approved_by_name")
        }),
        "created_at": created_at,
        "target": {key: action[key] for key in ("plan_id", "plan_name", "expected_status")},
    }


def _current_user(conn, token, validate_token, *, lock_session=False):
    payload = validate_token(token, connection=conn, **({"lock_session": True} if lock_session else {}))
    if payload is None:
        raise PauseRejected("pause_session_invalid")
    return payload["user_id"]


def _confirm_action(conn, load_action, user_id, trace_id, *, approved_by_name=None):
    action = load_action()
    if action["receipt_id"] is not None:
        return action
    if action["actual_status"] != action["expected_status"]:
        raise PauseRejected("pause_status_changed")
    updated = conn.execute(
        "UPDATE ad_plans SET status='paused' WHERE id=? AND user_id=? AND status=?",
        (action["plan_id"], user_id, action["expected_status"]),
    )
    if updated.rowcount != 1:
        raise IntegrityError("沙箱计划未暂停")
    params = (uuid4().hex, action["action_id"], trace_id)
    if approved_by_name is None:
        saved = conn.execute(
            """INSERT INTO diagnosis_pause_receipts (receipt_id, action_id, execution_trace_id, committed_status)
               VALUES (?, ?, ?, 'paused')""",
            params,
        )
    else:
        saved = conn.execute(
            """INSERT INTO chat_group_pause_receipts
               (receipt_id, action_id, execution_trace_id, committed_status, approved_by_user_id, approved_by_name)
               VALUES (?, ?, ?, 'paused', ?, ?)""",
            (*params, user_id, approved_by_name),
        )
    action = load_action()
    if saved.rowcount != 1 or action["receipt_id"] is None or action["actual_status"] != "paused":
        raise IntegrityError("暂停状态或回执读回失败")
    return action


def confirm_pause(action_id, token, validate_token, trace_id):
    if not pause_enabled():
        raise PauseRejected("pause_not_found")
    with closing(database.get_diagnosis_db()) as conn, conn:
        mysql = mysql_diagnosis.is_mysql(conn)
        conn.execute("BEGIN" if mysql else "BEGIN IMMEDIATE")
        # Lock the current session before the MySQL snapshot so same-owner replays see the first receipt.
        user_id = _current_user(conn, token, validate_token, lock_session=mysql)

        def load_action():
            return _load_action(conn, action_id, user_id, lock_target=mysql)

        return _receipt(_confirm_action(conn, load_action, user_id, trace_id))


def read_pause(action_id, token, validate_token):
    if not pause_enabled():
        raise PauseRejected("pause_not_found")
    with closing(database.get_diagnosis_db(read_only=database.settings.DIAGNOSIS_MYSQL_ENABLED)) as conn, conn:
        conn.execute("BEGIN")
        user_id = _current_user(conn, token, validate_token)
        action = _load_action(conn, action_id, user_id)
        if action["receipt_id"] is None:
            raise PauseRejected("pause_not_found")
        return {"receipt": _receipt(action), "actual_status": action["actual_status"]}


def group_pause_enabled():
    return not database.settings.DIAGNOSIS_MYSQL_ENABLED and bool(database.sandbox_db_path())


def create_group_pause_proposals(conn, run, entry_id, evidence):
    if (not group_pause_enabled() or evidence["review_status"] != "completed"
            or evidence["gap"] or evidence["missing_detail_plan_ids"]
            or evidence["scope"]["rule"] != PAUSE_RULE_ID):
        return
    from backend.agent.diagnosis import calculate_ctr, verified_ctr_policy

    rule = evidence["rule"]
    policy = verified_ctr_policy(PAUSE_RULE_ID, [{**rule, "content": rule["excerpt"]}])
    if policy is None:
        return
    for detail in evidence["details"]:
        plan_id = detail["id"]
        metrics = {name: next(row for row in evidence["metrics"]
                              if row["plan_id"] == plan_id
                              and all(row[key] == value for key, value in evidence["scope"][name].items()))
                   for name in ("baseline", "current")}
        calculation, decline = calculate_ctr(metrics["baseline"], metrics["current"])
        if (min(row["impressions"] for row in metrics.values()) < policy["min_impressions"]
                or decline < policy["min_relative_decline_pct"]):
            continue
        target = _eligible_target(conn, run.user_id, plan_id)
        if target is None:
            continue
        reason = (f"CTR 相对下降 {calculation['relative_decline_pct']:.2f}%，"
                  f"达到 {PAUSE_RULE_ID} 的 {policy['min_relative_decline_pct']}% 阈值")
        snapshot = {"conversation_id": run.conversation_id, "run_id": run.run_id, "entry_id": f"entry:{entry_id}",
                    "source_trace_id": run.trace_id, "reason": reason, "evidence": evidence,
                    "calculation": calculation, "rule_evaluation": {**policy, "matched": True}}
        saved = conn.execute(
            """INSERT INTO chat_group_pause_proposals
               (action_id, conversation_id, run_id, entry_id, plan_id, registration_id, plan_name, expected_status,
                reason, snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uuid4().hex, run.conversation_id, run.run_id, entry_id, plan_id, target["registration_id"],
             target["plan_name"], target["status"], reason, json.dumps(snapshot, ensure_ascii=False)),
        )
        if saved.rowcount != 1:
            raise IntegrityError("工作群暂停提案未写入")


def group_pause_summaries(conn, conversation_id):
    rows = conn.execute(
        """SELECT p.entry_id, p.action_id, p.plan_id, p.plan_name, p.expected_status, p.reason,
                  r.receipt_id, r.approved_by_user_id, r.approved_by_name, r.created_at
           FROM chat_group_pause_proposals p LEFT JOIN chat_group_pause_receipts r ON r.action_id=p.action_id
           WHERE p.conversation_id=? ORDER BY p.plan_id""",
        (conversation_id,),
    ).fetchall()
    summaries = {}
    for row in rows:
        summaries.setdefault(row["entry_id"], []).append({
            "action_id": row["action_id"], "reason": row["reason"],
            "target": {key: row[key] for key in ("plan_id", "plan_name", "expected_status")},
            "state": "approved" if row["receipt_id"] else "pending",
            "approval": {key: row[key] for key in ("receipt_id", "approved_by_user_id", "approved_by_name", "created_at")}
            if row["receipt_id"] else None,
        })
    return summaries


def group_pause_request(conversation_id, run_id, action_id, token, validate_token, trace_id, *, confirm):
    if not group_pause_enabled():
        raise PauseRejected("pause_sandbox_disabled")
    with closing(database.get_db(read_only=not confirm)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE" if confirm else "BEGIN")
        user_id = _current_user(conn, token, validate_token)
        user = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise PauseRejected("pause_session_invalid")

        def load_action():
            row = conn.execute(
                """SELECT p.*, r.receipt_id, r.execution_trace_id, r.committed_status, r.created_at,
                          r.approved_by_user_id, r.approved_by_name
                   FROM chat_group_pause_proposals p
                   JOIN chat_groups g ON g.conversation_id=p.conversation_id
                   JOIN chat_group_runs run ON run.run_id=p.run_id AND run.conversation_id=g.conversation_id
                   JOIN chat_group_entries answer ON answer.id=p.entry_id AND answer.run_id=run.run_id
                   LEFT JOIN chat_group_pause_receipts r ON r.action_id=p.action_id
                   WHERE p.action_id=? AND g.conversation_id=? AND run.run_id=? AND g.user_id=?
                         AND run.status='completed' AND answer.kind='assistant' AND answer.details IS NOT NULL""",
                (action_id, conversation_id, run_id, user_id),
            ).fetchone()
            if row is None:
                raise PauseRejected("pause_not_found")
            return _load_target_status(conn, row, user_id)

        if confirm:
            return _receipt(_confirm_action(conn, load_action, user_id, trace_id, approved_by_name=user["username"]))
        action = load_action()
        if action["receipt_id"] is None:
            raise PauseRejected("pause_not_found")
        return {"receipt": _receipt(action), "actual_status": action["actual_status"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="登记现有同属主的 active 沙箱计划")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--plan-id", type=int, required=True)
    args = parser.parse_args()
    try:
        register_sandbox_target(args.user_id, args.plan_id)
    except ValueError as error:
        parser.error(str(error))
    print(f"已登记沙箱计划 {args.plan_id}")
