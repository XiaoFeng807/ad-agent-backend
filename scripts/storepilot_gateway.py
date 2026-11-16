"""Isolated StorePilot transport acceptance. Run from the repository root."""

import argparse
import asyncio
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
from secrets import token_urlsafe
import shutil
import sqlite3
import subprocess
import sys
from threading import Lock
from time import time_ns
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from scripts import accept_task45_t1 as listing, task45_t1_fixture as fixture
from scripts.accept_task42_t1 import configure, read, write
from scripts.verify_task27_t1 import FixedModel as AdReplay, MESSAGES as AD_MESSAGES


CASES = ("normal", "before-headers", "client-abort", "upstream-abort", "lost-done", "lost-confirm",
         "rollback", "idle-timeout", "max-duration", "ad-worker-abort")
BASE = next(case for case in fixture.cases() if case["id"] == "control-tote")
STREAM = "/api/chat/stream"
PAUSE = {"normal", "client-abort", "upstream-abort", "idle-timeout", "max-duration", "ad-worker-abort"}


def initialize(root):
    root.mkdir(parents=True, exist_ok=False)
    db = root / "seed.sqlite3"
    configure(db)
    from backend.database import database

    password = token_urlsafe(24)
    (root / "password").write_text(password, encoding="utf-8")
    sources = fixture.source("base")
    listing.seed(db, database, password, sources)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO ad_plans (id,user_id,plan_name,impressions,clicks) VALUES (101,1,'QA',1500,120)")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    write(root / "manifest.json", {
        "seed_sha256": listing.digest(db), "fixture_sha256": listing.digest(Path(fixture.__file__)),
        "fixed_model_sha256": listing.digest(Path(listing.__file__)), "scenario": BASE,
        "as_of": fixture.AS_OF, "windows": fixture.WINDOWS, "sources": sources,
        "sql_oracle": listing.oracle(db, fixture.WINDOWS),
    })


class Journal:
    def __init__(self, folder):
        self.folder = folder
        self.lock = Lock()

    def log(self, event, **fields):
        with self.lock, (self.folder / "transport.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"epoch_ns": str(time_ns()), "event": event, **fields}) + "\n")

    def signal(self, name):
        return (self.folder / name).exists()

    def mark(self, name, **fields):
        self.log(name, **fields)
        (self.folder / name).touch()


def sql_state(db):
    result = listing.snapshot(db)
    with closing(listing.sql_connection(db)) as conn:
        for key, query in {
            "runs": "SELECT * FROM chat_group_runs ORDER BY run_id",
            "answers": "SELECT * FROM chat_group_entries WHERE kind='assistant' ORDER BY id",
            "ad_proposals": "SELECT * FROM chat_group_pause_proposals ORDER BY action_id",
            "ad_receipts": "SELECT * FROM chat_group_pause_receipts ORDER BY receipt_id",
        }.items():
            result[key] = [dict(row) for row in conn.execute(query)]
        result["slots"] = []
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='chat_group_run_slots'").fetchone():
            result["slots"] = [dict(row) for row in conn.execute("SELECT * FROM chat_group_run_slots ORDER BY acquired_ns")]
    return result


def app_for_case(folder, case, stack):
    from backend import observability
    from backend.agent.llm_provider import ProviderFactory
    from backend.captcha import captcha
    from backend.config.config import settings
    from backend.database import chat_groups
    from backend.memory import conversation_memory
    from backend.rag import rag_knowledge
    from scripts.eval_review_retrieval import FixedReviewEmbedding
    from tests.home_diagnosis_server import demo_captcha

    journal = Journal(folder)
    replay = listing.Replay()
    replay.case = BASE["id"]
    ad_replay = AdReplay() if case == "ad-worker-abort" else None
    if ad_replay:
        ad_replay.case = "first"
    dated_accept = chat_groups.accept
    requested_at = datetime.fromisoformat(fixture.AS_OF + "T12:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))

    def accept(*args, **kwargs):
        run, context = dated_accept(*args, **kwargs)
        return replace(run, requested_at=requested_at), context

    def complete(**payload):
        system = payload["messages"][0]["content"]
        if case in PAUSE and "temporary worker" in system and not journal.signal("model-held"):
            from time import monotonic, sleep

            journal.mark("model-held")
            deadline = monotonic() + (120 if case == "max-duration" else 40)
            while not journal.signal("release-model") and monotonic() < deadline:
                sleep(0.02)
            if not journal.signal("release-model"):
                raise TimeoutError("Edge did not release the model barrier")
            journal.log("model-released")
        if ad_replay:
            response = ad_replay.create(**payload)
            from backend.agent.request_budget import tool_call_fields

            message = response.choices[0].message
            with replay.lock:
                replay.requests.append({"role": "ad-review", "response": {
                    "content": message.content, "tool_calls": [tool_call_fields(call) for call in message.tool_calls or []],
                }})
            return response
        return replay.create(**payload)

    stack.enter_context(patch.object(replay.chat.completions, "create", complete))
    for obj, attr, value in (
        (ProviderFactory, "create", lambda: replay), (captcha, "generate_captcha", demo_captcha),
        (chat_groups, "accept", accept), (settings, "RAG_DB_PATH", str(folder / "chroma")),
        (rag_knowledge, "BGEEmbedding", FixedReviewEmbedding),
        (conversation_memory, "BGEEmbedding", FixedReviewEmbedding),
        (observability, "TRACE_LOG_FILE", folder / "traces.jsonl"),
        (observability, "FUNC_LOG_FILE", folder / "functions.jsonl"),
        (observability, "ERROR_LOG_FILE", folder / "errors.jsonl"),
        (observability, "PRODUCT_RERANK_LATE_FILE", folder / "rerank.jsonl"),
    ):
        stack.enter_context(patch.object(obj, attr, value))
    # Initialize independent Chroma state with the local embedding model.
    rag_knowledge.get_rag_knowledge()
    from fast_server import app

    return WireFaults(app, case, journal), replay


class WireFaults:
    def __init__(self, app, case, journal, *, heartbeats=False):
        self.app, self.case, self.journal = app, case, journal
        self.heartbeats = heartbeats or case == "max-duration"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        stream = scope["method"] == "POST" and path == STREAM
        confirm = scope["method"] == "POST" and "/listing-actions/" in path and path.endswith("/confirm")
        if not (stream or confirm):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        request_id = headers.get(b"x-request-id") or str(uuid4()).encode("ascii")
        scope = {**scope, "headers": [(key, value) for key, value in scope["headers"] if key != b"x-request-id"] +
                 [(b"x-request-id", request_id)]}
        fields = {"request_id": request_id.decode("ascii"), "route": "stream" if stream else "listing-confirm"}
        controls = sorted(key.decode("ascii") for key in headers if key.startswith(b"x-envoy-") or key == b"grpc-timeout")
        if stream and self.case == "before-headers":
            self.journal.mark("fault-triggered", **fields)
            scope["storepilot.cut"]()
            return
        self.journal.log("fastapi-entry", controls=controls, **fields)
        progress_sent = asyncio.Event()
        send_lock = asyncio.Lock()

        async def observe_receive():
            message = await receive()
            if message["type"] == "http.disconnect":
                self.journal.mark("app-disconnected", **fields)
            return message

        async def hold_and_cut(marker):
            self.journal.mark(marker, **fields)
            async with asyncio.timeout(30):
                while not self.journal.signal("cut-upstream"):
                    await asyncio.sleep(0.02)
            self.journal.mark("fault-triggered", **fields)
            scope["storepilot.cut"]()

        async def cut_while_running():
            while not self.journal.signal("cut-upstream"):
                await asyncio.sleep(0.02)
            self.journal.mark("fault-triggered", **fields)
            scope["storepilot.cut"]()

        async def send_heartbeats():
            await progress_sent.wait()
            while True:
                await asyncio.sleep(1)
                async with send_lock:
                    await send({"type": "http.response.body", "body": b": gateway-test heartbeat\n\n", "more_body": True})
                self.journal.log("test-heartbeat", **fields)

        async def observe_send(message):
            if message["type"] == "http.response.start":
                self.journal.log("app-response-headers", status=message["status"], **fields)
                if confirm and self.case == "lost-confirm" and not self.journal.signal("confirm-held"):
                    await hold_and_cut("confirm-held")
                    return
                message = {**message, "headers": [*message["headers"], (b"x-request-id", request_id)]}
            elif stream and self.case == "lost-done" and message.get("body", b"").startswith(b'data: {"type": "done"'):
                await hold_and_cut("done-held")
                return
            async with send_lock:
                await send(message)
            if stream and message.get("body", b"").startswith(b'data: {"type": "thinking"'):
                progress_sent.set()

        monitor = None
        if stream and self.case == "upstream-abort":
            monitor = asyncio.create_task(cut_while_running())
        if stream and self.heartbeats:
            monitor = asyncio.create_task(send_heartbeats())
        try:
            await self.app(scope, observe_receive, observe_send)
        finally:
            if monitor:
                monitor.cancel()
            self.journal.log("app-exit", **fields)


def install_wire_cut(stack):
    from uvicorn.protocols.http import h11_impl

    class TestCycle(h11_impl.RequestResponseCycle):
        async def run_asgi(self, app):
            def cut():
                self.disconnected = True
                self.message_event.set()
                self.transport.abort()

            self.scope["storepilot.cut"] = cut
            return await super().run_asgi(app)

    stack.enter_context(patch.object(h11_impl, "RequestResponseCycle", TestCycle))


def run(root, label, case, bind, origin, *, demo=False):
    import socket
    from threading import Thread
    from time import monotonic, sleep
    import uvicorn

    seed = root / "seed.sqlite3"
    assert listing.digest(seed) == read(root / "manifest.json")["seed_sha256"], "Seed file changed"
    folder = root / label / case
    folder.mkdir(parents=True, exist_ok=demo)
    db = folder / "run.sqlite3"
    if not demo or not db.exists():
        shutil.copyfile(seed, db)
    write(folder / "sql-before.json", sql_state(db))
    configure(db)
    os.environ["RAG_DB_PATH"] = str(folder / "chroma")
    host, port = bind.rsplit(":", 1)
    if case == "rollback":
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("""CREATE TRIGGER inject_receipt_failure BEFORE INSERT ON listing_change_receipts
                            BEGIN SELECT RAISE(ABORT, 'injected receipt INSERT failure'); END""")
    write(folder / "environment.json", {"os": platform.platform(), "python": sys.version, "sqlite": sqlite3.sqlite_version,
          "packages": {name: version(name) for name in ("fastapi", "starlette", "uvicorn", "anyio", "chromadb", "h11")},
          "node": subprocess.check_output(["node", "--version"], text=True).strip(),
          "seed_copy_sha256": listing.digest(seed), "origin": origin or "direct loopback", "entrypoint": "fast_server:app"})
    write(folder / "case.json", {"case": case, "ad_message": AD_MESSAGES["first"]})
    with ExitStack() as stack, socket.socket() as listener:
        app, replay = app_for_case(folder, case, stack)
        install_wire_cut(stack)
        listener.bind((host, int(port)))
        listener.listen()
        config = uvicorn.Config(app, host=host, port=int(port), http="h11", loop="asyncio", lifespan="off",
                                access_log=False, timeout_graceful_shutdown=7)
        server = uvicorn.Server(config)
        if demo:
            server.run(sockets=[listener])
            return 0
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 15
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.025)
            assert server.started, "Local FastAPI failed to start"
            if origin:
                from urllib.error import URLError
                from urllib.request import urlopen

                deadline = monotonic() + 15
                while True:
                    try:
                        with urlopen(origin + "/api/health", timeout=2):
                            break
                    except (URLError, TimeoutError):
                        if monotonic() >= deadline:
                            raise
                        sleep(0.15)
            with (folder / "edge.log").open("wb") as out:
                result = subprocess.run(["node", "tests/storepilot_gateway_browser.cjs",
                                         origin or f"http://127.0.0.1:{listener.getsockname()[1]}", str(folder), case],
                                        env={**os.environ, "STOREPILOT_TEST_PASSWORD": (root / "password").read_text(
                                            encoding="utf-8")}, stdout=out, stderr=subprocess.STDOUT, timeout=155)
        finally:
            (folder / "release-model").touch()
            (folder / "cut-upstream").touch()
            server.should_exit = True
            thread.join(9)
            write(folder / "sql-after.json", sql_state(db))
            write(folder / "model-responses.json", [{"role": row["role"], "response": row["response"]}
                                                     for row in replay.requests])
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("init", "run", "serve"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--label", default="direct")
    parser.add_argument("--case", choices=CASES, default="normal")
    parser.add_argument("--bind", default="127.0.0.1:0")
    parser.add_argument("--origin")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.stage == "init":
        initialize(root)
        return 0
    return run(root, args.label, "demo" if args.stage == "serve" else args.case, args.bind, args.origin,
               demo=args.stage == "serve")


if __name__ == "__main__":
    raise SystemExit(main())
