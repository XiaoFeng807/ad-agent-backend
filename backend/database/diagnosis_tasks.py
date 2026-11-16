import json
from contextlib import closing
from dataclasses import dataclass
from uuid import uuid4

from backend.agent.diagnosis import ContinueDiagnosis, DiagnosisSnapshot, NaturalDiagnosis, NaturalDiagnosisRequest
from backend.database import database, mysql_diagnosis
from backend.database.diagnosis_pauses import create_pause_proposal
from backend.database.mysql_diagnosis import is_mysql, plan_key
from backend.tools.plan_metrics import plan_metric_coverage


OWNED_ACTIVE_SCOPE = "owned_active_plans"


class InvalidNaturalSource(Exception):
    pass


class CandidateRevisionChanged(Exception):
    code = "candidate_revision_changed"

    def __init__(self):
        super().__init__("候选范围、计划状态或指标来源已变化，请重新诊断")


def active_analysis_candidates(conn, user_id, *, limit, lock=False):
    locking = lock and is_mysql(conn)
    index = " FORCE INDEX (owner_status_plan)" if locking else ""
    suffix = " FOR SHARE" if locking else ""
    return [dict(row) for row in conn.execute(
        f"""SELECT p.id AS plan_id, b.source_id, b.data_version, b.source_plan_id
           FROM ad_plans p{index}
           LEFT JOIN plan_metric_bindings b ON b.plan_id=p.id
           WHERE p.user_id=? AND p.status='active' ORDER BY p.id LIMIT ?{suffix}""",
        (user_id, limit),
    )]


def require_candidate_revision(conn, user_id, revision):
    if active_analysis_candidates(conn, user_id, limit=len(revision) + 1, lock=True) != revision:
        raise CandidateRevisionChanged()
    coverage, error = plan_metric_coverage(revision)
    if error or any("error_code" in item for item in coverage):
        raise CandidateRevisionChanged()


@dataclass(frozen=True)
class DiagnosisRun:
    snapshot: DiagnosisSnapshot | NaturalDiagnosis
    turn_id: int
    history: list[dict[str, str]]


def accept_diagnosis(user_id, params, message, *, trace_id=None):
    """Accept parameters and capture this turn's history in the same transaction."""
    with closing(database.get_diagnosis_db()) as conn, conn:
        mysql = is_mysql(conn)
        if mysql:
            conn.begin()
        else:
            conn.execute("BEGIN IMMEDIATE")
        lock = " FOR UPDATE" if mysql else ""
        if isinstance(params, ContinueDiagnosis):
            task_id = params.task_id
            task = conn.execute(
                "SELECT plan_id, rule_id FROM diagnosis_tasks WHERE user_id=? AND task_id=? AND mode='structured'" + lock,
                (user_id, task_id),
            ).fetchone()
            if task is None:
                return None
            plan_id = json.loads(task["plan_id"]) if mysql else task["plan_id"]
            snapshot = DiagnosisSnapshot(**params.model_dump(), plan_id=plan_id, rule_id=task["rule_id"])
            conn.execute(
                "UPDATE diagnosis_tasks SET baseline_start=?, baseline_end=?, current_start=?, current_end=? "
                "WHERE user_id=? AND task_id=?",
                (*_dates(snapshot), user_id, task_id),
            )
        elif isinstance(params, NaturalDiagnosisRequest):
            task_id = params.task_id or uuid4().hex
            if params.task_id:
                task = conn.execute("SELECT 1 FROM diagnosis_tasks WHERE user_id=? AND task_id=? AND mode='natural'" + lock,
                                    (user_id, task_id)).fetchone()
                if task is None:
                    return None
                source = conn.execute("SELECT reply, snapshot FROM diagnosis_turns WHERE user_id=? AND task_id=? AND id=?", (user_id, task_id, params.source_turn_id)).fetchone()
                if source is None or source["reply"] is None:
                    raise InvalidNaturalSource()
                previous = json.loads(source["snapshot"])
                if not previous.get("comparison") or previous.get("candidate_scope", {}).get("kind") != OWNED_ACTIVE_SCOPE or not previous.get("rule_id"):
                    raise InvalidNaturalSource()
                snapshot = NaturalDiagnosis(task_id=task_id, mode="natural", source_turn_id=params.source_turn_id, inherited_rule_id=previous["rule_id"],
                                            inherited_fields=("comparison_target", "candidate_scope_definition", "rule_id"), overridden_fields=("window",))
            else:
                snapshot = NaturalDiagnosis(task_id=task_id, mode="natural")
                conn.execute(
                    "INSERT INTO diagnosis_tasks "
                    "(user_id, task_id, plan_id, rule_id, baseline_start, baseline_end, current_start, current_end, mode) "
                    "VALUES (?, ?, ?, '', '', '', '', '', 'natural')",
                    (user_id, task_id, json.dumps("") if mysql else ""),
                )
        else:
            task_id = uuid4().hex
            snapshot = DiagnosisSnapshot(task_id=task_id, **params.model_dump())
            conn.execute(
                "INSERT INTO diagnosis_tasks "
                "(user_id, task_id, plan_id, rule_id, baseline_start, baseline_end, current_start, current_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, task_id, json.dumps(snapshot.plan_id) if mysql else snapshot.plan_id,
                 snapshot.rule_id, *_dates(snapshot)),
            )

        rows = conn.execute(
            "SELECT message, reply FROM diagnosis_turns WHERE user_id=? AND task_id=? "
            "ORDER BY id DESC LIMIT 25",
            (user_id, task_id),
        ).fetchall()
        history = []
        for row in reversed(rows):
            history.append({"role": "user", "content": row["message"]})
            if row["reply"] is not None:
                history.append({"role": "assistant", "content": row["reply"]})
        history.append({"role": "user", "content": message})
        turn_id = conn.execute(
            "INSERT INTO diagnosis_turns (user_id, task_id, message, snapshot) VALUES (?, ?, ?, ?)",
            (user_id, task_id, message, json.dumps({**snapshot.model_dump(), "source_trace_id": trace_id},
                                                 ensure_ascii=False)),
        ).lastrowid
        return DiagnosisRun(snapshot, turn_id, history)


def save_diagnosis_reply(user_id, run, reply, trace_id, evidence):
    """Commit the reply, original evidence and any proposals together."""
    snapshot = run.snapshot
    natural = isinstance(snapshot, NaturalDiagnosis)
    data = {
        **snapshot.model_dump(), "source_trace_id": trace_id,
        "citations": evidence.get("citations", []), "evidence_gaps": evidence.get("evidence_gaps", []),
        "metrics": evidence.get("metrics", {}), "calculation": evidence.get("calculation"),
        "rule_evaluation": evidence.get("rule_evaluation"),
    }
    if natural and "comparison" in evidence:
        data.update({key: evidence[key] for key in ("plan_id", "rule_id", "baseline", "current", "comparison")})
    if natural and "candidate_scope" in evidence:
        data["candidate_scope"] = evidence["candidate_scope"]
    saved_snapshot = json.dumps(data, ensure_ascii=False)
    with closing(database.get_diagnosis_db()) as conn, conn:
        mysql = is_mysql(conn)
        if mysql:
            # Accept and save serialize on the task before either locks a turn.
            conn.execute("SELECT task_id FROM diagnosis_tasks WHERE user_id=? AND task_id=? FOR UPDATE",
                         (user_id, snapshot.task_id))
        cursor = conn.execute(
            "UPDATE diagnosis_turns SET reply=?, snapshot=? WHERE user_id=? AND task_id=? AND id=? AND reply IS NULL",
            (reply, saved_snapshot, user_id, snapshot.task_id, run.turn_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("未找到待提交的诊断轮次")
        if natural:
            if "comparison" not in data:
                return {}
            require_candidate_revision(conn, user_id, evidence["candidate_revision"])
            conn.execute(
                "UPDATE diagnosis_tasks SET plan_id=?, rule_id=?, baseline_start=?, baseline_end=?, "
                "current_start=?, current_end=? WHERE user_id=? AND task_id=? AND ?=(SELECT MAX(id) FROM diagnosis_turns WHERE user_id=? AND task_id=?)",
                (json.dumps(data["plan_id"]) if mysql else data["plan_id"],
                 data["rule_id"], data["baseline"]["start_date"], data["baseline"]["end_date"],
                 data["current"]["start_date"], data["current"]["end_date"], user_id, snapshot.task_id, run.turn_id, user_id, snapshot.task_id),
            )
            if data["rule_evaluation"]["matched"] is not True:
                return {}
        plan_id = plan_key(data["plan_id"]) if mysql else data["plan_id"]
        if plan_id is None or conn.execute(
            "SELECT 1 FROM ad_plans WHERE id=? AND user_id=?" + (" FOR SHARE" if mysql else ""),
            (plan_id, user_id),
        ).fetchone() is None:
            return {}
        action_id = uuid4().hex
        cursor = conn.execute(
            "INSERT INTO diagnosis_review_proposals (action_id, turn_id, snapshot) VALUES (?, ?, ?)",
            (action_id, run.turn_id, saved_snapshot),
        )
        if cursor.rowcount != 1:
            raise LookupError("复核提案未写入")
        review = {"action_id": action_id, "turn_id": run.turn_id, **data}
        pause = None if natural else create_pause_proposal(conn, user_id, review)
        return {"review_proposal": review, **({"pause_proposal": pause} if pause else {})}


def _task_summary(row, *, mysql=False):
    summary = {
        "task_id": row["task_id"], "plan_id": json.loads(row["plan_id"]) if mysql else row["plan_id"],
        "rule_id": row["rule_id"],
        "baseline": {"start_date": row["baseline_start"], "end_date": row["baseline_end"]},
        "current": {"start_date": row["current_start"], "end_date": row["current_end"]},
    }
    if row["mode"] == "natural":
        summary["mode"] = "natural"
        if not summary["plan_id"]:
            summary.update(plan_id=None, rule_id=None, baseline=None, current=None)
    return summary


def list_diagnosis_tasks(conn, user_id, limit, before):
    rows = conn.execute(
        """WITH owned_tasks AS (
               SELECT task.*, COALESCE((
                   SELECT MIN(id) FROM diagnosis_turns
                   WHERE user_id=task.user_id AND task_id=task.task_id
               ), 0) AS first_turn_id
               FROM diagnosis_tasks task WHERE user_id=?
           )
           SELECT * FROM owned_tasks
           WHERE (? IS NULL OR (first_turn_id, task_id) < (?, ?))
           ORDER BY first_turn_id DESC, task_id DESC LIMIT ?""",
        (user_id, before[0], *before, limit + 1),
    ).fetchall()
    next_cursor = None
    if len(rows) > limit:
        rows.pop()
        last = rows[-1]
        next_cursor = f"{last['first_turn_id']}:{last['task_id']}"
    return {"tasks": [_task_summary(row, mysql=is_mysql(conn)) for row in rows], "next_cursor": next_cursor}


def read_diagnosis_task(conn, user_id, task_id):
    task = conn.execute(
        "SELECT * FROM diagnosis_tasks WHERE user_id=? AND task_id=?", (user_id, task_id),
    ).fetchone()
    if task is None:
        return None
    mysql = is_mysql(conn)
    summary = _task_summary(task, mysql=mysql)
    owned = None
    if mysql:
        task = {**task, "plan_id": summary["plan_id"]}
        owned = {row["id"] for row in conn.execute("SELECT id FROM ad_plans WHERE user_id=?", (user_id,))}
    if mysql and not mysql_diagnosis.pause_enabled():
        rows = conn.execute(
            """SELECT t.id, t.message, t.reply, t.snapshot, p.snapshot AS review_snapshot,
                      p.action_id AS review_action_id, r.review_id, NULL AS pause_action_id
               FROM diagnosis_turns t
               LEFT JOIN diagnosis_review_proposals p ON p.turn_id=t.id
               LEFT JOIN diagnosis_reviews r ON r.action_id=p.action_id AND r.user_id=t.user_id
               WHERE t.user_id=? AND t.task_id=? ORDER BY t.id""",
            (user_id, task_id),
        ).fetchall()
        return {**summary, "turns": [_read_turn(row, task, False, owned) for row in rows]}
    plan_reference = "pause.plan_id" if mysql else "json_extract(proposal.snapshot, '$.plan_id')"
    rows = conn.execute(
        f"""SELECT turn.id, turn.message, turn.reply, turn.snapshot, proposal.snapshot AS review_snapshot,
                  proposal.action_id AS review_action_id, review.review_id,
                  plan.id AS owned_plan_id, plan.status AS plan_status,
                  pause.action_id AS pause_action_id, pause.plan_id, pause.plan_name, pause.expected_status,
                  target.registration_id, receipt.receipt_id
           FROM diagnosis_turns turn
           LEFT JOIN diagnosis_review_proposals proposal ON proposal.turn_id=turn.id
           LEFT JOIN diagnosis_reviews review ON review.action_id=proposal.action_id AND review.user_id=turn.user_id
           LEFT JOIN diagnosis_pause_proposals pause ON pause.review_action_id=proposal.action_id AND pause.plan_id=?
           LEFT JOIN ad_plans plan ON plan.id={plan_reference} AND plan.user_id=turn.user_id
           LEFT JOIN diagnosis_sandbox_targets target ON target.plan_id=pause.plan_id AND target.user_id=turn.user_id
                                                     AND target.registration_id=pause.registration_id
           LEFT JOIN diagnosis_pause_receipts receipt ON receipt.action_id=pause.action_id
           WHERE turn.user_id=? AND turn.task_id=? ORDER BY turn.id""",
        (plan_key(task["plan_id"]) if mysql else task["plan_id"], user_id, task_id),
    )
    sandbox_enabled = mysql or bool(database.sandbox_db_path())
    return {**summary, "turns": [_read_turn(row, task, sandbox_enabled, owned) for row in rows]}


def _read_turn(row, task, sandbox_enabled, owned_plans=None):
    saved = row["reply"] is not None
    snapshot = row["snapshot"] or (row["review_snapshot"] if saved else None)
    turn = {
        "turn_id": row["id"], "message": row["message"], "reply": row["reply"],
        "status": "committed" if saved else "uncommitted",
        "snapshot": json.loads(snapshot) if snapshot else None,
        "review": None, "pause": None,
    }
    if not saved or row["review_action_id"] is None:
        return turn

    review_snapshot = json.loads(row["review_snapshot"])
    review_matches = review_snapshot.get("task_id") == task["task_id"] and (task["mode"] == "natural" or all(review_snapshot.get(field) == task[field] for field in ("plan_id", "rule_id")))
    owned = (row["owned_plan_id"] is not None if owned_plans is None
             else plan_key(review_snapshot.get("plan_id")) in owned_plans)
    review_available = review_matches and owned
    turn["review"] = {
        "action_id": row["review_action_id"], "review_id": row["review_id"],
        "state": "approved" if row["review_id"] else "pending" if review_available else "unavailable",
    }
    if row["pause_action_id"]:
        reason = None
        if not sandbox_enabled:
            reason = "sandbox_disabled"
        elif row["registration_id"] is None or row["owned_plan_id"] is None:
            reason = "target_unavailable"
        elif row["receipt_id"] is None and row["plan_status"] != row["expected_status"]:
            reason = "status_changed"
        turn["pause"] = {
            "action_id": row["pause_action_id"],
            "state": "approved" if row["receipt_id"] else "unavailable" if reason else "pending",
            "reason": reason,
            "target": {key: row[key] for key in ("plan_id", "plan_name", "expected_status")},
        }
    return turn


def _dates(snapshot):
    return (
        snapshot.baseline.start_date, snapshot.baseline.end_date,
        snapshot.current.start_date, snapshot.current.end_date,
    )
