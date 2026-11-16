"""Run the isolated StorePilot G2 fixture behind the two real Envoys."""

import argparse
from contextlib import ExitStack, closing
from contextvars import ContextVar
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from threading import Lock, Thread
from time import monotonic, sleep
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen

from scripts import storepilot_gateway as g1
from scripts.accept_task42_t1 import configure, read, write


ACCOUNTS = ("task45-operator", "g2-beta", "g2-gamma")
MATRIX = {"get_lanes": 4, "get_interval_ms": 200, "get_ticks": 30,
          "get_duration_ms": 6000, "agent_submissions": 6, "repeats": 3}


def initialize(root):
    g1.initialize(root)
    from backend.database import database

    db = root / "seed.sqlite3"
    password = (root / "password").read_text(encoding="utf-8")
    with closing(sqlite3.connect(db)) as conn, conn:
        for account in ACCOUNTS[1:]:
            user = conn.execute("INSERT INTO users(username, password) VALUES (?, ?)",
                                (account, database.hash_password(password))).lastrowid
            conn.execute("INSERT INTO listing_test_shops VALUES (?, ?, ?)", (account, g1.fixture.MARKET, user))
            for table, fields in (
                ("products", "version, title, bullets, price_cents, inventory_units"),
                ("sales", "version, series"), ("facts", "version, facts"), ("required_facts", "fields"),
            ):
                conn.execute(f"""INSERT INTO listing_test_{table} SELECT ?, marketplace_id, sku, {fields}
                                FROM listing_test_{table} WHERE shop_id=?""", (account, g1.fixture.SHOP))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    manifest = read(root / "manifest.json")
    write(root / "manifest.json", {**manifest, "seed_sha256": g1.listing.digest(db),
                                   "accounts": ACCOUNTS, "load": MATRIX})


def setup_barriers(folder, journal, replay, stack):
    from backend.agent.team_runtime import DelegationRun
    from backend.database import chat_groups, listing_operations
    from backend.observability import add_trace_step

    worker = ContextVar("g2_worker", default=None)
    modes = {}
    held = set()
    held_lock = Lock()
    accept_run = chat_groups.accept
    work = DelegationRun._work
    complete = replay.chat.completions.create
    read_facts = listing_operations.read_facts
    save_reply = chat_groups.GroupRun.save_reply

    def accept(*args, **kwargs):
        run, context = accept_run(*args, **kwargs)
        control = folder / f"mode-{run.conversation_id}"
        if control.is_file():
            modes[run.run_id] = control.read_text(encoding="utf-8").strip()
        return run, context

    def work_in_context(self, task, inputs, direct_inputs, attempt, trace_id, worker_id):
        token = worker.set((self._board.run_id, trace_id, worker_id))
        try:
            return work(self, task, inputs, direct_inputs, attempt, trace_id, worker_id)
        finally:
            worker.reset(token)

    def hold(kind, call):
        identity = worker.get()
        if identity is None:
            return call()
        run_id, trace_id, worker_id = identity
        if modes.get(run_id) != kind:
            return call()
        with held_lock:
            first = run_id not in held
            held.add(run_id)
        if not first:
            return call()
        ids = {"run_id": run_id, "trace_id": trace_id, "worker_id": worker_id}
        add_trace_step(trace_id, "fixture_call", f"{kind}_held", runtime_call_id=worker_id, evidence=ids)
        journal.mark(f"held-{run_id}", **ids, kind=kind)
        deadline = monotonic() + 45
        try:
            while not journal.signal(f"release-{run_id}") and not journal.signal("release-all"):
                if monotonic() >= deadline:
                    raise TimeoutError(f"G2 {kind} barrier was not released: {run_id}")
                sleep(0.01)
            return call()
        finally:
            add_trace_step(trace_id, "fixture_call", f"{kind}_exited", runtime_call_id=worker_id, evidence=ids)
            journal.log("blocked-call-exit", **ids, kind=kind)

    def save(run, *args, **kwargs):
        if modes.get(run.run_id) == "fail":
            raise sqlite3.OperationalError("injected reply write failure")
        return save_reply(run, *args, **kwargs)

    for obj, attr, value in (
        (chat_groups, "accept", accept), (DelegationRun, "_work", work_in_context),
        (replay.chat.completions, "create", lambda **payload: hold("model", lambda: complete(**payload))),
        (listing_operations, "read_facts", lambda *args, **kwargs: hold("tool", lambda: read_facts(*args, **kwargs))),
        (chat_groups.GroupRun, "save_reply", save),
    ):
        stack.enter_context(patch.object(obj, attr, value))


def run(args, root):
    import uvicorn

    seed = root / "seed.sqlite3"
    manifest = read(root / "manifest.json")
    assert g1.listing.digest(seed) == manifest["seed_sha256"]
    folder = root / args.label
    folder.mkdir()
    db = folder / "run.sqlite3"
    shutil.copyfile(seed, db)
    configure(db)
    os.environ["STOREPILOT_GROUP_INFLIGHT"] = {"A": "0:0", "B": "2:3"}[args.mode]
    os.environ["STOREPILOT_STARTS_PER_SECOND"] = "1" if args.case == "rate" else "100"
    os.environ["RAG_DB_PATH"] = str(folder / "chroma")
    write(folder / "plan.json", {"mode": args.mode, "case": args.case, "origins": args.origins,
                                 "group_inflight": os.environ["STOREPILOT_GROUP_INFLIGHT"],
                                 "starts_per_second": int(os.environ["STOREPILOT_STARTS_PER_SECOND"]),
                                 "seed_sha256": manifest["seed_sha256"], **manifest["load"]})
    write(folder / "sql-before.json", g1.sql_state(db))
    with ExitStack() as stack:
        case = "lost-confirm" if args.case == "approval" else "g2"
        app, replay = g1.app_for_case(folder, case, stack)
        app.heartbeats = True
        setup_barriers(folder, app.journal, replay, stack)
        g1.install_wire_cut(stack)
        host, port = args.bind.rsplit(":", 1)
        config = uvicorn.Config(app, host=host, port=int(port), http="h11", loop="asyncio", lifespan="off",
                                access_log=False, timeout_graceful_shutdown=10)
        server = uvicorn.Server(config)
        thread = Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 20
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.025)
            assert server.started, "Local FastAPI failed to start"
            for origin in args.origins:
                deadline = monotonic() + 15
                while True:
                    try:
                        with urlopen(origin + "/api/health", timeout=2):
                            break
                    except (URLError, TimeoutError):
                        if monotonic() >= deadline:
                            raise
                        sleep(0.1)
            with (folder / "edge.log").open("wb") as out:
                result = subprocess.run(["node", "tests/storepilot_g2_browser.cjs", str(folder)],
                                        env={**os.environ, "STOREPILOT_TEST_PASSWORD": (root / "password").read_text(
                                            encoding="utf-8")}, stdout=out, stderr=subprocess.STDOUT, timeout=150)
        finally:
            app.journal.mark("release-all")
            (folder / "cut-upstream").touch()
            server.should_exit = True
            thread.join(15)
            assert not thread.is_alive(), "Fixture server still has live requests"
            final = g1.sql_state(db)
            write(folder / "sql-after.json", final)
            write(folder / "model-responses.json", [{"role": row["role"], "response": row["response"]}
                                                     for row in replay.requests])
        if result.returncode == 0:
            from backend.database.chat_groups import GroupRun

            for slot in final["slots"]:
                [row] = [row for row in final["runs"] if row["run_id"] == slot["run_id"]]
                run = GroupRun(slot["user_id"], row["conversation_id"], row["run_id"], row["trace_id"], 0)
                assert run.drain() == (row["status"], None), row
            assert g1.sql_state(db) == final, "Repeated cleanup changed SQLite state"
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("init", "run"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--mode", choices=("A", "B"), default="B")
    parser.add_argument("--case", choices=("mix", "lifecycle", "approval", "rate"), default="mix")
    parser.add_argument("--label", default="trial")
    parser.add_argument("--bind", default="127.0.0.1:5010")
    parser.add_argument("--origins", nargs=2, default=["http://127.0.0.1:18081", "http://127.0.0.1:18082"])
    args = parser.parse_args()
    root = args.root.resolve()
    if args.stage == "init":
        initialize(root)
        return 0
    return run(args, root)


if __name__ == "__main__":
    raise SystemExit(main())
