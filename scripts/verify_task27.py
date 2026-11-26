"""Run the TASK-027 browser and SQL contract against a fresh SQLite or MySQL fixture."""

import argparse
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from hashlib import file_digest
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic_ns
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ATTACK = """<img src="data:image/png,invalid" onerror="this.dataset.securityProbe='executed'">"""
ASSISTANT_ATTACK = f"**正常回复**\n{ATTACK}\n[危险链接](javascript:alert(27))"


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


class FixedEmbedding:
    @staticmethod
    def name():
        return "task27-fixed"

    @staticmethod
    def build_from_config(config):
        return FixedEmbedding()

    def get_config(self):
        return {}

    def __call__(self, input):
        return [[1., 0.] if "A_" in text else [0., 1.] for text in input]

    def embed_query(self, input):
        return self(input)


class FixedModel:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)
        self.calls = []
        self.timeline = []
        self.injected = []
        self.a_waiting = Event()
        self.release_a = Event()
        self.lock = Lock()

    def mark(self, event):
        with self.lock:
            self.timeline.append({"event": event, "ns": monotonic_ns()})

    def create(self, messages, tools=None, response_format=None, **kwargs):
        from backend.agent.tests.test_home_delegation import dispatch, reply, worker_summary
        from backend.agent.tests.test_home_sse_tracing import tool_call

        current = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        receipts = {message["tool_call_id"]: json.loads(message["content"])
                    for message in messages if message["role"] == "tool"}
        if response_format:
            task = json.loads(current)["task"]
            case = task["task_id"]
            phase = "worker"
        else:
            case = current
            phase = "main" if tools else "planner"
        with self.lock:
            self.calls.append(deepcopy({"case": case, "phase": phase, "messages": messages, "tools": tools,
                                       "response_format": response_format, "options": kwargs}))
        if phase == "worker":
            if receipts:
                if case == "A_FAIL":
                    return worker_summary("No owned plan", status="missing")
                return worker_summary(case + "_HANDOFF")
            plan_id = {"A_WORK": 101, "B_WORK": 102, "A_FAIL": 201}[case]
            return reply("", tool_call(case + "_READ", name="get_plan_detail", plan_id=plan_id))
        if phase == "planner":
            return reply('{"steps":[]}')
        if case.endswith("_SEED") or case == "LEGACY_CONTINUE":
            return reply(case + "_ANSWER")
        if case == "A_RESUME":
            return reply("A_RESUME_ANSWER")
        if case in {"A_WORK", "B_WORK", "A_FAIL"}:
            if case != "A_FAIL" and "memory" not in receipts:
                return reply("", tool_call("memory", name="search_conversation_memory", query=case,
                                           user_id=18, conversation_id="forged", through_message_id=2**62))
            if "dispatch" not in receipts:
                return reply("", dispatch("dispatch", {
                    "task_id": case, "objective": "核对计划详情 " + (ATTACK if case == "A_WORK" else ""),
                    "capabilities": ["get_plan_detail"], "expected_output": "计划详情",
                    "stop_condition": "获得只读工具回执后结束",
                }))
            self.mark(case + "_main_answer")
            return reply(case + " " + (ASSISTANT_ATTACK if case == "A_WORK" else "完成"))
        raise AssertionError(f"Unexpected fixture model case {case}")


@contextmanager
def fixture(directory, mysqld, password):
    from backend.config.config import settings
    from backend.database import database

    with ExitStack() as stack:
        scratch = Path(stack.enter_context(TemporaryDirectory(dir=directory, prefix="fixture-")))
        if mysqld is None:
            stack.enter_context(patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(scratch / "task27.sqlite3")))
            with closing(database.get_db()) as conn, conn:
                conn.execute("""CREATE TABLE conversations
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT, created_at TEXT)""")
                conn.execute("INSERT INTO conversations VALUES (41, 17, 'user', 'LEGACY_QUESTION', '2025-01-01')")
                conn.execute("INSERT INTO conversations VALUES (42, 17, 'assistant', ?, '2025-01-01')",
                             (ASSISTANT_ATTACK,))
            database.init_db()
            database.init_db()
            connect = database.get_db
            bind = "?"
        else:
            from pydantic import SecretStr
            from scripts.verify_mysql_r2 import runtime_env
            from scripts.verify_mysql_t0 import isolated_mysql

            connect = stack.enter_context(isolated_mysql(mysqld, schema=ROOT / "scripts/mysql_r2_schema.sql",
                                                        schema_name="mysql_r2"))
            environment = runtime_env(connect, "", scratch)
            for key, value in {
                "DIAGNOSIS_MYSQL_ENABLED": True, "DIAGNOSIS_MYSQL_SCHEMA_VERSION": "2",
                "DIAGNOSIS_MYSQL_PORT": int(environment["DIAGNOSIS_MYSQL_PORT"]),
                "DIAGNOSIS_MYSQL_PASSWORD": SecretStr(environment["DIAGNOSIS_MYSQL_PASSWORD"]),
                "MYSQL_CHAT_MEMORY_ROOT": str(scratch / "memory"),
            }.items():
                stack.enter_context(patch.object(settings, key, value))
            bind = "%s"

        def sql(statement, params=(), *, rows=False):
            with closing(connect()) as conn:
                cursor = conn.cursor()
                cursor.execute(statement.replace("?", bind), params)
                if rows:
                    return [dict(row) for row in cursor.fetchall()]
                conn.commit()

        for user_id, username in ((17, "task27-owner"), (18, "task27-other")):
            sql("INSERT INTO users (id, username, password, role) VALUES (?, ?, ?, 'user')",
                (user_id, username, database.hash_password(password)))
        for plan_id, user_id, name in ((101, 17, "A_PLAN_ONLY"), (102, 17, "B_PLAN_ONLY"), (201, 18, "FOREIGN_PLAN")):
            sql("INSERT INTO ad_plans (id, user_id, plan_name, impressions, clicks) VALUES (?, ?, ?, 120, 12)",
                (plan_id, user_id, name))
        if mysqld is not None:
            sql("INSERT INTO conversations (id, user_id, role, content) VALUES (41, 17, 'user', 'LEGACY_QUESTION')")
            sql("INSERT INTO conversations (id, user_id, role, content) VALUES (42, 17, 'assistant', ?)",
                (ASSISTANT_ATTACK,))
            upgrade = (ROOT / "scripts/mysql_task27_schema.sql").read_text(encoding="utf-8")
            for _ in range(2):
                for statement in upgrade.split(";"):
                    if statement.strip():
                        sql(statement)
            for table in ("conversations", "chat_groups", "chat_group_runs", "chat_group_entries",
                          "chat_group_run_requests"):
                sql(f"GRANT INSERT ON mysql_r2.{table} TO 'mysql_r1_app'@'localhost'")
            for table in ("chat_groups", "chat_group_runs"):
                sql(f"GRANT UPDATE ON mysql_r2.{table} TO 'mysql_r1_app'@'localhost'")
        assert sql("SELECT id FROM conversations ORDER BY id", rows=True) == [{"id": 41}, {"id": 42}]
        assert sql("SELECT conversation_id FROM chat_groups", rows=True) == [{"conversation_id": "legacy-17"}]
        yield sql


@contextmanager
def server(directory, model):
    import uvicorn
    from chromadb.api.models.Collection import Collection
    from chromadb.api.shared_system_client import SharedSystemClient
    from fastapi import FastAPI
    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.memory import conversation_memory as memory
    from backend.tools import tools

    original_detail = tools.get_plan_detail
    original_query = Collection.query

    def plan_detail(plan_id, user_id=None):
        assert user_id == 17
        if plan_id == 101:
            model.mark("A_tool_waiting")
            model.a_waiting.set()
            if not model.release_a.wait(40):
                raise TimeoutError("A barrier not released")
        result = original_detail(plan_id, user_id)
        model.mark(f"plan_{plan_id}_returned")
        return result

    def poison_query(collection, *args, **kwargs):
        result = original_query(collection, *args, **kwargs)
        if collection.name != "conversations":
            return result
        scope = next(item["conversation_id"] for item in kwargs["where"]["$and"] if "conversation_id" in item)
        all_rows = collection.get(include=["documents", "metadatas"])
        opposite = next((doc, meta) for doc, meta in zip(all_rows["documents"], all_rows["metadatas"])
                        if meta.get("conversation_id") != scope)
        own_doc, own_meta = result["documents"][0][0], result["metadatas"][0][0]
        probes = [
            opposite,
            (opposite[0], {**opposite[1], "conversation_id": scope}),
            ("FORGED_VECTOR_DOCUMENT", own_meta),
            (own_doc, {**own_meta, "user_id": "18"}),
            ("UNBOUND_V2_VECTOR", {**own_meta, "schema_version": 2}),
        ]
        model.injected.append({"scope": scope, "filter": kwargs["where"], "receipts": probes})
        for doc, meta in probes:
            result["documents"][0].insert(0, doc)
            result["metadatas"][0].insert(0, meta)
            result["distances"][0].insert(0, 0.)
        return result

    with (patch.object(ProviderFactory, "create", return_value=model),
          patch.object(memory, "BGEEmbedding", FixedEmbedding),
          patch.object(Collection, "query", poison_query),
          patch.object(tools, "get_plan_detail", plan_detail),
          patch.object(observability, "TRACE_LOG_FILE", directory / "traces.jsonl"),
          patch.object(observability, "FUNC_LOG_FILE", directory / "functions.jsonl"),
          patch.object(observability, "ERROR_LOG_FILE", directory / "errors.jsonl"),
          socket.socket() as listener):
        from fast_server import app
        from backend.captcha import captcha
        from tests.home_diagnosis_server import demo_captcha
        control = FastAPI()
        app.mount("/__task27", control)

        @control.get("/state")
        def state():
            return {"a_waiting": model.a_waiting.is_set()}

        @control.post("/release")
        def release():
            model.mark("release_A")
            model.release_a.set()
            return {}

        listener.bind(("127.0.0.1", 0))
        listener.listen()
        instance = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="warning", access_log=False))
        thread = Thread(target=instance.run, kwargs={"sockets": [listener]}, daemon=True)
        with patch.object(captcha, "generate_captcha", demo_captcha):
            thread.start()
            try:
                yield f"http://127.0.0.1:{listener.getsockname()[1]}"
            finally:
                model.release_a.set()
                instance.should_exit = True
                thread.join(60)
                assert not thread.is_alive()
                for system in SharedSystemClient._identifier_to_system.values():
                    system.stop()
                SharedSystemClient.clear_system_cache()


def verify(directory, sql, model):
    browser = json.loads((directory / "browser.json").read_text(encoding="utf-8"))
    a, b = browser["ids"]["A"], browser["ids"]["B"]
    groups = sql("SELECT conversation_id, user_id, title, archived FROM chat_groups ORDER BY title", rows=True)
    runs = sql("SELECT * FROM chat_group_runs ORDER BY run_id", rows=True)
    entries = sql("SELECT * FROM chat_group_entries ORDER BY id", rows=True)
    legacy = sql("SELECT id, user_id, role, content FROM conversations ORDER BY id", rows=True)
    dump(directory / "sql.json", {"groups": groups, "runs": runs, "entries": entries, "legacy": legacy})
    assert [row["id"] for row in legacy][:2] == [41, 42]
    assert all("A_" not in row["content"] and "B_" not in row["content"] for row in legacy)
    assert len(groups) == 3
    trace_by_id = {trace["trace_id"]: trace for trace in
                   map(json.loads, (directory / "traces.jsonl").read_text(encoding="utf-8").splitlines())}
    for label, conversation_id in (("A", a), ("B", b)):
        other = "B" if label == "A" else "A"
        for call in [call for call in model.calls if call["case"].startswith(label + "_")]:
            payload = json.dumps({"messages": call["messages"], "tools": call["tools"]}, ensure_ascii=False)
            for forbidden in (other + "_SEED", other + "_PLAN_ONLY", other + "_WORK", "FORGED_VECTOR_DOCUMENT",
                              "UNBOUND_V2_VECTOR", "FOREIGN_PLAN", "LEGACY_QUESTION"):
                assert forbidden not in payload, (call["case"], call["phase"], forbidden)
            if call["phase"] == "worker":
                projection = json.loads(call["messages"][1]["content"])
                assert set(projection) == {"task", "inputs"} and projection["inputs"] == {}
                assert {tool["function"]["name"] for tool in call["tools"]} == {"get_plan_detail"}
                assert label + "_SEED" not in payload
        main = [call for call in model.calls if call["case"] == label + "_WORK" and call["phase"] == "main"]
        assert len(main) == 3
        memory = next(row["content"] for row in main[-1]["messages"] if row.get("tool_call_id") == "memory")
        assert "【历史对话检索结果】" in memory and label + "_SEED_ANSWER" in memory
        batch = next(json.loads(row["content"]) for row in main[-1]["messages"] if row.get("tool_call_id") == "dispatch")
        receipt, = batch["evidence"]
        assert receipt["result"]["plan_name"] == label + "_PLAN_ONLY"
        group_runs = [row for row in runs if row["conversation_id"] == conversation_id]
        group_entries = [entry for entry in entries if entry["run_id"] in {row["run_id"] for row in group_runs}]
        work, = [entry for entry in group_entries if entry["kind"] == "user" and entry["content"] == label + "_WORK"]
        events = [json.loads(entry["details"]) for entry in group_entries
                  if entry["run_id"] == work["run_id"] and entry["kind"] == "worker"]
        assert [row["kind"] for row in events] == ["queued", "dispatched", "tool_started", "receipt", "succeeded", "reclaimed"]
        assert events[2]["runtime_call_id"] == events[3]["runtime_call_id"] == receipt["runtime_call_id"]
        assert events[3]["artifact_id"] == receipt["artifact_id"]
        run = next(row for row in group_runs if row["run_id"] == work["run_id"])
        assert run["status"] == "completed"
        assert run["trace_id"] == browser["sse"][label][0]["trace_id"]
        assert run["run_id"] == browser["sse"][label][0]["run_id"]
        steps = trace_by_id[run["trace_id"]]["steps"]
        assert any(step["stage"] == "tool_result" and step.get("runtime_call_id") == receipt["runtime_call_id"]
                   and step["status"] == "ok" for step in steps)
        for action, event in (("created", events[1]), ("reclaimed", events[-1])):
            assert any(step["stage"] == "worker" and step["action"] == action
                       and step["runtime_call_id"] == event["worker_id"] for step in steps)
        expected_ids = [f"entry:{row['id']}" for row in group_entries if row["kind"] != "worker"]
        assert browser["dom"][label] == expected_ids
    order = [row["event"] for row in model.timeline]
    assert order.index("A_tool_waiting") < order.index("B_WORK_main_answer") < order.index("release_A")
    assert order.index("release_A") < order.index("A_WORK_main_answer")
    b_reply, = [row for row in entries if row["kind"] == "assistant" and row["run_id"] == browser["sse"]["B"][0]["run_id"]]
    a_receipt, = [row for row in entries if row["kind"] == "worker"
                  and row["run_id"] == browser["sse"]["A"][0]["run_id"] and json.loads(row["details"])["kind"] == "receipt"]
    assert b_reply["id"] < a_receipt["id"]
    assert len(model.injected) == 2
    return {"status": "passed", "model_calls": len(model.calls), "groups": [a, b], "timeline": order}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mysqld", type=Path)
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH="", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="",
                      JWT_SECRET=token_urlsafe(48), ANONYMIZED_TELEMETRY="False", LLM_PROVIDER="mock")
    from backend.database.database import DEFAULT_DB_PATH

    with DEFAULT_DB_PATH.open("rb") as stream:
        before = file_digest(stream, "sha256").hexdigest()
    password = token_urlsafe(24)
    model = FixedModel()
    evidence = {"status": "failed"}
    try:
        with fixture(directory, args.mysqld.resolve() if args.mysqld else None, password) as sql:
            with server(directory, model) as url, (directory / "browser.log").open("wb") as log:
                subprocess.run(["node", "tests/task27_browser.cjs", url, str(directory)], cwd=ROOT,
                               env={**os.environ, "AD_TASK27_FIXTURE_PASSWORD": password},
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=150)
            evidence = verify(directory, sql, model)
    finally:
        dump(directory / "model-inputs.json", model.calls)
        dump(directory / "vector-probes.json", model.injected)
        dump(directory / "timeline.json", model.timeline)
        with DEFAULT_DB_PATH.open("rb") as stream:
            after = file_digest(stream, "sha256").hexdigest()
        dump(directory / "report.json", {**evidence, "business_sqlite_sha256": {"before": before, "after": after}})
        assert before == after


if __name__ == "__main__":
    main()
