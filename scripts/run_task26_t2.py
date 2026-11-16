"""Run frozen TASK-026-T2 cases against the ordinary-chat route and real DeepSeek."""

import argparse
import asyncio
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from functools import wraps
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import socket
import sqlite3
import sys
from threading import Thread
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from scripts.run_real_diagnosis import Redactor, SDKWireAudit, require
from scripts.task26_t2_replay import ARTIFACT, OUTPUT, PROVENANCE, SAMPLE, map_plan, read_json, sha, write_json


LIMIT = 1300
PASSWORD = "demo-password"
SMOKE = ("T2-001", "T2-003")


class CappedAudit(SDKWireAudit):
    """Reserve every outbound attempt, including SDK retries, across all processes."""

    def __init__(self, redactor, ledger, run_id):
        super().__init__(redactor, capture_reply=True, capture_fixture_inputs=True)
        self.ledger = ledger
        self.run_id = run_id
        self.case_id = None
        with closing(sqlite3.connect(ledger, timeout=30)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sdk_attempts (run_id TEXT, case_id TEXT)")

    def request(self, request):
        require(self.case_id is not None, "unassigned_sdk_attempt")
        require(request.url.scheme == "https" and request.url.host == "api.deepseek.com",
                "unexpected_sdk_host")
        with closing(sqlite3.connect(self.ledger, timeout=30)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT count(*) FROM sdk_attempts").fetchone()[0]
            require(count < LIMIT, "task26_t2_sdk_cap")
            conn.execute("INSERT INTO sdk_attempts VALUES (?, ?)", (self.run_id, self.case_id))
        super().request(request)


def username(case):
    return case["id"].lower()


def select_cases(sample, ids):
    by_id = {case["id"]: case for case in sample["cases"]}
    if ids == "formal-browser":
        return [case for case in by_id.values() if case["transport"] == "chromium"]
    if ids == "smoke-browser":
        return [by_id[SMOKE[0]]]
    if ids == "smoke-asgi":
        return [by_id[SMOKE[1]]]
    if ids.startswith("asgi-"):
        lane, total = map(int, ids.removeprefix("asgi-").split("/"))
        assert 1 <= lane <= total
        return [case for case in by_id.values() if case["transport"] == "asgi"][lane - 1::total]
    return [by_id[case_id] for case_id in ids.split(",")]


class Fixture:
    def __init__(self, output, cases, audit, redactor, rules, server):
        self.output = output
        self.cases = {case["message"]: case for case in cases}
        self.audit = audit
        self.redactor = redactor
        self.rules = rules
        self.server = server
        self.metric_reads = []
        self.knowledge_reads = []
        self.active = False

    def instrument(self, function):
        @wraps(function)
        def metric(*args, **kwargs):
            result = function(*args, **kwargs)
            self.metric_reads.append(dict(args=args, kwargs=kwargs, receipt=result))
            return result
        return metric

    def instrument_knowledge(self, generate):
        @wraps(generate)
        def retrieve(query, *args, **kwargs):
            item = {"query": query, "chunk_ids": []}
            self.knowledge_reads.append(item)
            try:
                result = generate(query, *args, **kwargs)
            except Exception as error:
                item["error"] = type(error).__name__
                raise
            if result:
                item["chunk_ids"] = [hit["chunk_id"] for hit in result["evidence"]]
            return result
        return retrieve

    async def __call__(self, scope, receive, send):
        from backend import observability
        from backend.config.config import settings
        from backend.database import database

        if scope["type"] != "http" or scope["path"] != "/api/chat/stream" or scope["method"] != "POST":
            await self.server.app(scope, receive, send)
            return

        first = await receive()
        chunks = [first.get("body", b"")]
        while first.get("more_body"):
            first = await receive()
            chunks.append(first.get("body", b""))
        first = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        body = json.loads(first["body"])
        case = self.cases[body["message"]]
        require(not self.active and "diagnosis" not in body, "fixture_requires_serial_ordinary_chat")
        folder = self.output / case["id"]
        folder.mkdir(parents=True, exist_ok=True)
        require(not (folder / "raw.json").exists(), "attempt_already_archived")
        self.active = True
        self.metric_reads = []
        self.knowledge_reads = []
        sdk_start = len(self.audit.rows)
        self.audit.case_id = case["id"]
        self.audit.trace_id = None
        events, committed = [], []
        http_status = None
        started = perf_counter()
        failure = None
        metrics = ARTIFACT
        fixture_hash = None

        async def relay_receive():
            nonlocal first
            if first is not None:
                value, first = first, None
                return value
            return await receive()

        async def relay_send(frame):
            nonlocal http_status, committed
            if frame["type"] == "http.response.start":
                http_status = frame["status"]
            for block in frame.get("body", b"").decode().split("\n\n"):
                if not block.startswith("data: "):
                    continue
                event = json.loads(block[6:])
                events.append(event)
                self.audit.trace_id = event.get("trace_id")
                if event["type"] in {"done", "error"}:
                    with closing(database.get_db(read_only=True)) as conn:
                        committed = [row[0] for row in conn.execute(
                            "SELECT content FROM conversations WHERE user_id=? AND role='assistant' ORDER BY id",
                            (case["plan_id"],),
                        )]
            await send(frame)

        try:
            if case["missing_date"]:
                metrics = folder / "missing-history.sqlite3"
                shutil.copyfile(ARTIFACT, metrics)
                with closing(sqlite3.connect(metrics)) as conn, conn:
                    deleted = conn.execute("DELETE FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date=?",
                                           (case["source_plan_id"], case["missing_date"])).rowcount
                    require(deleted == 1, "injected_day_must_exist")
                fixture_hash = sha(metrics)
            settings.PLAN_METRICS_DB_PATH = str(metrics)
            await self.server.app(scope, relay_receive, relay_send)
        except Exception as error:
            failure = type(error).__name__
        finally:
            trace_ids = sorted({event["trace_id"] for event in events if event.get("trace_id")})
            report = dict(case=case, request=body, events=events, http_status=http_status, failure=failure,
                          elapsed_seconds=round(perf_counter() - started, 3), committed_at_terminal=committed,
                          fixture_sha256=fixture_hash, metric_calls=self.metric_reads,
                          knowledge_calls=self.knowledge_reads,
                          sdk_requests=self.audit.rows[sdk_start:], rule_originals=self.rules,
                          traces=[observability.get_trace(trace_id) for trace_id in trace_ids])
            write_json(folder / "raw.json", self.redactor.clean(report))
            with closing(database.get_db()) as conn, conn:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (case["plan_id"],))
            self.audit.case_id = None
            self.active = False
            print(json.dumps({"case": case["id"], "sdk": len(report["sdk_requests"]),
                              "elapsed": report["elapsed_seconds"], "trace": trace_ids,
                              "terminal": events[-1]["type"] if events else None}), file=sys.__stdout__, flush=True)


async def asgi_run(fixture, cases):
    import httpx
    from backend.database import database

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture), base_url="http://test") as client:
        for case in cases:
            login = fixture.server.auth_instance.login(username(case), PASSWORD)
            require(login, "fixture_login_failed")
            try:
                await client.post("/api/chat/stream", json={"message": case["message"]},
                                  headers={"authorization": "Bearer " + login["token"]})
            finally:
                with closing(database.get_db()) as conn, conn:
                    conn.execute("DELETE FROM sessions WHERE user_id=?", (case["plan_id"],))


def chromium_server(fixture):
    import uvicorn

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(fixture, lifespan="off", log_level="warning"))

        def stop_on_stdin_close():
            sys.stdin.read()
            server.should_exit = True

        Thread(target=stop_on_stdin_close, daemon=True).start()
        print(f"DEMO_URL=http://127.0.0.1:{listener.getsockname()[1]}", file=sys.__stdout__, flush=True)
        server.run(sockets=[listener])


def run(args):
    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.config.config import settings
    from backend.database import database
    from backend.memory import conversation_memory
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import DEMO_RULES, seed_review_rules
    from backend.tools import tools
    from scripts.eval_review_retrieval import FixedReviewEmbedding
    from tests.home_diagnosis_server import demo_captcha

    provenance = read_json(PROVENANCE)
    sample = read_json(args.sample)
    require(sample["data_version"] == provenance["data_version"] and sha(ARTIFACT) == provenance["artifact_sha256"],
            "unexpected_history_version")
    cases = select_cases(sample, args.cases)
    config = settings.llm_config()
    endpoint = urlsplit(config.base_url or "")
    require(config.provider == "deepseek" and config.api_key and config.model == "deepseek-flash"
            and endpoint.scheme == "https" and endpoint.netloc == "api.deepseek.com",
            "official_deepseek_flash_configuration_required")
    args.output.mkdir(parents=True, exist_ok=True)
    require(not (args.output / "sandbox.sqlite3").exists(), "use_a_new_run_directory")
    settings.JWT_SECRET = token_urlsafe(32)
    settings.DIAGNOSIS_SANDBOX_DB_PATH = str(args.output / "sandbox.sqlite3")
    settings.RAG_DB_PATH = str(args.output / "knowledge")
    settings.PLAN_METRICS_DB_PATH = str(ARTIFACT)
    observability.TRACE_LOG_FILE = args.output / "traces.jsonl"
    observability.FUNC_LOG_FILE = observability.ERROR_LOG_FILE = Path(os.devnull)
    redactor = Redactor((config.api_key, settings.OPENAI_API_KEY, settings.JWT_SECRET))
    audit = CappedAudit(redactor, args.ledger, args.output.name)
    provider = ProviderFactory.create()
    audit.attach(provider)
    database.init_db()
    with closing(database.get_db()) as conn, conn:
        for case in cases:
            user = case["plan_id"]
            conn.execute("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                         (user, username(case), database.hash_password(PASSWORD)))
            map_plan(conn, user, case["plan_id"], case["source_plan_id"], provenance)
    write_json(args.output / "run.json", {"transport": args.transport, "cases": [case["id"] for case in cases],
                                        "sample_sha256": sha(args.sample), "model": config.model, "sdk_limit": LIMIT})
    with ExitStack() as stack:
        stack.callback(provider._client.close)
        stack.enter_context(patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding))
        stack.enter_context(patch.object(conversation_memory, "BGEEmbedding", FixedReviewEmbedding))
        stack.enter_context(patch("backend.captcha.captcha.generate_captcha", demo_captcha))
        import fast_server

        rag = rag_knowledge.get_rag_knowledge()
        seed_review_rules(rag)
        rules = {chunk_id: rag.get_chunk(chunk_id)["content"]
                 for chunk_id, _, version, _, _ in DEMO_RULES if version == "v1"}
        fixture = Fixture(args.output, cases, audit, redactor, rules, fast_server)
        stack.enter_context(patch.object(rag, "generate", fixture.instrument_knowledge(rag.generate)))
        stack.enter_context(patch.object(tools, "get_plan_metrics_by_date",
                                         fixture.instrument(tools.get_plan_metrics_by_date)))
        if args.transport == "asgi":
            asyncio.run(asgi_run(fixture, cases))
        else:
            chromium_server(fixture)
    require(sha(ARTIFACT) == provenance["artifact_sha256"], "public_history_changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transport", choices=("asgi", "chromium"))
    parser.add_argument("cases")
    parser.add_argument("output", type=lambda path: Path(path).resolve())
    parser.add_argument("--sample", type=Path, default=SAMPLE)
    parser.add_argument("--ledger", type=lambda path: Path(path).resolve(), default=OUTPUT / "sdk-ledger.sqlite3")
    args = parser.parse_args()
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            run(args)
    except Exception as error:
        from scripts.run_real_diagnosis import CheckFailed

        reason = str(error) if isinstance(error, CheckFailed) else type(error).__name__
        write_json(args.output / "harness-error.json", {"error": reason})
        print(reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
