"""SQLite admission, checkpoints and committed events for background inspections."""

import hashlib
import json
import os
from contextlib import closing, contextmanager
from time import time_ns
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.database import chat_groups, database, listing_operations, orders
from backend.database.listing_evidence import publication_inputs, publication_report_basis
from backend.observability import add_trace_step, end_trace, start_trace


TERMINAL = "finished"


def configured_model():
    if database.settings.DIAGNOSIS_MYSQL_ENABLED or database.sandbox_db_path() is None:
        return None
    return os.getenv("STOREPILOT_BACKGROUND_MODEL") or None


def upgrade_sqlite(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS background_inspections (
        run_id TEXT PRIMARY KEY REFERENCES chat_group_runs(run_id), user_id INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL, spec TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0, executor_id TEXT, finalized INTEGER NOT NULL DEFAULT 0,
        answer_id INTEGER UNIQUE REFERENCES chat_group_entries(id),
        UNIQUE (user_id, idempotency_key)
    )""")
    if "executor_id" not in {row[1] for row in conn.execute("PRAGMA table_info(background_inspections)")}:
        conn.execute("ALTER TABLE background_inspections ADD COLUMN executor_id TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS background_inspection_events (
        run_id TEXT NOT NULL REFERENCES background_inspections(run_id), seq INTEGER NOT NULL,
        kind TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (run_id, seq)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS background_inspection_artifacts (
        artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES background_inspections(run_id),
        generation INTEGER NOT NULL, result TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS background_inspection_checkpoints (
        run_id TEXT NOT NULL REFERENCES background_inspections(run_id), step TEXT NOT NULL,
        artifact_id TEXT UNIQUE NOT NULL REFERENCES background_inspection_artifacts(artifact_id),
        PRIMARY KEY (run_id, step)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS background_inspection_publications (
        user_id INTEGER NOT NULL, conversation_id TEXT NOT NULL, publication_id TEXT NOT NULL,
        windows TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES background_inspections(run_id),
        PRIMARY KEY (user_id, conversation_id, publication_id, windows)
    )""")
    from backend.database import inspection_subscriptions

    inspection_subscriptions.upgrade_sqlite(conn)


def _json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def emit(conn, run_id, kind, **payload):
    conn.execute("""INSERT INTO background_inspection_events (run_id, seq, kind, payload)
        SELECT ?, coalesce(max(seq), 0) + 1, ?, ? FROM background_inspection_events WHERE run_id=?""",
                 (run_id, kind, _json(payload), run_id))


def basis(conn, user_id, candidates):
    """Read identity and source signatures without executing any evidence tools."""
    signatures = {}
    for candidate in candidates:
        target = {key: candidate[key] for key in listing_operations._TARGET_KEYS}
        sku = target["sku"]
        signatures[sku] = {}
        for kind in ("listing", "sales", "facts", "required_facts"):
            row = listing_operations._source(conn, user_id, target, kind=kind)
            signatures[sku][kind] = {
                "version": row["version"] if "version" in row.keys() else None,
                "sha256": hashlib.sha256(_json(dict(row)).encode()).hexdigest(),
            } if row is not None else None
    return signatures


def invalid_items(conn, owner, conversation_id, spec):
    try:
        chat_groups._group(conn, owner, conversation_id)
    except chat_groups.GroupNotFound:
        return [{"source": "conversation_owner"}]
    if conn.execute("SELECT 1 FROM users WHERE id=?", (owner,)).fetchone() is None:
        return [{"source": "conversation_owner"}]
    latest = basis(conn, owner, spec["candidates"])
    invalid = []
    for sku, original in spec["basis"].items():
        current = latest[sku]
        if current["listing"] is None:
            invalid.append({"sku": sku, "source": "product_scope"})
        else:
            invalid.extend({"sku": sku, "source": source, "expected_version": value and value["version"],
                            "current_version": current[source] and current[source]["version"]}
                           for source, value in original.items() if current[source] != value)
    return invalid


def accept(user_id, conversation_id, key, message, model, make_spec, trace_id, request_id):
    request_json = _json({"conversation_id": conversation_id, "message": message})
    with chat_groups._write() as conn:
        group = chat_groups._group(conn, user_id, conversation_id)
        existing = conn.execute("SELECT run_id, request_json FROM background_inspections "
                                "WHERE user_id=? AND idempotency_key=?", (user_id, key)).fetchone()
        if existing:
            if existing["request_json"] != request_json:
                raise chat_groups.GroupConflict("idempotency_key_conflict")
            return existing["run_id"], False
        if group["archived"] or chat_groups._running(conn, conversation_id):
            raise chat_groups.GroupConflict("工作群已归档或已有一轮正在运行")
        now = chat_groups.alert_review.now()
        spec = make_spec(conn, user_id, message, now)
        spec.update(model=model, basis=basis(conn, user_id, spec["candidates"]))
        return _insert(conn, user_id, conversation_id, key, message, request_json, spec, now, trace_id, request_id), True


def _insert(conn, user_id, conversation_id, key, message, request_json, spec, now, trace_id, request_id):
    run, _ = chat_groups._accept(conn, user_id, conversation_id, trace_id, message, 1,
                                 background=True, requested_at=now)
    conn.execute("""INSERT INTO background_inspections (run_id, user_id, idempotency_key, request_json, spec)
                    VALUES (?, ?, ?, ?, ?)""", (run.run_id, user_id, key, request_json, _json(spec)))
    emit(conn, run.run_id, "accepted", trace_id=trace_id, request_id=request_id, acquired_ns=run.slot_acquired_ns)
    return run.run_id


def publication_visible(conn, user_id, publication):
    target = publication["target"]
    return (not listing_operations.scope_error(conn, user_id, **target)
            and orders.has_access(conn, user_id, publication["snapshot"]["source"], target["shop_id"]))


def accept_publication(user_id, conversation_id, publication_id, windows, recheck, model, reader, trace_id, request_id):
    """Bind a server publication and its actual inputs in the group admission transaction."""
    with chat_groups._write() as conn:
        group = chat_groups._group(conn, user_id, conversation_id)
        record = reader(conn, user_id, publication_id)
        return _accept_publication(conn, group, publication_id, windows, recheck, model, record, trace_id, request_id)


class PublicationReportUnused(LookupError):
    pass


def _accept_publication(conn, group, publication_id, windows, recheck, model, record, trace_id, request_id,
                        *, expected_report=None, allow_listing_proposal=True, subscription_id=None):
    user_id, conversation_id = group["user_id"], group["conversation_id"]
    notification_key = (user_id, conversation_id, publication_id, _json(windows))
    if record is None or record["publication_id"] != publication_id:
        raise listing_operations.ListingNotFound
    publication = {key: record[key] for key in ("publication_id", "target", "batch_id",
                                                "sequence", "published_at", "published_by_user_id")}
    publication["snapshot"] = listing_operations._order_snapshot(record)
    if not publication_visible(conn, user_id, publication):
        raise listing_operations.ListingNotFound
    original = conn.execute("""SELECT run_id FROM background_inspection_publications
        WHERE (user_id, conversation_id, publication_id, windows)=(?,?,?,?)""", notification_key).fetchone()
    if original and not recheck:
        return original["run_id"], False
    now = chat_groups.alert_review.now()
    today = now.astimezone(ZoneInfo(listing_operations.SALES_TIMEZONE)).date().isoformat()
    if windows[-1] >= today:
        raise listing_operations.ListingInvalid("请提供两个已结束的销量比较窗口")
    spec = {"publication": publication, "windows": windows, "statistics_date": today, "model": model}
    inputs = publication_inputs(conn, user_id, conversation_id, spec)
    if (inputs["sales"] or {}).get("order_snapshot") != publication["snapshot"]:
        raise chat_groups.GroupConflict("listing_publication_superseded")
    if expected_report is not None:
        report = inputs["report"]
        if (report.get("report") or {}).get("import_id") != expected_report:
            raise PublicationReportUnused("report_unused")
        if publication_report_basis(report, None) is None:
            raise PublicationReportUnused("report_incomplete")
    spec["input_digest"] = listing_operations._digest(inputs)
    if subscription_id is not None:
        spec["subscription_id"] = subscription_id
    # The user key column is also used by the legacy HTTP route. Its ASCII limit
    # leaves this server-generated namespace independent of user keys.
    identity = [conversation_id, windows, spec["input_digest"]]
    if not allow_listing_proposal:
        spec["allow_listing_proposal"] = False
        identity.append("inspect")
    key = "发布:" + listing_operations._digest(identity)
    while existing := conn.execute("""SELECT b.run_id, r.status FROM background_inspections b
                JOIN chat_group_runs r USING (run_id) WHERE b.user_id=? AND b.idempotency_key=?""",
                                   (user_id, key)).fetchone():
        if not recheck or existing["status"] in ("completed", "running"):
            break
        key = "发布:" + listing_operations._digest([key, existing["run_id"]])
    if existing:
        run_id, created = existing["run_id"], False
    else:
        if group["archived"] or chat_groups._running(conn, conversation_id):
            raise chat_groups.GroupConflict("工作群已归档或已有一轮正在运行")
        target = publication["target"]
        message = f"检查已发布的商品数据 · {target['shop_id']} / {target['marketplace_id']} / {target['sku']}"
        run_id = _insert(conn, user_id, conversation_id, key, message, _json(spec), spec, now, trace_id, request_id)
        created = True
    if allow_listing_proposal:
        conn.execute("""INSERT OR IGNORE INTO background_inspection_publications VALUES (?, ?, ?, ?, ?)""",
                     (*notification_key, run_id))
    return run_id, created


@contextmanager
def _read():
    with closing(database.get_db(read_only=True)) as conn:
        conn.execute("BEGIN")
        yield conn


def read(user_id, conversation_id, run_id, after=0, *, include_answer=False):
    with _read() as conn:
        row = conn.execute("""SELECT r.status, b.finalized, b.answer_id, b.spec FROM background_inspections b
             JOIN chat_group_runs r USING (run_id) JOIN chat_groups g USING (conversation_id)
             WHERE b.run_id=? AND g.conversation_id=? AND b.user_id=? AND g.user_id=?
             AND EXISTS (SELECT 1 FROM users WHERE id=?)""",
                           (run_id, conversation_id, user_id, user_id, user_id)).fetchone()
        if row is None:
            raise chat_groups.GroupNotFound
        events = [{"seq": event["seq"], "kind": event["kind"], **json.loads(event["payload"])}
                  for event in conn.execute("""SELECT seq, kind, payload FROM background_inspection_events
                      WHERE run_id=? AND seq>? ORDER BY seq""", (run_id, after))]
        result = {"run_id": run_id, "status": row["status"], "finalized": bool(row["finalized"]), "events": events}
        spec = json.loads(row["spec"])
        if publication := spec.get("publication"):
            if not publication_visible(conn, user_id, publication):
                raise chat_groups.GroupNotFound
            result["publication"] = publication
            result["windows"] = spec["windows"]
            result["input_sha256"] = spec["input_digest"]
        if include_answer and row["answer_id"] is not None:
            answer = conn.execute("SELECT id, content, details FROM chat_group_entries WHERE id=?",
                                  (row["answer_id"],)).fetchone()
            details = json.loads(answer["details"] or "{}")
            if not chat_groups._listing_visible(conn, user_id, details):
                raise chat_groups.GroupNotFound
            result["answer"] = {"entry_id": f"entry:{answer['id']}", "content": answer["content"]}
        return result


_PENDING = """FROM background_inspections b
    JOIN chat_group_runs r USING (run_id) JOIN chat_group_run_slots s USING (run_id)
    WHERE b.finalized=0 AND s.user_id=b.user_id AND s.released_ns IS NULL
    AND r.status IN ('running', 'stopped', 'superseded')
    AND (r.status!='running' OR json_extract(b.spec,'$.subscription_id') IS NULL OR EXISTS (
        SELECT 1 FROM inspection_subscriptions a JOIN chat_groups g USING(conversation_id)
        JOIN order_shop_access o ON (o.source,o.shop_id,o.user_id)=(a.source,a.shop_id,a.user_id)
        JOIN listing_test_shops l ON (l.shop_id,l.marketplace_id,l.user_id)=(a.shop_id,a.marketplace_id,a.user_id)
        JOIN listing_test_products p ON (p.shop_id,p.marketplace_id,p.sku)=(a.shop_id,a.marketplace_id,a.sku)
        WHERE a.subscription_id=json_extract(b.spec,'$.subscription_id') AND g.user_id=a.user_id AND NOT g.archived
    ))"""


def pending():
    with _read() as conn:
        return [(row["run_id"], row["generation"]) for row in conn.execute(
            "SELECT b.run_id, b.generation " + _PENDING + " ORDER BY s.acquired_ns, b.run_id")]


def claim(run_id, executor_id, trace_id):
    """Recheck eligibility while holding the process lock for this run."""
    with chat_groups._write() as conn:
        row = conn.execute("SELECT b.*, r.conversation_id " + _PENDING + " AND b.run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        job = {**dict(row), "generation": row["generation"] + 1, "executor_id": executor_id,
               "trace_id": trace_id, "spec": json.loads(row["spec"])}
        conn.execute("UPDATE background_inspections SET generation=?, executor_id=? WHERE run_id=?",
                     (job["generation"], executor_id, run_id))
        _emit_execution(conn, job, "claimed")
        return job


class StaleExecution(RuntimeError):
    pass


class Terminated(RuntimeError):
    pass


class AwaitingPublicationAccess(RuntimeError):
    pass


class Invalidated(RuntimeError):
    def __init__(self, items):
        self.items = items


def _fence(conn, job, *, cleanup=False):
    row = conn.execute("""SELECT b.generation, b.executor_id, b.finalized, b.user_id, r.conversation_id,
                          r.status, s.released_ns, s.user_id AS slot_owner FROM background_inspections b
                          JOIN chat_group_runs r USING (run_id) JOIN chat_group_run_slots s USING (run_id)
                          WHERE b.run_id=?""", (job["run_id"],)).fetchone()
    if row is None:
        raise StaleExecution("run_not_found")
    if row["generation"] != job["generation"]:
        raise StaleExecution("generation_mismatch")
    if not row["executor_id"] or row["executor_id"] != job.get("executor_id"):
        raise StaleExecution("executor_mismatch")
    if row["user_id"] != job["user_id"] or row["conversation_id"] != job["conversation_id"]:
        raise StaleExecution("execution_context_mismatch")
    if row["finalized"]:
        raise StaleExecution("run_finalized")
    if row["released_ns"] is not None or row["slot_owner"] != job["user_id"]:
        raise StaleExecution("slot_not_held")
    if row["status"] != "running":
        if row["status"] not in ("stopped", "superseded"):
            raise StaleExecution("run_not_executable")
        if not cleanup:
            raise Terminated
    return row["status"]


@contextmanager
def _write_execution(job, operation, *, cleanup=False):
    try:
        with chat_groups._write() as conn:
            yield conn, _fence(conn, job, cleanup=cleanup)
    except StaleExecution as error:
        trace_id = start_trace(source="background_inspection_fence")
        add_trace_step(trace_id, "background_inspection", "write_rejected", detail=operation,
                       status="error", error_code=str(error), runtime_call_id=job["run_id"],
                       evidence={key: job.get(key) for key in ("executor_id", "generation", "trace_id")})
        end_trace(trace_id, "rejected")
        raise


def _emit_execution(conn, job, kind, **payload):
    emit(conn, job["run_id"], kind, **payload, epoch_ns=time_ns(),
         **{key: job[key] for key in ("generation", "executor_id", "trace_id")})


def validate(conn, job):
    _fence(conn, job)
    spec = job["spec"]
    if "publication" in spec:
        owner, group = job["user_id"], job["conversation_id"]
        try:
            current = chat_groups._group(conn, owner, group)
        except chat_groups.GroupNotFound:
            raise Invalidated([{"source": "conversation_owner"}]) from None
        if current["archived"] or not publication_visible(conn, owner, spec["publication"]):
            if "subscription_id" in spec:
                raise AwaitingPublicationAccess
            raise Invalidated([{"source": "product_scope"}])
        inputs = publication_inputs(conn, owner, group, spec)
        if listing_operations._digest(inputs) != spec["input_digest"]:
            raise Invalidated([{"source": "publication_inputs"}])
        return inputs
    if items := invalid_items(conn, job["user_id"], job["conversation_id"], spec):
        raise Invalidated(items)


def prepare(job, step, *, tool=None, runtime_call_id=None):
    with _write_execution(job, "prepare") as (conn, _):
        row = conn.execute("""SELECT a.artifact_id, a.result FROM background_inspection_checkpoints c
                 JOIN background_inspection_artifacts a USING (artifact_id) WHERE c.run_id=? AND c.step=?""",
                           (job["run_id"], step)).fetchone()
        if row is not None:
            _emit_execution(conn, job, "reused", step=step, artifact_id=row["artifact_id"])
            return row["artifact_id"], json.loads(row["result"])
        metadata = {"tool": tool, "runtime_call_id": runtime_call_id} if tool else {}
        _emit_execution(conn, job, "step_started", step=step, **metadata)
        return None


def checkpoint(job, step, result, *, tool=None, runtime_call_id=None):
    artifact_id = uuid4().hex
    with _write_execution(job, "checkpoint") as (conn, _):
        conn.execute("INSERT INTO background_inspection_artifacts VALUES (?, ?, ?, ?)",
                     (artifact_id, job["run_id"], job["generation"],
                      _json({"result": result, "tool_call_id": runtime_call_id})))
        conn.execute("INSERT INTO background_inspection_checkpoints VALUES (?, ?, ?)",
                     (job["run_id"], step, artifact_id))
        metadata = {"tool": tool, "runtime_call_id": runtime_call_id} if tool else {}
        _emit_execution(conn, job, "step_completed", step=step, artifact_id=artifact_id, **metadata)
    return artifact_id


def finish(job, status, content=None, evidence=None, *, invalid=None):
    run = chat_groups.GroupRun(job["user_id"], job["conversation_id"], job["run_id"], job["trace_id"], 0)
    with _write_execution(job, "finish", cleanup=True) as (conn, current):
        entry_id = None
        if current != "running":
            status = current
        else:
            if status == "completed":
                validate(conn, job)
            if invalid:
                _emit_execution(conn, job, "invalidated", items=invalid)
            if content is not None:
                entry_id = run._entry(conn, "assistant", content, details=_json(evidence) if evidence else None)
                if evidence and evidence.get("proposal") and "publication" in job["spec"]:
                    listing_operations.create_group_proposal(conn, run, entry_id, evidence)
            run._finish(conn, status)
        released_ns = chat_groups.time_ns()
        released = conn.execute("""UPDATE chat_group_run_slots SET released_ns=?
            WHERE run_id=? AND released_ns IS NULL""", (released_ns, run.run_id)).rowcount
        conn.execute("UPDATE background_inspections SET finalized=1, answer_id=? WHERE run_id=?",
                     (entry_id, run.run_id))
        _emit_execution(conn, job, TERMINAL, status=status, answer_id=entry_id,
                        released_ns=released_ns if released else None)
    return status, released_ns if released else None
