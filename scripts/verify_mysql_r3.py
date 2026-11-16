"""Run the ordinary chat contract against isolated R2 copies over loopback HTTP."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock, Thread, current_thread
from time import monotonic, monotonic_ns, sleep
from unittest.mock import patch

import httpx
import pymysql

from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
from backend.agent.tests.test_home_sse_tracing import FixedModel, tool_call
from backend.agent.tests.test_plan_metrics import synthetic_artifact
from backend.agent.tests.test_team_runtime import task
from backend.database.database import DEFAULT_DB_PATH, hash_password
from scripts.eval_review_retrieval import FixedReviewEmbedding
from scripts.migrate_mysql_r2 import DDL, import_copy, source_digest
from scripts.verify_mysql_r1 import dump, headers, login, scenario
from scripts.verify_mysql_r2 import runtime_env
from scripts.verify_mysql_t0 import _change, _query, isolated_mysql


OWNER, OTHER = 17, 18
PLAN, OTHER_PLAN = 101, 201
LATE_LOW = ("R3_LATE_LOW_QUESTION", "R3_LATE_LOW_ANSWER")
STREAM, HISTORY = "/api/chat/stream", "/api/chat/history"
CASES = (
    "direct_and_owner_scope", "model_delegation", "all_advertised_reads", "history_and_sessions",
    "snapshot_late_ids_and_reverse_completion", "reply_commit_rollback", "chroma_write_failure",
    "restart_recall", "cross_instance_and_forged_receipts",
)
READS = {
    "get_dashboard_data": {}, "get_daily_trend": {"days": 7}, "get_plans_summary": {},
    "get_alerts": {"unread_only": True}, "get_account_detail": {"account_id": 701},
    "compare_plans": {"plan_id_1": PLAN, "plan_id_2": PLAN}, "get_week_over_week": {},
    "get_activity_timeline": {}, "get_decision_summary": {}, "get_hot_products": {},
    "get_daily_report_by_date": {"start_date": "2026-10-01", "end_date": "2026-10-07"},
    "get_plan_metrics_by_date": {"plan_id": PLAN, "start_date": "2026-10-01", "end_date": "2026-10-02"},
    "search_knowledge": {"query": "点击率下降复核 v1"},
}


class R3Embedding(FixedReviewEmbedding):
    @staticmethod
    def name():
        return "mysql-r3-fixed-embedding"

    @staticmethod
    def build_from_config(config):
        return R3Embedding()

    def __call__(self, input):
        return [[1.0, 0.0] if "R3_direct" in text or LATE_LOW[0] in text else [0.0, 1.0] for text in input]


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def wait_for(predicate):
    deadline = monotonic() + 20
    while monotonic() < deadline:
        if value := predicate():
            return value
        sleep(0.025)
    raise TimeoutError("R3 barrier timed out")


class R3Model(FixedModel):
    def __init__(self, emit):
        super().__init__()
        self.emit = emit

    def create(self, messages, tools=None, response_format=None, **kwargs):
        latest = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        receipts = {message["tool_call_id"]: json.loads(message["content"])
                    for message in messages if message["role"] == "tool"}
        role = "worker" if response_format else "main" if tools else "auxiliary"
        self.emit("model_input", role=role, payload={
            "messages": messages, "tools": tools, "response_format": response_format, **kwargs,
        })
        if role == "worker":
            return (worker_summary("R3_WORKER_READ_COMPLETE") if receipts else
                    reply("", tool_call("worker-owned", name="get_plan_detail", plan_id=PLAN)))
        if role == "auxiliary" and "任务规划专家" in messages[0]["content"]:
            return reply('{"steps":[]}')
        if role == "main":
            if latest == "R3_direct" and not receipts:
                return reply("", tool_call("owned", name="get_plan_detail", plan_id=PLAN),
                             tool_call("foreign", name="get_plan_detail", plan_id=OTHER_PLAN),
                             tool_call("spoof", name="get_plan_detail", plan_id=OTHER_PLAN, user_id=OTHER),
                             tool_call("legacy", name="query_data", query="forbidden"))
            if latest in {"R3_early", "R3_recall", "R3_forged", "R3_other"} and "memory" not in receipts:
                return reply("", tool_call("memory", name="search_conversation_memory", query="R3_direct",
                                           user_id=OTHER, through_message_id=2**63 - 1))
            if latest in {"R3_delegation", "R3_early"} and "dispatch" not in receipts:
                refs = [receipts["prior"]["artifact_id"]] if "prior" in receipts else []
                if latest == "R3_early" and not refs:
                    return reply("", tool_call("prior", name="get_plan_detail", plan_id=PLAN))
                return reply("", dispatch("dispatch", {**task("lookup"), "direct_refs": refs}))
            if latest.startswith("R3_reads") and not receipts:
                group = list(READS.items())
                group = group[:7] if latest.endswith("1") else group[7:]
                return reply("", *(tool_call(name, name=name, **args) for name, args in group))
        return reply(latest.replace("R3_", "R3_REPLY_", 1))


def serve(port, directory):
    import uvicorn
    from chromadb.api.models.Collection import Collection
    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.config.config import settings
    from backend.database import chat_storage, mysql_diagnosis
    from backend.memory import conversation_memory as memory
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import seed_review_rules

    case = ContextVar("r3_case", default="")
    lock = Lock()
    sequence = 0
    probes = directory / f"probes-{os.getpid()}.jsonl"
    trace_path = directory / f"traces-{os.getpid()}.jsonl"
    app_reader = dict(host="127.0.0.1", port=settings.DIAGNOSIS_MYSQL_PORT,
                      user=mysql_diagnosis.USER, password=settings.DIAGNOSIS_MYSQL_PASSWORD.get_secret_value(),
                      database=mysql_diagnosis.schema_name(), cursorclass=pymysql.cursors.DictCursor)

    def emit(probe, **data):
        nonlocal sequence
        with lock, probes.open("a", encoding="utf-8") as stream:
            sequence += 1
            stream.write(json.dumps({"probe": probe, "case": case.get(), "thread": current_thread().name,
                                     "sequence": sequence, "tick_ns": monotonic_ns(), **data},
                                    ensure_ascii=False) + "\n")

    def read_chat(view):
        return _query(lambda: pymysql.connect(**app_reader),
                      "SELECT CONNECTION_ID() AS reader, id, user_id, role, content, chat_view_id "
                      "FROM conversations WHERE chat_view_id=%s ORDER BY id", (view,))

    original_connect = sqlite3.connect

    def sqlite_guard(file, *args, **kwargs):
        allowed = (settings.PLAN_METRICS_DB_PATH and kwargs.get("uri")
                   and file == Path(settings.PLAN_METRICS_DB_PATH).resolve().as_uri() + "?mode=ro")
        emit("sqlite_python_open", source="public_metrics" if allowed else "blocked_business", allowed=bool(allowed))
        if not allowed:
            raise AssertionError("Unexpected Python SQLite open")
        return original_connect(file, *args, **kwargs)

    connection = mysql_diagnosis.MySQLDiagnosisConnection
    execute, commit = connection.execute, connection.commit

    def observe_sql(self, sql, params=()):
        cursor = execute(self, sql, params)
        if sql.startswith("INSERT INTO conversations"):
            self.r3_message = (params[1], cursor.lastrowid)
        elif sql.startswith("START TRANSACTION WITH CONSISTENT SNAPSHOT"):
            emit("snapshot_open", connection=self._connection.thread_id())
        elif sql.startswith("SELECT") and any(f" {table} " in sql + " " for table in (
                "ad_plans", "ad_accounts", "daily_reports", "alerts", "decision_logs",
                "plan_metric_bindings", "conversations")):
            emit("sql_read", sql=sql, params=params, rows=cursor.rowcount, connection=self._connection.thread_id())
        return cursor

    def observe_commit(self):
        message = getattr(self, "r3_message", None)
        if message and message[0] == "assistant" and case.get() == "commit-fail":
            emit("reply_commit_fault", id=message[1], writer=self._connection.thread_id())
            raise mysql_diagnosis.MySQLStorageError("Injected failure before reply commit")
        commit(self)
        if message:
            emit("message_commit", role=message[0], id=message[1], writer=self._connection.thread_id())

    def history(conn, *args, **kwargs):
        if case.get() == "early":
            (directory / "early-paused").touch()
            emit("early_paused_before_history", before=kwargs["before"])
            wait_for((directory / "early-release").exists)
        return read_messages(conn, *args, **kwargs)

    def add(self, *args, **kwargs):
        if self.name != "conversations":
            return add_vector(self, *args, **kwargs)
        view = case.get()
        rows = read_chat(view)
        assert any(row["role"] == "assistant" for row in rows)
        emit("before_chroma_add", sql=rows, metadatas=kwargs["metadatas"])
        if view == "memory-fail":
            emit("chroma_add_fault")
            raise RuntimeError("Injected Chroma add failure")
        result = add_vector(self, *args, **kwargs)
        emit("chroma_added", ids=kwargs["ids"])
        return result

    def index(user_id, user_msg, ai_reply, **kwargs):
        if case.get() == "late":
            low = json.loads((directory / "late-low-ids.json").read_text(encoding="utf-8"))
            store(user_id, *LATE_LOW, user_message_id=low[0], assistant_message_id=low[1])
        store(user_id, user_msg, ai_reply, **kwargs)

    def query(self, *args, **kwargs):
        result = query_vector(self, *args, **kwargs)
        if self.name != "conversations":
            return result
        emit("chroma_query", where=kwargs["where"], matches=result["ids"][0],
             documents=result["documents"][0], metadata=result["metadatas"][0])
        if case.get() == "forged":
            candidates = json.loads((directory / "forged-candidates.json").read_text(encoding="utf-8"))
            emit("injected_vector_receipts", receipts=candidates)
            # Simulate a backend returning hits outside its filter. Keep any real hits.
            for item in candidates:
                result["documents"][0].insert(0, item["document"])
                result["metadatas"][0].insert(0, item["metadata"])
                result["distances"][0].insert(0, 0.0)
        return result

    async def observe(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        view = dict(scope["headers"]).get(b"x-r3-case", b"").decode()
        token = case.set(view)

        async def checked_send(message):
            if message["type"] == "http.response.start":
                message["headers"].append((b"x-r3-pid", str(os.getpid()).encode()))
            if message["type"] == "http.response.body" and message.get("body", b"").startswith(b"data: "):
                event = json.loads(message["body"][6:])
                if event["type"] in {"done", "error"}:
                    rows = read_chat(view)
                    emit("before_sse_terminal", event=event, sql=rows)
            await send(message)

        try:
            await app(scope, receive, checked_send)
        finally:
            case.reset(token)

    observability.TRACE_LOG_FILE = trace_path
    observability.FUNC_LOG_FILE = directory / f"functions-{os.getpid()}.jsonl"
    observability.ERROR_LOG_FILE = directory / f"errors-{os.getpid()}.jsonl"
    read_messages = chat_storage.read_messages
    store = memory.store_conversation
    add_vector, query_vector = Collection.add, Collection.query
    with (patch.object(sqlite3, "connect", sqlite_guard),
          patch.object(connection, "execute", observe_sql), patch.object(connection, "commit", observe_commit),
          patch.object(chat_storage, "read_messages", history),
          patch.object(ProviderFactory, "create", return_value=R3Model(emit)),
          patch.object(memory, "BGEEmbedding", R3Embedding),
          patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding),
          patch.object(memory, "store_conversation", index),
          patch.object(Collection, "add", add), patch.object(Collection, "query", query)):
        seed_review_rules(rag_knowledge.get_rag_knowledge())
        from fast_server import app
        server = uvicorn.Server(uvicorn.Config(observe, host="127.0.0.1", port=port,
                                               log_level="warning", access_log=False, lifespan="on"))

        def stop():
            sys.stdin.readline()
            server.should_exit = True

        Thread(target=stop, daemon=True).start()
        server.run()
        return int(not server.started)


@contextmanager
def api_process(env, directory):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log = directory / f"api-{port}.log"
    with log.open("wb") as output:
        proc = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "scripts.verify_mysql_r3",
                                 "--serve", str(port), "--output-dir", str(directory)],
                                env=env, stdin=subprocess.PIPE, stdout=output, stderr=output,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=35, trust_env=False) as client:
                def ready():
                    if proc.poll() is not None:
                        raise RuntimeError(f"FastAPI startup failed. See {log.name}")
                    try:
                        response = client.get("/api/health", timeout=0.5)
                        return response if response.status_code == 200 else None
                    except httpx.TransportError:
                        return None

                response = wait_for(ready)
                yield client, int(response.headers["x-r3-pid"])
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def fixture(connect, source, artifact, manifest, scratch, memory_root):
    imported = import_copy(source, connect)
    assert imported["status"] == "imported"
    scratch.mkdir()
    env = runtime_env(connect, artifact, scratch)
    env["MYSQL_CHAT_MEMORY_ROOT"] = str(memory_root)
    password = token_urlsafe(24)
    with closing(connect()) as conn, conn.cursor() as cursor:
        cursor.executemany("UPDATE users SET username=%s, password=%s WHERE id=%s",
                           [("owner-r3", hash_password(password), OWNER), ("other-r3", hash_password(password), OTHER)])
        cursor.execute("INSERT INTO ad_accounts (id, account_name, platform, user_id) "
                       "VALUES (701,'R3_OWN_ACCOUNT','synthetic',17)")
        cursor.execute("UPDATE ad_plans SET account_id=701, plan_name='R3_OWN_PLAN' WHERE id=101 AND user_id=17")
        assert cursor.rowcount == 1
        cursor.execute("UPDATE ad_plans SET plan_name='R3_OTHER_PLAN' WHERE id=201 AND user_id=18")
        assert cursor.rowcount == 1
        cursor.execute("UPDATE plan_metric_bindings SET source_id=%s, data_version=%s, source_plan_id='fixture-campaign' "
                       "WHERE plan_id=101", (manifest["source_id"], manifest["data_version"]))
        cursor.execute("INSERT INTO daily_reports (report_date, cost, impressions, clicks, sales, user_id) "
                       "VALUES ('2026-10-01', 10, 100, 10, 20, 17), ('2026-10-01', 90, 900, 90, 90, 18)")
        cursor.execute("INSERT INTO alerts (message, user_id) VALUES ('R3_OWN_ALERT',17), ('R3_OTHER_ALERT',18)")
        cursor.execute("GRANT INSERT ON mysql_r2.conversations TO 'mysql_r1_app'@'localhost'")
        conn.commit()
    facts, = _query(connect, "SELECT @@server_uuid AS uuid, VERSION() AS version, DATABASE() AS db")
    return env, password, facts


def chat(client, auth, view, *, expected="done"):
    with client.stream("POST", STREAM, headers={**auth, "x-r3-case": view}, json={
        "message": f"R3_{view}", "chat_view_id": view, "user_id": OTHER, "through_message_id": 2**63 - 1,
    }) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
    assert [event["type"] for event in events] == (["thinking", "text", "done"] if expected == "done" else
                                                   ["thinking", "error"]), events
    assert events[0]["trace_id"] == events[-1]["trace_id"]
    return events


def probes(directory, pid, kind, view=None):
    return [probe for probe in lines(directory / f"probes-{pid}.jsonl")
            if probe["probe"] == kind and (view is None or probe["case"] == view)]


def trace(directory, pid, events):
    key = events[-1]["trace_id"]
    row, = [item for item in lines(directory / f"traces-{pid}.jsonl") if item["trace_id"] == key]
    return row


def receipts(directory, pid, view, *, role="main"):
    return {
        message["tool_call_id"]: json.loads(message["content"])
        for probe in probes(directory, pid, "model_input", None if role == "worker" else view)
        if probe["role"] == role
        for message in probe["payload"]["messages"] if message["role"] == "tool"
    }


def terminal(directory, pid, view, events):
    probe, = probes(directory, pid, "before_sse_terminal", view)
    assert probe["event"] == events[-1]
    expected = ["user", "assistant"] if events[-1]["type"] == "done" else ["user"]
    assert [row["role"] for row in probe["sql"]] == expected
    if len(expected) == 2:
        assert probe["sql"][-1]["content"] == events[-2]["content"]
    commits = probes(directory, pid, "message_commit", view)
    for row in probe["sql"]:
        commit, = [item for item in commits if item["id"] == row["id"]]
        # Windows can report the same clock tick for two ordered probe events.
        assert commit["writer"] != row["reader"] and commit["sequence"] < probe["sequence"]
    return probe["sql"]


def check_no_business_sqlite(directory, pid):
    assert all(probe["allowed"] for probe in probes(directory, pid, "sqlite_python_open"))


def persisted_memory(root, user_id):
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(path=str(root / "conv_memory"), settings=Settings(anonymized_telemetry=False))
    collection = client.get_collection("conversations", embedding_function=R3Embedding())
    data = collection.get(where={"user_id": str(user_id)})
    return {
        "vectors": [{"id": key, "document": document, "metadata": meta}
                    for key, document, meta in zip(data["ids"], data["documents"], data["metadatas"], strict=True)],
        "json": json.loads((root / "user_memory" / f"user_{user_id}.json").read_text(encoding="utf-8")),
    }


def inspect_memory(root, user_id, destination):
    # A separate process releases Chroma's native files before Windows cleans the fixture.
    subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "scripts.verify_mysql_r3",
                    "--inspect-memory", str(root), str(user_id), "--output-dir", str(destination)],
                   check=True, timeout=30, stdout=subprocess.DEVNULL)
    return json.loads(destination.read_text(encoding="utf-8"))


def verify_first_process(connect, client, pid, directory, password, evidence):
    auth = headers(login(client, connect, "owner-r3", password))
    other = headers(login(client, connect, "other-r3", password))
    with scenario(evidence, CASES[0]) as result:
        assert client.get("/").status_code == 200
        events = chat(client, auth, "direct")
        got = receipts(directory, pid, "direct")
        assert got["owned"]["result"]["plan_name"] == "R3_OWN_PLAN"
        assert got["foreign"]["error_code"] == "plan_not_found"
        assert got["spoof"]["error_code"] == "invalid_tool_arguments"
        assert got["legacy"]["error_code"] == "unauthorized_tool"
        assert not any(step["stage"] == "worker" for step in trace(directory, pid, events)["steps"])
        rows = terminal(directory, pid, "direct", events)
        assert {row["user_id"] for row in rows} == {OWNER}
        result.update(sse=events, sql=rows, receipts=got, pid=pid)

    with scenario(evidence, CASES[1]) as result:
        events = chat(client, auth, "delegation")
        got = receipts(directory, pid, "delegation")["dispatch"]
        assert got["status"] == "completed" and got["states"] == {"lookup": "succeeded"} and not got["gaps"]
        plan, = got["evidence"]
        assert plan["arguments"] == {"plan_id": PLAN} and plan["result"]["plan_name"] == "R3_OWN_PLAN"
        workers = [step for step in trace(directory, pid, events)["steps"] if step["stage"] == "worker"]
        assert [step["action"] for step in workers] == ["created", "reclaimed"]
        assert workers[0]["runtime_call_id"] == workers[1]["runtime_call_id"]
        sql = [row for row in probes(directory, pid, "sql_read")
               if "FROM ad_plans p" in row["sql"] and row["thread"].startswith("team-worker")]
        assert any(row["params"] == [PLAN, OWNER] and row["rows"] == 1 for row in sql)
        result.update(sse=events, sql=terminal(directory, pid, "delegation", events),
                      worker=workers, worker_sql=sql, handoff=got)

    with scenario(evidence, CASES[2]) as result:
        got = {}
        for view in ("reads1", "reads2"):
            events = chat(client, auth, view)
            terminal(directory, pid, view, events)
            got.update(receipts(directory, pid, view))
        assert got.keys() == READS.keys()
        assert all(value.get("success", True) and value.get("result", {}).get("success", True)
                   if isinstance(value.get("result"), dict) else value.get("success", True) for value in got.values()), got
        assert got["get_account_detail"]["result"]["account_name"] == "R3_OWN_ACCOUNT"
        assert got["get_daily_trend"]["result"][0]["impressions"] == 100
        assert got["get_plan_metrics_by_date"]["result"]["impressions"] == 2000
        assert got["search_knowledge"]["result"]["evidence"]
        assert "R3_OTHER" not in json.dumps(got, ensure_ascii=False)
        assert len(probes(directory, pid, "sqlite_python_open")) == 1
        result["receipts"] = got

    with scenario(evidence, CASES[3]) as result:
        own = client.get(HISTORY, headers=auth)
        excluded = client.get(HISTORY, params={"exclude_chat_view_id": "direct"}, headers=auth)
        foreign = client.get(HISTORY, headers=other)
        assert own.status_code == excluded.status_code == foreign.status_code == 200
        assert "R3_REPLY_direct" in own.text and "R3_direct" not in excluded.text
        assert "合成历史回复" in excluded.text
        assert "R3_" not in foreign.text
        assert client.get(HISTORY).status_code == 401
        assert client.get("/api/plans", headers=auth).status_code == 404
        replaced = auth
        auth = headers(login(client, connect, "owner-r3", password))
        assert client.get(HISTORY, headers=replaced).status_code == 401
        assert client.post(STREAM, headers=replaced, json={"message": "R3_expired"}).status_code == 401
        result.update(owner=own.json()["data"], excluded=excluded.json()["data"], foreign=foreign.json()["data"],
                      unauthorized=401, replaced_session=401)

    with scenario(evidence, CASES[4]) as result:
        # InnoDB allocates these IDs before accepting the early request.
        with closing(connect()) as hole, hole.cursor() as cursor:
            cursor.execute("INSERT INTO conversations (user_id,role,content) VALUES (17,'user','R3_ROLLED_BACK')")
            hole_id = cursor.lastrowid
            hole.rollback()
        with closing(connect()) as pending, pending.cursor() as cursor:
            pending_id = pending.thread_id()
            low_ids = []
            for role, content in zip(("user", "assistant"), LATE_LOW, strict=True):
                cursor.execute("INSERT INTO conversations (user_id,role,content) VALUES (%s,%s,%s)",
                               (OWNER, role, content))
                low_ids.append(cursor.lastrowid)
            dump(directory / "late-low-ids.json", low_ids)
            assert _query(connect, "SELECT id FROM conversations WHERE id IN (%s,%s,%s)", (hole_id, *low_ids)) == []
            with ThreadPoolExecutor(max_workers=1) as thread:
                early = thread.submit(chat, client, auth, "early")
                try:
                    wait_for((directory / "early-paused").exists)
                    bound, = _query(connect, "SELECT id FROM conversations WHERE chat_view_id='early'")
                    assert hole_id < low_ids[0] < low_ids[1] < bound["id"]
                    pending.commit()
                    assert len(_query(connect, "SELECT id FROM conversations WHERE id IN (%s,%s)", low_ids)) == 2
                    late_events = chat(client, auth, "late")
                    late_sql = terminal(directory, pid, "late", late_events)
                    assert not early.done()
                finally:
                    (directory / "early-release").touch()
                early_events = early.result(timeout=35)
        early_sql = terminal(directory, pid, "early", early_events)
        assert low_ids[-1] < early_sql[0]["id"] < late_sql[0]["id"] < late_sql[1]["id"] < early_sql[1]["id"]
        assert not _query(connect, "SELECT id FROM conversations WHERE id=%s", (hole_id,))
        raw_hits, = probes(directory, pid, "chroma_query", "early")
        assert any(item["assistant_message_id"] == low_ids[-1] for item in raw_hits["metadata"])
        main = probes(directory, pid, "model_input", "early")
        workers = [row for row in probes(directory, pid, "model_input") if row["role"] == "worker"][-2:]
        assert main and len(workers) == 2 and "direct_inputs" in workers[0]["payload"]["messages"][1]["content"]
        full = json.dumps(main + workers, ensure_ascii=False)
        for marker in (*LATE_LOW, "R3_late", "R3_REPLY_late", "R3_ROLLED_BACK"):
            assert marker not in full
        assert "R3_REPLY_direct" in receipts(directory, pid, "early")["memory"]["result"]
        result.update(rolled_back_id=hole_id, late_low_ids=low_ids, late_low_writer=pending_id,
                      accepted_boundary=early_sql[0]["id"], early_sse=early_events, late_sse=late_events,
                      early_sql=early_sql, late_sql=late_sql, vector_hit=raw_hits,
                      main_model_calls=len(main), worker_model_calls=len(workers))

    for key, view, expected, error in (
        (CASES[5], "commit-fail", "error", "save_reply_failed"),
        (CASES[6], "memory-fail", "done", "conversation_memory_write_failed"),
    ):
        with scenario(evidence, key) as result:
            events = chat(client, auth, view, expected=expected)
            rows = terminal(directory, pid, view, events)
            record = trace(directory, pid, events)
            failures = [step for step in record["steps"] if step.get("error_code") == error]
            assert len(failures) == 1 and failures[0]["status"] == "error"
            assert record["status"] == ("error" if expected == "error" else "completed")
            assert not probes(directory, pid, "chroma_added", view)
            assert len(probes(directory, pid, "before_chroma_add", view)) == (expected == "done")
            if expected == "error":
                fault, = probes(directory, pid, "reply_commit_fault", view)
                assert not _query(connect, "SELECT id FROM conversations WHERE id=%s", (fault["id"],))
                result["rolled_back_reply_id"] = fault["id"]
            result.update(sse=events, sql=rows, trace=failures[0])
    foreign_events = chat(client, other, "foreign-seed")
    terminal(directory, pid, "foreign-seed", foreign_events)
    check_no_business_sqlite(directory, pid)


def verify_restart(connect, env, password, root, foreign, directory, evidence):
    owner = inspect_memory(root, OWNER, directory / "memory-a-owner.json")
    other = inspect_memory(root, OTHER, directory / "memory-a-other.json")
    cases = evidence["scenarios"]
    indexed_ids = {row["metadata"]["user_message_id"] for row in owner["vectors"]}
    assert all(cases[name]["sql"][0]["id"] not in indexed_ids for name in CASES[5:7])
    assert "R3_commit-fail" not in json.dumps(owner["json"])
    assert "R3_memory-fail" in json.dumps(owner["json"])
    own_turn, = [row for row in owner["vectors"] if "R3_direct" in row["document"]]
    foreign_turn, = [row for row in foreign["vectors"] if "R3_direct" in row["document"]]
    other_turn, = other["vectors"]
    assert own_turn["document"] == foreign_turn["document"]
    for field in ("user_id", "user_message_id", "assistant_message_id", "content_sha256"):
        assert own_turn["metadata"][field] == foreign_turn["metadata"][field]
    assert own_turn["id"] != foreign_turn["id"]
    assert own_turn["metadata"]["storage_namespace"] != foreign_turn["metadata"]["storage_namespace"]
    assert "R3_b-only" in json.dumps(foreign["json"]) and "R3_b-only" not in json.dumps(owner)
    assert "R3_delegation" in json.dumps(owner["json"]) and "R3_delegation" not in json.dumps(foreign)
    candidates = [foreign_turn, other_turn,
                  {**other_turn, "metadata": {**other_turn["metadata"], "user_id": str(OWNER)}},
                  {**own_turn, "document": "R3_MISMATCHED_DOCUMENT"},
                  {**own_turn, "metadata": {**own_turn["metadata"], "content_sha256": "0" * 64}}]
    dump(directory / "forged-candidates.json", candidates)
    dump(directory / "persisted-memory.json", {"A_owner": owner, "A_other": other, "B_owner": foreign})
    with closing(connect()) as conn, conn.cursor() as cursor:
        cursor.executemany("INSERT INTO conversations (user_id,role,content) VALUES (17,%s,%s)",
                           [("user" if i % 2 == 0 else "assistant", f"R3_recent_{i}") for i in range(52)])
        conn.commit()
    with api_process(env, directory) as (client, pid):
        auth = headers(login(client, connect, "owner-r3", password))
        other_auth = headers(login(client, connect, "other-r3", password))
        with scenario(evidence, CASES[7]) as result:
            assert pid != cases[CASES[0]]["pid"]
            events = chat(client, auth, "recall")
            got = receipts(directory, pid, "recall")["memory"]["result"]
            assert "R3_REPLY_direct" in got and LATE_LOW[1] in got
            first, *_ = [probe for probe in probes(directory, pid, "model_input", "recall")
                         if probe["role"] == "main"]
            assert "R3_REPLY_direct" not in json.dumps(first["payload"]["messages"], ensure_ascii=False)
            reads = [row for row in probes(directory, pid, "sql_read", "recall") if "id IN (?, ?)" in row["sql"]]
            assert any(row["params"] == [OWNER, own_turn["metadata"]["user_message_id"],
                                         own_turn["metadata"]["assistant_message_id"]] and row["rows"] == 2
                       for row in reads)
            result.update(pid=pid, sse=events, sql=terminal(directory, pid, "recall", events), memory=got,
                          memory_source_reads=reads)

        with scenario(evidence, CASES[8]) as result:
            other_events = chat(client, other_auth, "other")
            other_got = receipts(directory, pid, "other")["memory"]["result"]
            assert "R3_REPLY_foreign-seed" in other_got and "R3_REPLY_direct" not in other_got
            events = chat(client, auth, "forged")
            got = receipts(directory, pid, "forged")["memory"]["result"]
            assert got.count("R3_REPLY_direct") == 1
            assert all(marker not in got for marker in (
                "R3_foreign-seed", "R3_MISMATCHED_DOCUMENT", "R3_b-only",
            ))
            injected, = probes(directory, pid, "injected_vector_receipts", "forged")
            queried, = probes(directory, pid, "chroma_query", "forged")
            assert {"storage_namespace": root.name} in queried["where"]["$and"]
            result.update(other_sse=other_events, other_sql=terminal(directory, pid, "other", other_events),
                          owner_sse=events, owner_sql=terminal(directory, pid, "forged", events),
                          shared_turn_ids=[own_turn["metadata"][key] for key in (
                              "user_message_id", "assistant_message_id",
                          )], injected=injected["receipts"], owner_memory=got, other_memory=other_got,
                          distinct_namespaces=[root.name, foreign_turn["metadata"]["storage_namespace"]])
        check_no_business_sqlite(directory, pid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysqld", type=Path)
    parser.add_argument("--source", type=Path, default=Path("logs/mysql-r2/run-05/sanitized.sqlite3"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--inspect-memory", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    if args.serve:
        return serve(args.serve, directory)
    if args.inspect_memory:
        root, user_id = args.inspect_memory
        dump(directory, persisted_memory(Path(root), int(user_id)))
        return 0
    if not __debug__ or not args.mysqld or not args.mysqld.is_file() or directory.exists():
        parser.error("Use assertions, an existing mysqld binary and a new output directory")
    directory.mkdir(parents=True)
    source = args.source.resolve()
    evidence = {"scenarios": {}}
    before = source_digest(source)
    default_before = source_digest(DEFAULT_DB_PATH)
    server = args.mysqld.resolve()
    try:
        with TemporaryDirectory(prefix="mysql-r3-fixture-") as temp, \
                isolated_mysql(server, schema=DDL, schema_name="mysql_r2") as first, \
                isolated_mysql(server, schema=DDL, schema_name="mysql_r2") as second:
            temp = Path(temp)
            _, artifact, manifest = synthetic_artifact(temp)
            memory_base = temp / "memory"
            env_a, password_a, facts_a = fixture(first, source, artifact, manifest, temp / "A", memory_base)
            env_b, password_b, facts_b = fixture(second, source, artifact, manifest, temp / "B", memory_base)
            evidence["servers"] = {"A": facts_a, "B": facts_b}
            assert facts_a["uuid"] != facts_b["uuid"]
            b_logs = directory / "instance-b"
            b_logs.mkdir()
            with api_process(env_b, b_logs) as (client, pid):
                auth = headers(login(client, second, "owner-r3", password_b))
                for view in ("direct", "b-only"):
                    events = chat(client, auth, view)
                    terminal(b_logs, pid, view, events)
                check_no_business_sqlite(b_logs, pid)
            foreign = inspect_memory(memory_base / "mysql_r2" / facts_b["uuid"], OWNER, b_logs / "memory-owner.json")
            with api_process(env_a, directory) as (client, pid):
                verify_first_process(first, client, pid, directory, password_a, evidence)
            verify_restart(first, env_a, password_a, memory_base / "mysql_r2" / facts_a["uuid"],
                           foreign, directory, evidence)
    finally:
        after = source_digest(source)
        default_after = source_digest(DEFAULT_DB_PATH)
        evidence.update(sqlite_source_sha256={"before": before, "after": after},
                        sqlite_business_sha256={"before": default_before, "after": default_after})
        for name in CASES:
            evidence["scenarios"].setdefault(name, {"status": "not_run"})
        evidence["counts"] = {status: sum(item["status"] == status for item in evidence["scenarios"].values())
                              for status in ("passed", "failed", "not_run")}
        dump(directory / "report.json", evidence)
        assert before == after and default_before == default_after
    print(evidence["counts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
