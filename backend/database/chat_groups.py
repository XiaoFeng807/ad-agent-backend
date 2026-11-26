"""Persistent ordinary chat groups, with an owner-scoped legacy history."""

import json
import os
from contextlib import closing, contextmanager
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from time import time_ns
from uuid import uuid4

from backend.database import alert_review, chat_storage, diagnosis_pauses, listing_operations, mysql_diagnosis, orders
from backend.database import product_receipt_history, review_receipt_history, search_term_reports
from backend.observability import add_trace_step


TABLE_COLUMNS = {
    "chat_groups": ("conversation_id", "user_id", "title", "archived", "created_at"),
    "chat_group_runs": ("run_id", "conversation_id", "trace_id", "status"),
    "chat_group_entries": ("id", "run_id", "kind", "content", "priority", "details", "created_at"),
}


def _inflight_limits():
    value = os.getenv("STOREPILOT_GROUP_INFLIGHT")
    if value is None:
        return None
    user_limit, global_limit = map(int, value.split(":"))
    if min(user_limit, global_limit) < 0:
        raise ValueError("STOREPILOT_GROUP_INFLIGHT requires nonnegative user:global limits")
    return user_limit, global_limit


INFLIGHT_LIMITS = _inflight_limits()


def upgrade_sqlite(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_groups (
        conversation_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_group_runs (
        run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES chat_groups(conversation_id),
        trace_id TEXT NOT NULL, status TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_group_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES chat_group_runs(run_id),
        kind TEXT NOT NULL, content TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 1, details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_group_run_requests (
        user_id INTEGER NOT NULL, conversation_id TEXT NOT NULL REFERENCES chat_groups(conversation_id),
        action TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
        run_id TEXT NOT NULL UNIQUE REFERENCES chat_group_runs(run_id),
        shop_id TEXT, marketplace TEXT,
        PRIMARY KEY (user_id, conversation_id, action, request_key)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_group_date_proposals (
        proposal_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES chat_group_runs(run_id),
        request TEXT NOT NULL,
        payload TEXT NOT NULL,
        trace_id TEXT NOT NULL,
        replacement_run_id TEXT UNIQUE REFERENCES chat_group_runs(run_id),
        kind TEXT NOT NULL DEFAULT 'date_change'
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_group_date_proposals)")}
    if "windows" in columns:
        conn.execute("ALTER TABLE chat_group_date_proposals RENAME COLUMN windows TO payload")
    if "kind" not in columns:
        conn.execute("ALTER TABLE chat_group_date_proposals ADD COLUMN kind TEXT NOT NULL DEFAULT 'date_change'")
    conn.execute("CREATE INDEX IF NOT EXISTS chat_groups_owner ON chat_groups(user_id, archived)")
    conn.execute("CREATE INDEX IF NOT EXISTS chat_group_runs_group ON chat_group_runs(conversation_id, status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_group_run_slots (
        run_id TEXT PRIMARY KEY REFERENCES chat_group_runs(run_id), user_id INTEGER NOT NULL,
        acquired_ns INTEGER NOT NULL, released_ns INTEGER
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS chat_group_active_slots ON chat_group_run_slots(user_id)
                    WHERE released_ns IS NULL""")
    conn.execute("CREATE INDEX IF NOT EXISTS chat_group_entries_run ON chat_group_entries(run_id, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_group_pause_proposals (
        action_id TEXT PRIMARY KEY NOT NULL,
        conversation_id TEXT NOT NULL REFERENCES chat_groups(conversation_id),
        run_id TEXT NOT NULL REFERENCES chat_group_runs(run_id),
        entry_id INTEGER NOT NULL REFERENCES chat_group_entries(id),
        plan_id INTEGER NOT NULL,
        registration_id TEXT NOT NULL,
        plan_name TEXT NOT NULL,
        expected_status TEXT NOT NULL CHECK (expected_status = 'active'),
        reason TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        UNIQUE (entry_id, plan_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS chat_group_pause_group ON chat_group_pause_proposals(conversation_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_group_pause_receipts (
        receipt_id TEXT PRIMARY KEY NOT NULL,
        action_id TEXT NOT NULL UNIQUE REFERENCES chat_group_pause_proposals(action_id),
        execution_trace_id TEXT NOT NULL,
        committed_status TEXT NOT NULL CHECK (committed_status = 'paused'),
        approved_by_user_id INTEGER NOT NULL,
        approved_by_name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )""")
    conn.execute("""INSERT OR IGNORE INTO chat_groups (conversation_id, user_id, title)
        SELECT 'legacy-' || user_id, user_id, '历史聊天' FROM conversations
        WHERE user_id IS NOT NULL GROUP BY user_id""")
    from backend.database import background_inspections

    background_inspections.upgrade_sqlite(conn)


class GroupNotFound(LookupError):
    pass


class GroupConflict(RuntimeError):
    pass


class GroupCapacityExceeded(RuntimeError):
    pass


class GroupStorageUnavailable(RuntimeError):
    pass


def require_schema(conn):
    if mysql_diagnosis.is_mysql(conn):
        rows = conn.execute(
            "SELECT TABLE_NAME AS name FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"
        ).fetchall()
        if not TABLE_COLUMNS.keys() <= {row["name"] for row in rows}:
            raise GroupStorageUnavailable


def _legacy_id(user_id):
    return f"legacy-{user_id}"


@contextmanager
def _write():
    with closing(chat_storage.connect()) as conn, conn:
        require_schema(conn)
        if mysql_diagnosis.is_mysql(conn):
            conn.begin()
        else:
            conn.execute("BEGIN IMMEDIATE")
        yield conn


def _ensure_legacy(conn, user_id):
    # A legacy client can add the first messages after the schema upgrade.
    if conn.execute("SELECT 1 FROM conversations WHERE user_id=? LIMIT 1", (user_id,)).fetchone():
        sql = "INSERT INTO chat_groups (conversation_id, user_id, title) VALUES (?, ?, ?)"
        if mysql_diagnosis.is_mysql(conn):
            sql += " ON DUPLICATE KEY UPDATE conversation_id=chat_groups.conversation_id"
        else:
            sql += " ON CONFLICT(conversation_id) DO NOTHING"
        conn.execute(sql, (_legacy_id(user_id), user_id, "历史聊天"))


def _group(conn, user_id, conversation_id, *, lock=False):
    sql = "SELECT * FROM chat_groups WHERE user_id=? AND conversation_id=?"
    if lock and mysql_diagnosis.is_mysql(conn):
        sql += " FOR UPDATE"
    row = conn.execute(sql, (user_id, conversation_id)).fetchone()
    if row is None:
        raise GroupNotFound
    result = dict(row)
    result["archived"] = bool(result["archived"])
    return result


def create(user_id, title):
    conversation_id = uuid4().hex
    with _write() as conn:
        conn.execute("INSERT INTO chat_groups (conversation_id, user_id, title) VALUES (?, ?, ?)",
                     (conversation_id, user_id, title))
        return _group(conn, user_id, conversation_id)


def list_groups(user_id):
    with closing(chat_storage.connect(read_only=True)) as conn:
        require_schema(conn)
        needs_legacy = conn.execute(
            """SELECT 1 FROM conversations WHERE user_id=?
               AND NOT EXISTS (SELECT 1 FROM chat_groups WHERE conversation_id=?) LIMIT 1""",
            (user_id, _legacy_id(user_id)),
        ).fetchone()
    if needs_legacy:
        with _write() as conn:
            _ensure_legacy(conn, user_id)
    with closing(chat_storage.connect(read_only=True)) as conn:
        rows = conn.execute(
            "SELECT * FROM chat_groups WHERE user_id=? ORDER BY created_at DESC, conversation_id", (user_id,)
        ).fetchall()
        return [{**dict(row), "archived": bool(row["archived"])} for row in rows]


def _running(conn, conversation_id):
    return conn.execute(
        "SELECT run_id FROM chat_group_runs WHERE conversation_id=? AND status='running' LIMIT 1",
        (conversation_id,),
    ).fetchone() is not None


def set_archived(user_id, conversation_id, archived):
    with _write() as conn:
        group = _group(conn, user_id, conversation_id, lock=True)
        if archived and _running(conn, conversation_id):
            raise GroupConflict("请等待本轮结束后归档")
        conn.execute("UPDATE chat_groups SET archived=? WHERE conversation_id=? AND user_id=?",
                     (int(archived), conversation_id, user_id))
        return {**group, "archived": archived}


def _legacy_entries(conn, user_id, *, limit=None):
    columns, join = chat_storage.review_receipt_columns(conn, "c")
    sql = f"""SELECT c.id, c.role AS kind, c.content, c.priority, c.created_at, {columns}
              FROM conversations c {join} WHERE c.user_id=? AND c.role IN ('user', 'assistant')"""
    if limit is not None:
        rows = reversed(conn.execute(sql + " ORDER BY c.id DESC LIMIT ?", (user_id, limit)).fetchall())
    else:
        rows = conn.execute(sql + " ORDER BY c.id", (user_id,)).fetchall()
    entries = [dict(row) for row in rows]
    chat_storage.filter_review_receipts(conn, user_id, entries, role_key="kind")
    return entries


_ENTRIES = """FROM chat_group_entries e JOIN chat_group_runs r ON r.run_id=e.run_id
    WHERE r.conversation_id=?"""


def _olist_visible(conn, user_id, details):
    return (details.get("kind") != orders.OLIST_SALES_EVIDENCE
            or orders.has_access(conn, user_id, orders.OLIST, details["metrics"]["shop_id"]))


def _listing_visible(conn, user_id, details):
    kind = details.get("kind")
    candidates = ()
    if kind == listing_operations.KIND:
        candidates = details.get("candidates") or (details["target"],)
    elif kind in {listing_operations.SELECTION_KIND, listing_operations.INSPECTION_KIND, search_term_reports.INSPECTION_KIND}:
        candidates = details["candidates"]
    elif kind == search_term_reports.KIND:
        return search_term_reports.has_shop_access(conn, user_id, details["report"])
    return (all(not listing_operations.scope_error(conn, user_id, row["shop_id"], row["marketplace_id"], row["sku"])
                for row in candidates)
            and (kind not in {listing_operations.KIND, listing_operations.INSPECTION_KIND,
                              search_term_reports.INSPECTION_KIND}
                 or listing_operations.evidence_visible(conn, user_id, details)))


def _reply_visible(conn, user_id, content, details):
    return (_olist_visible(conn, user_id, details) and _listing_visible(conn, user_id, details)
            and review_receipt_history.visible(conn, user_id, content, details))


def _stored_reply_details(value):
    try:
        details = json.loads(value) if value else None
    except ValueError:
        return {}
    return details if isinstance(details, dict) else {}


def _sales_summary(metrics, *, olist):
    if not metrics:
        return None
    summary = {}
    for key in ("baseline", "current"):
        if key not in metrics:
            continue
        window = metrics[key]
        summary[key] = {"start_date": window["start_date"], "end_date": window["end_date"],
                        "units": (window["totals"] if olist else window)["units"]}
    change = metrics["change"]["units"] if olist else None
    return {**summary, "units_delta": change["absolute"] if olist else metrics.get("units_delta"),
            "units_change_pct": change["relative_pct"] if olist else metrics.get("units_change_pct")}


def history(user_id, conversation_id):
    with closing(chat_storage.connect(read_only=True)) as conn:
        require_schema(conn)
        if not mysql_diagnosis.is_mysql(conn):
            conn.execute("BEGIN")
        group = _group(conn, user_id, conversation_id)
        runs = [dict(row) for row in conn.execute(
            "SELECT run_id, trace_id, status FROM chat_group_runs WHERE conversation_id=?", (conversation_id,)
        ).fetchall()]
        if not mysql_diagnosis.is_mysql(conn):
            background_ids = {row[0] for row in conn.execute(
                "SELECT b.run_id FROM background_inspections b JOIN chat_group_runs r USING (run_id) "
                "WHERE r.conversation_id=?", (conversation_id,))}
            for run in runs:
                if run["run_id"] in background_ids:
                    run["background"] = True
                if run["status"] == "running":
                    run["can_change_dates"] = _review_request(conn, run["run_id"]) is not None
                    run["can_stop"] = True
        entries = []
        pauses = {} if mysql_diagnosis.is_mysql(conn) else diagnosis_pauses.group_pause_summaries(conn, conversation_id)
        listings = {} if mysql_diagnosis.is_mysql(conn) else listing_operations.group_proposals(conn, user_id, conversation_id)
        runs_by_id = {run["run_id"]: run for run in runs}
        if conversation_id == _legacy_id(user_id):
            entries.extend({**dict(row), "entry_id": f"legacy:{row['id']}"} for row in _legacy_entries(conn, user_id))
        rows = conn.execute(
            "SELECT e.*, r.trace_id " + _ENTRIES + " ORDER BY e.id", (conversation_id,)
        ).fetchall()
        replies = {row["id"]: _stored_reply_details(row["details"]) for row in rows if row["kind"] == "assistant"}
        hidden_reviews = {row["run_id"] for row in rows if row["id"] in replies
                          and not review_receipt_history.visible(conn, user_id, row["content"], replies[row["id"]])}
        hidden_listings = {row["run_id"] for row in rows if row["id"] in replies
                           and not _listing_visible(conn, user_id, replies[row["id"]])}
        for row in rows:
            entry = dict(row)
            details = entry.pop("details")
            if entry["kind"] == "stop":
                runs_by_id[entry["run_id"]]["stop"] = json.loads(details)
                continue
            if entry["run_id"] in hidden_reviews:
                if entry["kind"] == "worker":
                    continue
                if entry["kind"] == "assistant":
                    entries.append({**entry, "entry_id": f"entry:{entry['id']}",
                                    "content": review_receipt_history.UNAVAILABLE})
                    continue
            if entry["run_id"] in hidden_listings:
                if entry["kind"] == "worker":
                    continue
                if entry["kind"] == "assistant":
                    approved = [item for item in listings.get(entry["id"], ()) if item["state"] == "approved"]
                    entries.append({**entry, "entry_id": f"entry:{entry['id']}",
                                    "content": "当前商品或依据无法查看。",
                                    **({"listing_actions": approved} if approved else {})})
                    continue
            if decoded := replies.get(entry["id"]):
                kind = decoded.get("kind")
                if not _olist_visible(conn, user_id, decoded):
                    entries.append({**entry, "entry_id": f"entry:{entry['id']}",
                                    "content": "当前店铺访问权限已变化，无法查看该轮销量结果。"})
                    continue
                if kind == listing_operations.SELECTION_KIND:
                    entries.append({**entry, "entry_id": f"entry:{entry['id']}"})
                    continue
                if kind == listing_operations.KIND:
                    entry["evidence_kind"] = listing_operations.KIND
                    entry["listing_actions"] = listings.get(entry["id"], [])
                    entry["sales_summary"] = _sales_summary(decoded.get("metrics"), olist=False)
                    entry["evidence_gap_count"] = len(decoded.get("gaps") or [])
                    entry["has_product_details"] = bool(decoded.get("listing") or decoded.get("facts"))
                elif kind in {listing_operations.INSPECTION_KIND, search_term_reports.INSPECTION_KIND}:
                    entry["evidence_kind"] = kind
                elif kind == orders.OLIST_SALES_EVIDENCE:
                    entry["evidence_kind"] = kind
                    entry["sales_summary"] = _sales_summary(decoded["metrics"], olist=True)
                elif kind == search_term_reports.KIND:
                    entry["evidence_kind"] = kind
                    entry["search_terms"] = [{key: item[key] for key in ("id", "term", "action", "reason")}
                                             for item in decoded["items"]]
                elif proposals := pauses.get(entry["id"]):
                    entry["pause_actions"] = proposals
                entries.append({**entry, "entry_id": f"entry:{entry['id']}", "has_evidence": True})
                continue
            entries.append({**entry, "entry_id": f"entry:{entry['id']}",
                            **({"event": json.loads(details)} if details and entry["kind"] != "assistant" else {})})
        product_receipt_history.filter_receipts(conn, user_id, entries, role_key="kind")
        return {"conversation": group, "entries": entries, "runs": runs}


def read_reply_evidence(conn, user_id, conversation_id, run_id):
    _group(conn, user_id, conversation_id)
    row = conn.execute(
        "SELECT e.id, e.content, e.details " + _ENTRIES
        + " AND r.run_id=? AND r.status='completed' AND e.kind='assistant' AND e.details IS NOT NULL",
        (conversation_id, run_id),
    ).fetchone()
    if row is None:
        raise GroupNotFound
    details = _stored_reply_details(row["details"])
    if not details or not _reply_visible(conn, user_id, row["content"], details):
        raise GroupNotFound
    if details.get("kind") == search_term_reports.KIND:
        search_term_reports.normalize_legacy_metrics(details)
    return {"conversation_id": conversation_id, "run_id": run_id, "entry_id": f"entry:{row['id']}",
            **details}


def reply_evidence(user_id, conversation_id, run_id):
    with closing(chat_storage.connect(read_only=True)) as conn:
        require_schema(conn)
        if not mysql_diagnosis.is_mysql(conn):
            conn.execute("BEGIN")
        return read_reply_evidence(conn, user_id, conversation_id, run_id)


@dataclass(frozen=True)
class GroupRun:
    user_id: int
    conversation_id: str
    run_id: str
    trace_id: str
    message_id: int
    requested_at: datetime | None = None
    product_context: dict | None = None
    slot_acquired_ns: int | None = None
    check_progress_session: Callable | None = field(default=None, repr=False, compare=False)

    def _entry(self, conn, kind, content, priority=1, details=None):
        return conn.execute(
            "INSERT INTO chat_group_entries (run_id, kind, content, priority, details) VALUES (?, ?, ?, ?, ?)",
            (self.run_id, kind, content, priority, details),
        ).lastrowid

    def progress(self, kind, content, **event):
        with _write() as conn:
            _group(conn, self.user_id, self.conversation_id, lock=True)
            if not self._active(conn) and not (kind == "reclaimed" and conn.execute(
                "SELECT 1 FROM chat_group_runs WHERE conversation_id=? AND run_id=? AND status IN ('superseded', 'stopped')",
                (self.conversation_id, self.run_id),
            ).fetchone()):
                return
            if self.check_progress_session is not None and not self.check_progress_session(conn):
                return
            self._entry(conn, "worker", content, details=json.dumps(
                {"kind": kind, **event, "run_id": self.run_id}, ensure_ascii=False))

    def save_reply(self, content, priority, *, evidence=None, validate=None):
        with _write() as conn:
            _group(conn, self.user_id, self.conversation_id, lock=True)
            if not self._active(conn):
                raise GroupConflict("工作群运行已结束")
            if validate is not None:
                validate(conn)
            if evidence and not _olist_visible(conn, self.user_id, evidence):
                evidence = None
                content = "当前店铺访问权限已变化，本轮销量结果未提交。"
            if evidence and not _listing_visible(conn, self.user_id, evidence):
                add_trace_step(self.trace_id, "listing_proposal", "blocked", error_code="listing_not_found",
                               evidence={"run_id": self.run_id})
                evidence = None
                content = "当前店铺访问权限已变化，本轮商品结果未提交。"
            if evidence and (receipt := evidence.get("alert_review")) and receipt["associations"]:
                from backend.agent.alert_review import finish_without_scope, recheck_receipt

                latest = recheck_receipt(conn, self.user_id, receipt)
                if latest != receipt:
                    evidence = {}
                    content = finish_without_scope(latest, self.trace_id, evidence_sink=evidence.update)
            if evidence and evidence.get("kind") == search_term_reports.INSPECTION_KIND:
                from backend.agent.operations_inspection import commit_result

                content, evidence = commit_result(conn, self, content, evidence)
            message_id = self._entry(conn, "assistant", content, priority,
                                     details=json.dumps(evidence, ensure_ascii=False) if evidence else None)
            if evidence and evidence.get("kind") == listing_operations.KIND:
                try:
                    listing_operations.create_group_proposal(conn, self, message_id, evidence)
                except (listing_operations.ListingConflict, listing_operations.ListingNotFound) as error:
                    code = str(error) or "listing_not_found"
                    add_trace_step(self.trace_id, "listing_proposal", "blocked", error_code=code,
                                   evidence={"run_id": self.run_id})
                    content = "商品或证据已变化，本轮提案未提交。请重新取证后生成提案。"
                    conn.execute("UPDATE chat_group_entries SET content=?, details=NULL WHERE id=?",
                                 (content, message_id))
            elif evidence and evidence.get("kind") not in (orders.OLIST_SALES_EVIDENCE, listing_operations.SELECTION_KIND,
                                                           listing_operations.INSPECTION_KIND, search_term_reports.KIND,
                                                           search_term_reports.INSPECTION_KIND):
                diagnosis_pauses.create_group_pause_proposals(conn, self, message_id, evidence)
            self._finish(conn, "completed")
            return message_id, content

    def finish(self, status):
        with _write() as conn:
            _group(conn, self.user_id, self.conversation_id, lock=True)
            self._finish(conn, status)

    def drain(self, unfinished_status="interrupted"):
        """Close a never-started or interrupted run and release its slot after execution exits."""
        with _write() as conn:
            self._finish(conn, unfinished_status)
            released_ns = time_ns()
            released = conn.execute(
                """UPDATE chat_group_run_slots SET released_ns=?
                   WHERE run_id=? AND user_id=? AND released_ns IS NULL""",
                (released_ns, self.run_id, self.user_id),
            ).rowcount
            status = conn.execute("SELECT status FROM chat_group_runs WHERE run_id=?", (self.run_id,)).fetchone()["status"]
        return status, released_ns if released else None

    def _finish(self, conn, status):
        conn.execute(
            "UPDATE chat_group_runs SET status=? WHERE conversation_id=? AND run_id=? AND status='running'",
            (status, self.conversation_id, self.run_id),
        )

    def _active(self, conn):
        return conn.execute(
            "SELECT 1 FROM chat_group_runs WHERE conversation_id=? AND run_id=? AND status='running'",
            (self.conversation_id, self.run_id),
        ).fetchone() is not None

    def status(self):
        with closing(chat_storage.connect(read_only=True)) as conn:
            return conn.execute(
                "SELECT status FROM chat_group_runs WHERE conversation_id=? AND run_id=?",
                (self.conversation_id, self.run_id),
            ).fetchone()["status"]


def _accept(conn, user_id, conversation_id, trace_id, message, priority, *, background=False, requested_at=None):
    requested_at = requested_at or alert_review.now()
    run_id = uuid4().hex
    track_slot = (background or INFLIGHT_LIMITS is not None) and not mysql_diagnosis.is_mysql(conn)
    if track_slot:
        user_limit, global_limit = INFLIGHT_LIMITS or (0, 0)
        counts = conn.execute(
            "SELECT count(*) AS total, count(CASE WHEN user_id=? THEN 1 END) AS owned "
            "FROM chat_group_run_slots WHERE released_ns IS NULL", (user_id,),
        ).fetchone()
        if user_limit and counts["owned"] >= user_limit:
            raise GroupCapacityExceeded("account_inflight_exceeded")
        if global_limit and counts["total"] >= global_limit:
            raise GroupCapacityExceeded("global_inflight_exceeded")
    rows = conn.execute(
            "SELECT e.kind, e.content, e.priority, e.details, r.status " + _ENTRIES
            + " AND r.status NOT IN ('superseded', 'stopped') AND e.kind IN ('user', 'assistant') ORDER BY e.id DESC LIMIT 49",
            (conversation_id,),
        ).fetchall()
    prior = []
    for row in reversed(rows):
        details = _stored_reply_details(row["details"])
        if _olist_visible(conn, user_id, details) and _listing_visible(conn, user_id, details):
            entry = dict(row)
            if row["kind"] == "assistant" and not review_receipt_history.visible(conn, user_id, row["content"], details):
                entry.update(content=review_receipt_history.UNAVAILABLE, details=None)
            prior.append(entry)
    product_context = None
    for row in reversed(prior):
        if row["kind"] != "assistant" or row["status"] != "completed" or not row["details"]:
            continue
        details = _stored_reply_details(row["details"])
        kind = details.get("kind")
        if kind == listing_operations.KIND and details.get("target"):
            product_context = {"kind": kind, "target": details["target"]}
            if details.get("decline_request"):
                product_context.update(decline_request=details["decline_request"], statistics_date=details["statistics_date"])
        elif kind == listing_operations.SELECTION_KIND:
            product_context = details
        elif kind == orders.OLIST_SALES_EVIDENCE:
            product_context = {"kind": kind, **{key: details["metrics"][key] for key in ("shop_id", "product_id")}}
        if product_context:
            break
    if conversation_id == _legacy_id(user_id) and len(prior) < 49:
        prior = _legacy_entries(conn, user_id, limit=49 - len(prior)) + prior
    context = [{"role": row["kind"], "content": row["content"] or "", "priority": row["priority"]} for row in prior]
    if not mysql_diagnosis.is_mysql(conn):
        product_receipt_history.filter_choices(conn, user_id, context)
    context.append({"role": "user", "content": message, "priority": priority})
    conn.execute(
        "INSERT INTO chat_group_runs (run_id, conversation_id, trace_id, status) VALUES (?, ?, ?, 'running')",
        (run_id, conversation_id, trace_id),
    )
    message_id = GroupRun(user_id, conversation_id, run_id, trace_id, 0)._entry(conn, "user", message, priority)
    acquired_ns = None
    if track_slot:
        acquired_ns = time_ns()
        conn.execute("INSERT INTO chat_group_run_slots (run_id, user_id, acquired_ns) VALUES (?, ?, ?)",
                     (run_id, user_id, acquired_ns))
    return GroupRun(user_id, conversation_id, run_id, trace_id, message_id, requested_at, product_context, acquired_ns), context


@dataclass(frozen=True)
class RunRequest:
    action: str
    key: str
    fingerprint: str

    @classmethod
    def from_payload(cls, action, key, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        return cls(action, key, sha256(encoded).hexdigest())

    def replay(self, conn, user_id, conversation_id):
        # Call while holding the same group/write lock used for new admission.
        row = conn.execute(
            """SELECT r.run_id, r.trace_id, r.status, q.fingerprint, q.shop_id, q.marketplace
               FROM chat_group_run_requests q JOIN chat_group_runs r
               ON r.run_id=q.run_id AND r.conversation_id=q.conversation_id
               WHERE q.user_id=? AND q.conversation_id=? AND q.action=? AND q.request_key=?""",
            (user_id, conversation_id, self.action, self.key),
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != self.fingerprint:
            raise GroupConflict("idempotency_key_conflict")
        return {key: row[key] for key in ("run_id", "trace_id", "status", "shop_id", "marketplace")}

    def record(self, conn, run, *, shop_id=None, marketplace=None):
        conn.execute(
            """INSERT INTO chat_group_run_requests
               (user_id, conversation_id, action, request_key, fingerprint, run_id, shop_id, marketplace)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run.user_id, run.conversation_id, self.action, self.key, self.fingerprint,
             run.run_id, shop_id, marketplace),
        )


def _accept_new(conn, group, trace_id, message, priority):
    if group["archived"]:
        raise GroupConflict("请先恢复归档工作群")
    if _running(conn, group["conversation_id"]):
        raise GroupConflict("此工作群已有一轮正在运行")
    return _accept(conn, group["user_id"], group["conversation_id"], trace_id, message, priority)


def accept(user_id, conversation_id, trace_id, message, priority, *, identity=None, authorize=None):
    with _write() as conn:
        group = _group(conn, user_id, conversation_id, lock=True)
        if authorize is not None:
            authorize(conn)
        if identity is not None and (replay := identity.replay(conn, user_id, conversation_id)):
            if replay["status"] == "completed":
                row = conn.execute(
                    "SELECT content, details FROM chat_group_entries WHERE run_id=? AND kind='assistant'",
                    (replay["run_id"],),
                ).fetchone()
                if row and not _reply_visible(conn, user_id, row["content"], _stored_reply_details(row["details"])):
                    raise GroupNotFound
            return None, {key: replay[key] for key in ("run_id", "trace_id", "status")}
        run, context = _accept_new(conn, group, trace_id, message, priority)
        if identity is not None:
            identity.record(conn, run)
        return run, context


def _review_request(conn, run_id):
    from backend.agent.conditional_detail import parse_detail_condition
    from backend.agent.multi_window_review import parse_review_goal

    row = conn.execute("SELECT content, priority FROM chat_group_entries WHERE run_id=? AND kind='user'",
                       (run_id,)).fetchone()
    if row and parse_detail_condition(row["content"])[1] is None:
        _, goal = parse_review_goal(row["content"])
        if goal:
            return row["content"], row["priority"], goal
    return None


def _change_run(conn, user_id, conversation_id, run_id):
    if mysql_diagnosis.is_mysql(conn):
        raise GroupStorageUnavailable
    _group(conn, user_id, conversation_id)
    row = conn.execute("SELECT trace_id, status FROM chat_group_runs WHERE conversation_id=? AND run_id=?",
                       (conversation_id, run_id)).fetchone()
    if row is None:
        raise GroupNotFound
    return row


def _require_running(row):
    if row["status"] != "running":
        raise GroupConflict("本轮已结束，请发起新一轮" if row["status"] != "superseded" else "本轮已被替换，请刷新工作群")


def stop(user_id, conversation_id, run_id):
    with _write() as conn:
        row = _change_run(conn, user_id, conversation_id, run_id)
        if row["status"] == "stopped":
            entry = conn.execute("SELECT details FROM chat_group_entries WHERE run_id=? AND kind='stop'",
                                 (run_id,)).fetchone()
            return json.loads(entry["details"])
        if row["status"] == "completed":
            raise GroupConflict("本轮答案已提交，可刷新工作群查看")
        _require_running(row)
        user = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise GroupNotFound
        run = GroupRun(user_id, conversation_id, run_id, row["trace_id"], 0)
        record = {"conversation_id": conversation_id, "run_id": run_id, "trace_id": run.trace_id,
                  "stopped_by_user_id": user_id, "stopped_by_name": user["username"],
                  "stopped_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        run._finish(conn, "stopped")
        run._entry(conn, "stop", "已停止", details=json.dumps(record, ensure_ascii=False))
        return record


def change_scope(user_id, conversation_id, run_id):
    with closing(chat_storage.connect(read_only=True)) as conn:
        if not mysql_diagnosis.is_mysql(conn):
            conn.execute("BEGIN")
        row = _change_run(conn, user_id, conversation_id, run_id)
        _require_running(row)
        review = _review_request(conn, run_id)
        if review is None:
            raise GroupConflict("本轮没有可修改的基准窗和当前窗")
        return review[2], row["trace_id"]


def _apply_change(conn, user_id, message, goal, change):
    from backend.agent.group_date_change import InvalidDateChange, InvalidPlanChange, PlanReplacement, ReviewDates, revised_request

    if isinstance(change, PlanReplacement):
        plan_ids = change.apply(goal)
        checked = (*goal.plan_ids, change.add_plan_id)
        owned = conn.execute(
            "SELECT id FROM ad_plans WHERE user_id=? AND id IN (?, ?, ?)", (user_id, *checked),
        ).fetchall()
        if len(owned) != len(checked):
            raise InvalidPlanChange("计划不可用于本次替换")
        before, after = ("、".join(f"#{plan}" for plan in pair) for pair in (goal.plan_ids, plan_ids))
        return change.revise(message, goal, plan_ids), {
            "kind": "plan_change", "old_plan_ids": list(goal.plan_ids), "new_plan_ids": list(plan_ids),
            **change.model_dump(),
        }, f"已替换旧轮。{before} → {after}"
    old_dates = ReviewDates(baseline=goal.baseline, current=goal.current)
    if change == old_dates:
        raise InvalidDateChange("请提供与本轮不同的日期")
    return revised_request(message, goal, change), {
        "kind": "date_change", "old_dates": old_dates.model_dump(), "new_dates": change.model_dump(),
    }, f"已替换旧轮。{old_dates.label()} → {change.label()}"


def propose_change(user_id, conversation_id, run_id, request, change, trace_id):
    with _write() as conn:
        _require_running(_change_run(conn, user_id, conversation_id, run_id))
        message, _, goal = _review_request(conn, run_id)
        _, preview, _ = _apply_change(conn, user_id, message, goal, change)
        proposal_id = uuid4().hex
        conn.execute(
            """INSERT INTO chat_group_date_proposals (proposal_id, run_id, request, payload, trace_id, kind)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (proposal_id, run_id, request, change.model_dump_json(), trace_id, preview["kind"]),
        )
        return {"proposal_id": proposal_id, **preview}


def confirm_change(user_id, conversation_id, run_id, proposal_id, trace_id, *, dates_only=False):
    from backend.agent.group_date_change import PlanReplacement, ReviewDates

    with _write() as conn:
        old = _change_run(conn, user_id, conversation_id, run_id)
        proposal = conn.execute(
            "SELECT * FROM chat_group_date_proposals WHERE proposal_id=? AND run_id=?", (proposal_id, run_id),
        ).fetchone()
        if proposal is None or (dates_only and proposal["kind"] != "date_change"):
            raise GroupNotFound
        replacement_id = proposal["replacement_run_id"]
        if not replacement_id:
            _require_running(old)
        message, priority, goal = _review_request(conn, run_id)
        model = PlanReplacement if proposal["kind"] == "plan_change" else ReviewDates
        revised, preview, summary = _apply_change(conn, user_id, message, goal, model.model_validate_json(proposal["payload"]))
        if replacement_id:
            replacement = _change_run(conn, user_id, conversation_id, replacement_id)
            return None, None, {"run_id": replacement_id, "trace_id": replacement["trace_id"],
                                "status": replacement["status"]}
        previous = GroupRun(user_id, conversation_id, run_id, old["trace_id"], 0)
        previous._finish(conn, "superseded")
        replacement, context = _accept(conn, user_id, conversation_id, trace_id, revised, priority)
        conn.execute("UPDATE chat_group_date_proposals SET replacement_run_id=? WHERE proposal_id=?",
                     (replacement.run_id, proposal_id))
        change = {**preview, "request": proposal["request"], "proposal_id": proposal_id,
                  "proposal_trace_id": proposal["trace_id"], "old_run_id": run_id, "old_trace_id": old["trace_id"],
                  "new_run_id": replacement.run_id, "new_trace_id": trace_id}
        previous._entry(conn, "change", summary, details=json.dumps(change, ensure_ascii=False))
        return replacement, context, change


def committed_turn(conn, user_id, conversation_id, user_message_id, assistant_message_id, *, visible_only=False):
    row = conn.execute(
        """SELECT q.content AS question, a.content AS answer, a.details FROM chat_group_entries q
           JOIN chat_group_entries a ON a.run_id=q.run_id
           JOIN chat_group_runs r ON r.run_id=q.run_id
           JOIN chat_groups g ON g.conversation_id=r.conversation_id
           WHERE g.user_id=? AND g.conversation_id=? AND r.status='completed'
             AND q.id=? AND q.kind='user' AND a.id=? AND a.kind='assistant'""",
        (user_id, conversation_id, user_message_id, assistant_message_id),
    ).fetchone()
    if row and _olist_visible(conn, user_id, (details := _stored_reply_details(row["details"]))):
        if visible_only and not review_receipt_history.visible(
            conn, user_id, row["answer"], details,
        ):
            return None
        return row["question"], row["answer"]
    return None
