"""Exercise homepage SSE and conversation memory with a local BGE snapshot."""

import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import sqlite3
import subprocess
import sys
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODEL = "BAAI/bge-small-zh-v1.5"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
QUERY = "过去讨论过的冷链运输中，如何防止鲜花花瓣冻伤？"
TOOL = "search_conversation_memory"
FAIL_DETAIL = "TASK25_PRIVATE_FIXTURE_EXCEPTION"
FILLER = "请为数学课堂写一句有关三角形内角和的提示"
SEEDS = {
    "origin": ("鲜花冷链运输遇低温时，怎样避免花瓣冻伤？", "用隔温包装并监测车厢温度"),
    "sibling": ("鲜切花冷藏转运怎么防花瓣冻伤？", "使用隔热箱并设置冷藏温度报警"),
    "other": ("低温配送鲜花怎样保护花瓣免受冻害？", "增加保温衬层并监控温度"),
    "partner": ("如何调试小提琴琴弦？", "按标准音高逐弦调音"),
    "ttl_valid": ("如何保存纸质手稿？", "放入无酸档案盒"),
    "ttl_edge": ("如何保护瓷器？", "放置防震垫"),
    "ttl_expired": ("如何保管铜器？", "保持环境干燥"),
    "ttl_invalid": ("如何存放羽毛？", "置于密封盒内"),
    "legacy_first": ("如何清洁望远镜？", "使用柔软镜头刷"),
    "legacy_second": ("如何晾干陶土？", "置于通风阴凉处"),
}


def marker(name, role):
    return f"T25_{name.upper()}_{role}"


def dump(data):
    return json.dumps(data, ensure_ascii=False, default=vars)


def write(path, data):
    path.write_text(dump(data) + "\n", encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(messages):
    return sha256(dump(messages).encode()).hexdigest()


class Audit:
    def __init__(self, output):
        self.output = output
        self.case = "startup"
        self.events = []
        self.fault = None
        self.chat = SimpleNamespace(completions=self)

    def log(self, kind, **data):
        event = {"case": self.case, "pid": os.getpid(), "kind": kind, **data}
        self.events.append(event)
        with (self.output / "ledger.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(dump(event) + "\n")

    def create(self, messages, **options):
        from backend.agent.tests.test_home_sse_tracing import FixedModel, tool_call

        names = {tool["function"]["name"] for tool in options.get("tools") or []}
        call_id = f"call_t25_{self.case}"
        has_receipt = any(m["role"] == "tool" and m["tool_call_id"] == call_id for m in messages)
        calls = []
        answer = "固定复跑回复。"
        if self.case in SEEDS:
            answer = f"{SEEDS[self.case][1]} {marker(self.case, 'A')}"
        if options.get("max_tokens") == 500:
            answer = '{"steps": []}'
        if self.case.startswith("recall_") and TOOL in names and not has_receipt:
            calls = [tool_call(call_id, name=TOOL, query=QUERY)]
            answer = None
        self.log("sdk", request=json.loads(dump({"messages": messages, **options})),
                 response={"content": answer, "tool_calls": json.loads(dump(calls))})
        if options.get("stream"):
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=answer))])])
        return FixedModel.response(answer, calls)


class MemoryWarnings(logging.Handler):
    def __init__(self, audit):
        super().__init__(logging.WARNING)
        self.audit = audit

    def emit(self, record):
        self.audit.log("memory_warning", code=record.getMessage())


def configure(output):
    os.environ.update(AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", HF_HUB_OFFLINE="1",
                      TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False", LLM_PROVIDER="mock",
                      GLM_API_KEY="", API_KEY="", DEEPSEEK_API_KEY="", OPENAI_API_KEY="", DEBUG="0",
                      DIAGNOSIS_MYSQL_ENABLED="0", PLAN_METRICS_DB_PATH="", JWT_SECRET=token_urlsafe(32),
                      DIAGNOSIS_SANDBOX_DB_PATH=str(output / "isolated.sqlite3"),
                      RAG_DB_PATH=str(output / "knowledge"))


def preflight():
    import importlib.metadata as metadata
    from huggingface_hub import snapshot_download
    from backend.rag import chinese_embedding

    expected = {"torch": "2.8.0", "sentence-transformers": "5.1.2", "transformers": "4.57.1"}
    versions = {name: metadata.version(name) for name in expected}
    assert versions == expected and chinese_embedding.BGE_REVISION == REVISION, versions
    snapshot = Path(snapshot_download(MODEL, revision=REVISION, local_files_only=True))
    encoder = chinese_embedding._get_model()
    dimension = encoder.get_sentence_embedding_dimension()
    assert snapshot.name == REVISION and dimension == 512
    return encoder, {"interpreter": sys.executable, "python": sys.version, "versions": versions,
                     "model": MODEL, "revision": REVISION, "snapshot": str(snapshot), "dimension": dimension,
                     "encoder": f"{type(encoder).__module__}.{type(encoder).__qualname__}",
                     "snapshot_weights": {p.name: p.stat().st_size for p in snapshot.iterdir()
                                          if p.suffix in {".bin", ".safetensors"}}}


def sql(output, statement, params=()):
    with closing(sqlite3.connect((output / "isolated.sqlite3").as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(statement, params)]


def committed(output, owner, terminal, question):
    if "run_id" in terminal:
        rows = sql(output, """SELECT e.id, e.kind AS role, e.content, e.run_id, r.conversation_id,
                               g.user_id, r.status, r.trace_id FROM chat_group_entries e
                               JOIN chat_group_runs r ON r.run_id=e.run_id
                               JOIN chat_groups g ON g.conversation_id=r.conversation_id
                               WHERE e.run_id=? AND e.kind IN ('user','assistant') ORDER BY e.id""",
                   (terminal["run_id"],))
        assert all(row["status"] == "completed" and row["trace_id"] == terminal["trace_id"]
                   and row["conversation_id"] == terminal["conversation_id"] for row in rows)
    else:
        rows = list(reversed(sql(output, "SELECT id, role, content, user_id FROM conversations "
                                 "WHERE user_id=? ORDER BY id DESC LIMIT 2", (owner,))))
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == question and all(row["user_id"] == owner for row in rows)
    return rows


def record_id(owner, group, question_id):
    scope = f":{group}" if group else ""
    return sha256(f"sqlite-turn:{owner}{scope}:{question_id}".encode()).hexdigest()


def records(collection, ids):
    import numpy as np

    response = collection.get(ids=ids, include=["documents", "metadatas", "embeddings"])
    return {key: {"id": key, "document": doc, "metadata": meta,
                  "dimension": len(vector), "vector_sha256": sha256(np.asarray(vector, dtype="<f4").tobytes()).hexdigest(),
                  "norm": float(np.linalg.norm(vector))}
            for key, doc, meta, vector in zip(response["ids"], response["documents"], response["metadatas"],
                                              response["embeddings"], strict=True)}


def verify_record(indexed, rows, case_events):
    question, answer = rows
    meta = indexed["metadata"]
    assert meta["user_id"] == str(question["user_id"])
    assert meta.get("conversation_id") == question.get("conversation_id")
    assert (meta["user_message_id"], meta["assistant_message_id"]) == (question["id"], answer["id"])
    if question.get("conversation_id"):
        assert meta["content_sha256"] == digest([question["content"], answer["content"]])
    assert indexed["document"] == f"用户: {question['content']}\nAI: {answer['content']}"
    encoded, = [event for event in case_events if event["kind"] == "encode" and event["operation"] == "add"]
    assert indexed["dimension"] == 512 and abs(indexed["norm"] - 1) < 1e-5
    assert indexed["vector_sha256"] == encoded["vectors"][0] and encoded["texts"] == [indexed["document"]]


def exercise(output, phase, audit, http, collection):
    from backend import observability

    def login(owner):
        resp = http.post("/api/login", json={"username": f"task25-{owner}", "password": f"fixture-{owner}",
                                            "captcha_id": "", "captcha_text": ""})
        assert resp.json()["code"] == 200
        return {"Authorization": "Bearer " + resp.json()["data"]["token"]}

    assert "/api/chat/stream" in http.get("/").raise_for_status().text
    headers = {owner: login(owner) for owner in (17, 18)}
    if phase == "seed":
        groups = {}
        for key, owner in (("a", 17), ("b", 17), ("c", 18)):
            resp = http.post("/api/chat/conversations", headers=headers[owner], json={"title": f"合成工作群 {key}"})
            assert resp.status_code == 201
            groups[key] = resp.json()["data"]["conversation_id"]
        write(output / "groups.json", groups)
    else:
        groups = read(output / "groups.json")

    def turn(case, key, question, fault=None):
        audit.case, audit.fault = case, fault
        before = len(audit.events)
        owner = 18 if key == "c" else 17
        group = groups.get(key)
        body = {"message": question, **({"conversation_id": group} if group else {})}
        with http.stream("POST", "/api/chat/stream", headers=headers[owner], json=body) as response:
            response.raise_for_status()
            assert response.headers["content-type"].startswith("text/event-stream")
            sse = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
        audit.log("sse", request=body, events=sse)
        terminal, = [e for e in sse if e["type"] in {"done", "error", "stopped", "superseded"}]
        assert terminal == sse[-1] and terminal["type"] == "done", sse
        rows = committed(output, owner, terminal, question)
        assert rows[1]["content"] == "".join(e["content"] for e in sse if e["type"] == "text")
        trace = observability.get_trace(terminal["trace_id"])
        assert trace["status"] == "completed"
        key_id = record_id(owner, group, rows[0]["id"])
        indexed = records(collection, [key_id]).get(key_id)
        result = {"sqlite": rows, "chroma": indexed, "terminal": terminal, "trace": trace["trace_id"]}
        audit.log("commit", **result)
        case_events = audit.events[before:]
        if fault == "add":
            errors = [step["error_code"] for step in trace["steps"] if step["stage"] == "memory"]
            assert indexed is None and errors == ["conversation_memory_write_failed"], errors
            assert sum(event["kind"] == "write_fault" for event in case_events) == 1
        else:
            assert indexed is not None, trace
            verify_record(indexed, rows, case_events)
        return result, case_events, trace

    if phase == "seed":
        results = {}
        for name in SEEDS:
            key = {"sibling": "b", "other": "c"}.get(name, None if name.startswith("legacy_") else "a")
            question = f"{SEEDS[name][0]} {marker(name, 'Q')}"
            results[name], _, _ = turn(name, key, question)
            assert results[name]["sqlite"][1]["content"].endswith(marker(name, "A"))
        for key in ("a", None):
            for index in range(25):
                turn(f"pad_{key}_{index:02}", key, FILLER)
        write(output / "seed.json", results)
        return

    seeds = read(output / "seed.json")
    ids = {name: row["chroma"]["id"] for name, row in seeds.items()}
    persisted = records(collection, list(ids.values()))
    assert persisted == {row["chroma"]["id"]: row["chroma"] for row in seeds.values()}
    audit.log("post_restart_chroma", records=persisted)
    for source in seeds.values():
        question, answer = source["sqlite"]
        if "run_id" in source["terminal"]:
            rows = committed(output, question["user_id"], source["terminal"], question["content"])
        else:
            rows = sql(output, "SELECT id, role, content, user_id FROM conversations WHERE id IN (?,?) ORDER BY id",
                       (question["id"], answer["id"]))
        assert rows == source["sqlite"]

    def candidate(name, **overrides):
        row = persisted[ids[name]]
        return {**row, "metadata": {**row["metadata"], **overrides}}

    def mixed(first, second):
        q, a = seeds[first]["sqlite"][0], seeds[second]["sqlite"][1]
        assert q["user_id"] == a["user_id"] and q.get("conversation_id") == a.get("conversation_id")
        assert q["id"] < a["id"]
        if q.get("run_id"):
            assert q["run_id"] != a["run_id"]
        row = candidate(first, assistant_message_id=a["id"], content_sha256=digest([q["content"], a["content"]]))
        return {**row, "document": f"用户: {q['content']}\nAI: {a['content']}"}

    cases = {}

    def recall(name, key, *, allow=(), deny=(), old=(), fault=None, warning=None):
        case = "recall_" + name
        result, events, trace = turn(case, key, QUERY, fault)
        call_id = f"call_t25_{case}"
        sdk = [e for e in events if e["kind"] == "sdk"]
        first, = [e for e in sdk if any(tc["id"] == call_id for tc in e["response"]["tool_calls"])]
        following, = [e for e in sdk if any(m.get("tool_call_id") == call_id for m in e["request"]["messages"])]
        receipt, = [m for m in following["request"]["messages"] if m.get("tool_call_id") == call_id]
        verified = json.loads(receipt["content"])
        if isinstance(verified, dict) and "result" in verified:
            verified = verified["result"]
        assert sdk.index(first) < sdk.index(following) and receipt["role"] == "tool"
        steps = [step for step in trace["steps"] if step["action"] == TOOL and step.get("tool_call_id") == call_id]
        assert [step["stage"] for step in steps] == ["tool_call", "tool_result"], trace
        query, = [e for e in events if e["kind"] == "query"]
        owner, group = result["sqlite"][0]["user_id"], groups.get(key)
        boundary = result["sqlite"][0]["id"]
        where = [{"user_id": str(owner)}, {"schema_version": 3 if group else 2},
                 {"user_message_id": {"$lt": boundary}}, {"assistant_message_id": {"$lt": boundary}}]
        if group:
            where.extend([{"storage_namespace": "sqlite"}, {"conversation_id": group}])
        assert query["kwargs"]["where"] == {"$and": where}
        inputs = [m for e in sdk for m in e["request"]["messages"]]
        scanned = dump(inputs)
        non_tool = dump([m for m in inputs if m["role"] != "tool"])
        matches = {source: {role: sorted({m["role"] for m in inputs if marker(source, role) in dump(m)})
                            for role in ("Q", "A")} for source in (*allow, *deny)}
        checks = {
            "exact_allowed_turns": all(persisted[ids[s]]["document"] in verified for s in allow),
            "no_denied_markers": all(marker(s, r) not in scanned for s in deny for r in ("Q", "A")),
            "outside_recent_history": all(marker(s, r) not in non_tool for s in old for r in ("Q", "A")),
            "warning": warning is None or warning in [e.get("code") for e in events],
            "no_raw_error": FAIL_DETAIL not in scanned and FAIL_DETAIL not in dump(trace),
        }
        if fault == "query":
            code = "conversation_memory_filter_failed"
            checks["fixed_error"] = steps[-1].get("error_code") == code and verified["error_code"] == code
        else:
            encoded, = [e for e in events if e["kind"] == "encode" and e["operation"] == "query"]
            checks["real_query"] = (encoded["texts"] == [f"为这个句子生成表示以用于检索相关文章：{QUERY}"]
                                    and encoded["shape"] == [1, 512] and encoded["normalized"] is True)
            if not fault:
                checks["native_filter_and_hit"] = (set(ids[s] for s in allow) <= set(query["native"]["ids"][0])
                                                    and all(ids[s] not in query["native"]["ids"][0] for s in deny))
            if isinstance(fault, list):
                checks["candidate_delivered"] = query["injected"] == fault and query["candidate_ids"] == [
                    row["id"] for row in fault
                ]
            if isinstance(fault, list) and not allow:
                checks["empty_verified_receipt"] = "未找到相关历史对话" in verified
        cases[name] = {"passed": all(checks.values()), "checks": checks, "matched_roles": matches,
                       "trace_id": result["trace"], "tool_call_id": call_id, "tool_receipt": receipt, "verified": verified,
                       "candidate_ids": query.get("candidate_ids"), "native_ids": (query.get("native") or {}).get("ids"),
                       "filter": query["kwargs"]["where"], "warnings": [e["code"] for e in events if "code" in e]}
        write(output / "cases.json", cases)
        if not cases[name]["passed"]:
            request = {"message": QUERY, **({"conversation_id": group} if group else {})}
            write(output / f"repro-{name}.json", {"case": name, "request": request,
                                                   "sqlite_sources": {s: seeds[s]["sqlite"] for s in (*allow, *deny)},
                                                   "candidate_at_verification": query.get("injected"), "result": cases[name]})
            if not (output / "first-deviation.json").exists():
                write(output / "first-deviation.json", {"case": name, "result": cases[name], "query": query,
                                                        "request_before": first, "request_after": following})

    recall("restart", "a", allow=("origin",), deny=("sibling", "other"), old=("origin",))
    recall("sibling", "b", allow=("sibling",), deny=("origin", "other"))
    recall("other", "c", allow=("other",), deny=("origin", "sibling"))
    recall("foreign_group", "a", deny=("sibling",), fault=[candidate("sibling")],
           warning="conversation_memory_namespace_rejected")
    recall("foreign_owner", "a", deny=("other",), fault=[candidate("other")],
           warning="conversation_memory_owner_rejected")
    for suffix, ttl in (("valid", "2026-11-13"), ("edge", "2026-11-12"), ("expired", "2026-11-11"),
                        ("invalid", "not-a-date")):
        source = "ttl_" + suffix
        allowed = suffix == "valid"
        collection.update(ids=[ids[source]], metadatas=[{"ttl": ttl}])
        stored = records(collection, [ids[source]])[ids[source]]
        assert stored["metadata"]["ttl"] == ttl and stored["vector_sha256"] == persisted[ids[source]]["vector_sha256"]
        recall(source, "a", allow=(source,) if allowed else (), deny=() if allowed else (source,), old=(source,),
               fault=[stored],
               warning="conversation_memory_boundary_rejected" if suffix == "expired" else None)
    recall("group_mismatch", "a", deny=("origin", "partner"), fault=[mixed("origin", "partner")],
           warning="conversation_memory_receipt_rejected")
    recall("legacy_mismatch", None, deny=("legacy_first", "legacy_second"),
           fault=[mixed("legacy_first", "legacy_second")])
    resp = http.post("/api/chat/conversations", headers=headers[17], json={"title": "合成空索引工作群"})
    assert resp.status_code == 201
    groups["empty"] = resp.json()["data"]["conversation_id"]
    recall("no_hit", "empty", deny=("origin", "sibling", "other"))
    empty = cases["no_hit"]
    empty["checks"]["no_candidates"] = empty["candidate_ids"] == [] and "未找到相关历史对话" in empty["verified"]
    empty["passed"] = all(empty["checks"].values())
    for key in ("a", None):
        scope = "group" if key else "legacy"
        name = f"write_failure_{scope}"
        result, _, _ = turn(name, key, "验证索引故障后仍可提交回复", "add")
        cases[name] = {"passed": True, **result}
        recall(f"filter_failure_{scope}", key, deny=("origin", "sibling", "other", "legacy_first", "legacy_second"),
               fault="query", warning="conversation_memory_filter_failed")
    write(output / "cases.json", cases)


def serve(output, phase):
    configure(output)
    try:
        encoder, environment = preflight()
    except Exception as error:
        write(output / f"{phase}-blocked.json", {"status": "blocked", "interpreter": sys.executable,
                                               "model": MODEL, "revision": REVISION, "error": str(error)})
        raise

    import httpx
    import numpy as np
    import torch
    import uvicorn
    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.database import database
    from backend.memory import conversation_memory as memory

    torch.set_num_threads(2)
    observability.TRACE_LOG_FILE = output / "traces.jsonl"
    observability.FUNC_LOG_FILE = output / "functions.jsonl"
    observability.ERROR_LOG_FILE = output / "errors.jsonl"
    audit = Audit(output)
    database.init_db()
    if phase == "seed":
        with closing(database.get_db()) as conn, conn:
            conn.executemany("INSERT INTO users (id, username, password) VALUES (?, ?, ?)", [
                (owner, f"task25-{owner}", database.hash_password(f"fixture-{owner}")) for owner in (17, 18)
            ])
    chroma = memory._get_client()
    collection = memory._get_collection(chroma)
    actual_add, actual_query, actual_encode = collection.add, collection.query, encoder.encode
    operation = None

    def encode(texts, *args, **kwargs):
        vectors = actual_encode(texts, *args, **kwargs)
        audit.log("encode", operation=operation, texts=texts, normalized=kwargs.get("normalize_embeddings"),
                  shape=list(vectors.shape), vectors=[sha256(np.asarray(v, dtype="<f4").tobytes()).hexdigest() for v in vectors])
        return vectors

    def add(**kwargs):
        nonlocal operation
        operation = "add"
        meta, = kwargs["metadatas"]
        ids = (meta["user_message_id"], meta["assistant_message_id"])
        if meta.get("conversation_id"):
            committed_at_add = sql(output, "SELECT e.id, e.content, e.run_id, r.status FROM chat_group_entries e "
                                   "JOIN chat_group_runs r ON r.run_id=e.run_id WHERE e.id IN (?,?) ORDER BY e.id", ids)
            assert {row["status"] for row in committed_at_add} == {"completed"}
            assert len({row["run_id"] for row in committed_at_add}) == 1
        else:
            committed_at_add = sql(output, "SELECT id, content FROM conversations WHERE id IN (?,?) ORDER BY id", ids)
        assert tuple(row["id"] for row in committed_at_add) == ids
        audit.log("before_add", sql=committed_at_add, id=kwargs["ids"][0])
        if audit.fault == "add":
            audit.log("write_fault", kwargs=kwargs)
            raise RuntimeError(FAIL_DETAIL)
        return actual_add(**kwargs)

    def query(**kwargs):
        nonlocal operation
        operation = "query"
        if audit.fault == "query":
            audit.log("query", kwargs=kwargs, fault="filter_failure")
            raise RuntimeError(FAIL_DETAIL)
        native = actual_query(**kwargs)
        injected = audit.fault if isinstance(audit.fault, list) else None
        result = native if injected is None else {
            "ids": [[row["id"] for row in injected]], "documents": [[row["document"] for row in injected]],
            "metadatas": [[row["metadata"] for row in injected]], "distances": [[0.0 for _ in injected]],
        }
        audit.log("query", kwargs=kwargs, native=native, injected=injected, candidate_ids=result["ids"][0])
        return result

    class ShanghaiClock(datetime):
        @classmethod
        def now(cls, tz=None):
            now = datetime(2026, 11, 12, tzinfo=SHANGHAI)
            return now.replace(tzinfo=None) if tz is None else now.astimezone(tz)

    warnings = MemoryWarnings(audit)
    memory.logger.addHandler(warnings)
    with (patch.object(ProviderFactory, "create", return_value=audit),
          patch.object(memory, "_get_collection", return_value=collection),
          patch.object(collection, "add", side_effect=add), patch.object(collection, "query", side_effect=query),
          patch.object(encoder, "encode", side_effect=encode), patch.object(memory, "datetime", ShanghaiClock),
          socket.socket() as listener):
        from fast_server import app

        listener.bind(("127.0.0.1", 0))
        listener.listen()
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", timeout_graceful_shutdown=10))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 15
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.05)
            assert server.started
            audit.log("process_started", phase=phase, db=str(database.sandbox_db_path()),
                      chroma=str(database.sandbox_memory_root() / "conv_memory"), clock="2026-11-12T00:00:00+08:00",
                      transport="Uvicorn loopback HTTP", **environment)
            with httpx.Client(base_url=base_url, timeout=90, trust_env=False) as http:
                exercise(output, phase, audit, http, collection)
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive(), "server shutdown timed out"
            audit.log("process_stopped", phase=phase)
    memory.logger.removeHandler(warnings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("seed", "recall"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if args.phase:
        serve(output, args.phase)
        return 0
    output.mkdir(parents=True, exist_ok=False)
    stages = []
    for phase in ("seed", "recall"):
        with (output / f"{phase}.log").open("wb") as log:
            child = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "scripts.verify_task25_t1",
                                      "--output-dir", str(output), "--phase", phase], cwd=ROOT, stdout=log, stderr=log)
            code = child.wait()
        stages.append({"phase": phase, "launcher_pid": child.pid, "exit": code})
        if code:
            status = "blocked" if (output / f"{phase}-blocked.json").exists() else "failed"
            write(output / "report.json", {"status": status, "stages": stages, "real_glm_requests": 0})
            print(f"{status}: {output / (phase + '.log')}")
            return 2 if status == "blocked" else 1
    with (output / "ledger.jsonl").open(encoding="utf-8") as stream:
        lifecycle = [e for line in stream if (e := json.loads(line))["kind"] in {"process_started", "process_stopped"}]
    first, stopped, second, ended = lifecycle
    assert [(e["kind"], e["phase"]) for e in lifecycle] == [
        ("process_started", "seed"), ("process_stopped", "seed"),
        ("process_started", "recall"), ("process_stopped", "recall"),
    ]
    assert first["pid"] == stopped["pid"] != second["pid"] == ended["pid"]
    assert (first["db"], first["chroma"]) == (second["db"], second["chroma"])
    for stage, start in zip(stages, (first, second), strict=True):
        stage["server_pid"] = start["pid"]
    cases = read(output / "cases.json")
    failed = [name for name, case in cases.items() if not case["passed"]]
    write(output / "report.json", {"status": "failed" if failed else "passed", "stages": stages,
                                   "cases": len(cases), "failed": failed, "real_glm_requests": 0})
    print(f"{len(cases) - len(failed)}/{len(cases)} passed. Failures: {failed}. Evidence: {output}")
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
