"""Run the isolated G3 browser and process-exit acceptance against two Envoys."""

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import sqlite3
import subprocess
import sys
from time import monotonic, sleep, time_ns
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts import accept_task45_t1 as sql
from scripts import storepilot_gateway as gateway
from scripts import task45_t1_fixture as base
from scripts import task45_t3_fixture as fixture
from scripts.accept_task42_t1 import configure, write


MODEL = "fixed-t45-three-sku"
ORIGINS = ("http://127.0.0.1:18081", "http://127.0.0.1:18082")
CASES = ("reconnect", "lost-start", "kill-before", "kill-after", "version-change")
STAMP = datetime.fromisoformat(base.AS_OF + "T12:00:00+08:00")
FACT_STEP = f"{base.TOTE}/{sql.FACTS}"


def wait_for(call, label, seconds=32):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if value := call():
            return value
        sleep(0.05)
    raise AssertionError(f"Timed out: {label}")


def hold(folder, name):
    (folder / f"held-{name}").touch()
    wait_for(lambda: (folder / f"release-{name}").exists(), f"release {name}", 70)


def fixture_server(folder):
    import uvicorn
    from backend.captcha import captcha
    from backend.database import alert_review
    from tests.home_diagnosis_server import demo_captcha
    from fast_server import app

    journal = gateway.Journal(folder)
    with ExitStack() as stack:
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        stack.enter_context(patch.object(alert_review, "now", lambda: STAMP))
        gateway.install_wire_cut(stack)

        async def wire(scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            request_id = dict(scope["headers"]).get(b"x-request-id", b"").decode()
            path = scope["path"]
            stream = path.endswith("/events") and "/background-inspections/" in path and scope["method"] == "GET"

            async def observe_receive():
                message = await receive()
                if stream and message["type"] == "http.disconnect":
                    journal.mark("watch-disconnected", request_id=request_id)
                return message

            async def observe_send(message):
                if message["type"] == "http.response.start":
                    if (path.endswith("/background-inspections") and message["status"] == 202
                            and journal.signal("cut-start") and not journal.signal("start-response-lost")):
                        journal.mark("start-response-lost", request_id=request_id)
                        scope["storepilot.cut"]()
                        return
                    if stream:
                        cursor = dict(scope["headers"]).get(b"last-event-id", b"").decode()
                        journal.log("watch-headers", request_id=request_id, cursor=cursor,
                                    status=message["status"], run_id=path.split("/")[-2])
                await send(message)

            await app(scope, observe_receive, observe_send)

        uvicorn.run(wire, host="127.0.0.1", port=5010, http="h11", lifespan="off", access_log=False)


def fixed_provider(record):
    from scripts.accept_task45_t3 import Replay

    def complete(**payload):
        assert payload["model"] == MODEL
        record("fixed-model-review")
        frozen = SimpleNamespace(active={"case": "base", "group": "A"})
        content, calls = Replay.reply(frozen, payload)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))


def fixture_worker(folder, mode):
    from backend import observability
    from backend.agent import background_inspection as worker
    from backend.database import background_inspections as storage, listing_operations

    journal = gateway.Journal(folder)
    active = None
    original_execute = worker.execute
    original_checkpoint = storage.checkpoint
    original_validate = storage.validate

    def execute(job, provider):
        nonlocal active
        active = job
        write(folder / f"claim-{job['generation']}.json", job)
        return original_execute(job, provider)

    def record(event, **fields):
        journal.log(event, **{key: active[key] for key in ("run_id", "trace_id", "generation")}, **fields)

    def hold_for_kill():
        write(folder / "interrupted-trace.json", observability.get_trace(active["trace_id"]))
        hold(folder, "kill")

    def wrap(tool, reader):
        def read(conn, owner, **arguments):
            step = f"{arguments['sku']}/{tool}"
            record("bottom-tool-start", step=step, pid=os.getpid())
            if mode == "reconnect" and arguments["sku"] == base.TOTE:
                if tool == sql.SALES:
                    hold(folder, "first")
                if tool == sql.CURRENT:
                    hold(folder, "second")
            if mode in {"kill-before", "version-change"} and step == FACT_STEP:
                hold_for_kill()
            result = reader(conn, owner, **arguments)
            record("bottom-tool-return", step=step)
            return result
        return read

    def checkpoint(job, step, result, **link):
        artifact = original_checkpoint(job, step, result, **link)
        if mode == "kill-after" and step == FACT_STEP:
            write(folder / "committed-fact.json", {"artifact_id": artifact})
            hold_for_kill()
        return artifact

    def validate(conn, job):
        if mode == "resume" and not (folder / "held-resume").exists():
            hold(folder, "resume")
        return original_validate(conn, job)

    provider = fixed_provider(record)
    trace_file = "contender-traces.jsonl" if mode == "contender" else "executor-traces.jsonl"
    with ExitStack() as stack:
        for obj, name, value in [
            (observability, "TRACE_LOG_FILE", folder / trace_file),
            (worker, "execute", execute), (storage, "checkpoint", checkpoint), (storage, "validate", validate),
            (worker.ProviderFactory, "create", lambda: provider),
            (listing_operations, "read_sales", wrap(sql.SALES, listing_operations.read_sales)),
            (listing_operations, "read_listing", wrap(sql.CURRENT, listing_operations.read_listing)),
            (listing_operations, "read_facts", wrap(sql.FACTS, listing_operations.read_facts)),
        ]:
            stack.enter_context(patch.object(obj, name, value))
        worker.serve()


def snapshot(db, *, extra_tables=None):
    with closing(sql.sql_connection(db)) as conn:
        conn.execute("BEGIN")
        tables = {
            "runs": "chat_group_runs", "jobs": "background_inspections", "slots": "chat_group_run_slots",
            "answers": "chat_group_entries WHERE kind='assistant'", "answer_inserts": "g3_answer_inserts",
            "events": "background_inspection_events ORDER BY run_id, seq",
            "artifacts": "background_inspection_artifacts ORDER BY artifact_id",
            "checkpoints": "background_inspection_checkpoints ORDER BY step",
            "listing_proposals": "listing_change_proposals", "listing_receipts": "listing_change_receipts",
            "pause_proposals": "chat_group_pause_proposals", "pause_receipts": "chat_group_pause_receipts",
            "change_proposals": "chat_group_date_proposals", "product_mutations": "g3_product_mutations",
        }
        tables.update(extra_tables or {})
        result = {key: [dict(row) for row in conn.execute("SELECT * FROM " + table)] for key, table in tables.items()}
    for row in result["events"]:
        row.update(json.loads(row.pop("payload")))
    return result


def request(origin, path, *, token=None, data=None, headers=None):
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(origin + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    try:
        response = urlopen(req, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return {"status": response.status, "request_id": response.headers.get("x-request-id"),
                "body": json.loads(raw) if raw and "json" in response.headers.get("Content-Type", "") else None}


def login(origin, account, password):
    captcha_id = request(origin, "/api/captcha")["body"]["captcha_id"]
    return request(origin, "/api/login", data={"username": account, "password": password,
                   "captcha_id": captcha_id, "captcha_text": "0726"})["body"]["data"]["token"]


@contextmanager
def child(stage, folder, env, *, mode="normal", label=None):
    with (folder / f"{label or stage}.log").open("wb") as log:
        proc = subprocess.Popen([sys.executable, "-m", "scripts.storepilot_g3", stage, str(folder), "--mode", mode],
                                env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=12)


def seed(db, password):
    from backend.database import database

    sql.seed(db, database, password, fixture.source("base"))
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO users(username,password) VALUES (?,?)", ("g3-other", database.hash_password(password)))
        conn.execute("CREATE TABLE g3_answer_inserts (run_id TEXT, answer_id INTEGER)")
        conn.execute("""CREATE TRIGGER g3_answer_insert AFTER INSERT ON chat_group_entries WHEN new.kind='assistant'
                        BEGIN INSERT INTO g3_answer_inserts VALUES (new.run_id, new.id); END""")
        conn.execute("CREATE TABLE g3_product_mutations (table_name TEXT, operation TEXT)")
        for name in ("shops", "products", "facts", "sales", "required_facts"):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER g3_{name}_{operation} AFTER {operation} ON listing_test_{name}
                    BEGIN INSERT INTO g3_product_mutations VALUES ('{name}', '{operation}'); END""")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def verify(folder, case, before, final, api):
    [job], [run], [slot], [answer], [insert] = (final[key] for key in ("jobs", "runs", "slots", "answers", "answer_inserts"))
    run_id = job["run_id"]
    assert run["run_id"] == slot["run_id"] == answer["run_id"] == insert["run_id"] == run_id
    assert job["answer_id"] == answer["id"] == insert["answer_id"] and job["finalized"] == 1
    rows = final["events"]
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    assert rows[-1]["answer_id"] == answer["id"] and rows[-1]["status"] == run["status"]
    assert rows[-1]["released_ns"] == slot["released_ns"] and slot["released_ns"] > slot["acquired_ns"]
    assert len([row for row in rows if row["kind"] == "finished"]) == 1
    assert len([row for row in rows if row["kind"] == "accepted"]) == 1
    assert all(not final[key] for key in ("listing_proposals", "listing_receipts", "pause_proposals",
                                         "pause_receipts", "change_proposals"))
    assert final["product_mutations"] == ([{"operation": "UPDATE", "table_name": "facts"}] if case == "version-change" else [])
    transport = [json.loads(line) for line in (folder / "transport.jsonl").read_text(encoding="utf-8").splitlines()]
    tools = [row for row in transport if row["event"].startswith("bottom-tool-")]
    assert all(row["run_id"] == run_id for row in tools)
    actual_starts = [row["step"] for row in tools if row["event"] == "bottom-tool-start"]
    dispatches = [row for row in rows if row["kind"] == "step_started" and row.get("tool")]
    assert actual_starts == [row["step"] for row in dispatches]
    counts = {step: actual_starts.count(step) for step in sorted(set(actual_starts))}
    checkpoint_ids = {row["step"]: row["artifact_id"] for row in final["checkpoints"]}
    assert set(checkpoint_ids.values()) == {row["artifact_id"] for row in final["artifacts"]}
    reused = {row["step"]: row["artifact_id"] for row in rows if row["kind"] == "reused"}
    assert all(checkpoint_ids[step] == artifact for step, artifact in reused.items())
    if case == "version-change":
        assert run["status"] == "invalidated" and answer["details"] is None
        [invalid] = [row for row in rows if row["kind"] == "invalidated"]
        assert invalid["items"] == [{"sku": base.TOTE, "source": "facts", "expected_version": base.PRODUCTS[base.TOTE][2],
                                    "current_version": base.PRODUCTS[base.TOTE][2] + 1}]
        assert not any(row.get("generation") == 2 for row in tools)
    else:
        assert run["status"] == "completed" and len(checkpoint_ids) == 10 and len(counts) == 9
        assert sum(counts.values()) == (10 if case == "kill-before" else 9)
        [review] = [row for row in transport if row["event"] == "fixed-model-review"]
        assert review["run_id"] == run_id
        evidence = json.loads(answer["details"])
        assert evidence["priority"]["status"] == "selected"
        assert len(evidence["products"]) == 3 and all(row["review_complete"] for row in evidence["products"])
        for product in evidence["products"]:
            for tool, receipt in product["receipts"].items():
                assert receipt["artifact_id"] == checkpoint_ids[f"{product['sku']}/{tool}"]
    if before:
        [old_slot] = before["slots"]
        assert old_slot["run_id"] == run_id and old_slot["released_ns"] is None
        assert old_slot["acquired_ns"] == slot["acquired_ns"] and not before["answers"]
        committed_before = {row["step"]: row["artifact_id"] for row in before["checkpoints"]}
        trace = json.loads((folder / "interrupted-trace.json").read_text(encoding="utf-8"))
        [claim_1, claim_2] = [row for row in rows if row["kind"] == "claimed"]
        assert trace["trace_id"] == claim_1["trace_id"] != claim_2["trace_id"]
        [fault_call] = [row for row in trace["steps"] if row["stage"] == "tool_call" and row["action"] == sql.FACTS]
        assert fault_call["runtime_call_id"] == next(row["runtime_call_id"] for row in dispatches
                                                     if row["generation"] == 1 and row["step"] == FACT_STEP)
        if case == "kill-before":
            assert reused == committed_before and len(reused) == 2 and counts[FACT_STEP] == 2
            assert FACT_STEP not in committed_before
        elif case == "kill-after":
            assert reused == committed_before and len(reused) == 3 and counts[FACT_STEP] == 1
            assert committed_before[FACT_STEP] == json.loads((folder / "committed-fact.json").read_text(
                encoding="utf-8"))["artifact_id"]
    write(folder / "reconciliation.json", {"run_id": run_id, "status": run["status"], "answer_id": answer["id"],
          "answer_inserts": len(final["answer_inserts"]), "reused_steps": reused,
          "actual_reruns": {step: count for step, count in counts.items() if count > 1},
          "bottom_tool_counts": counts, "checkpoint_ids": checkpoint_ids, "api": api,
          "product_mutations": final["product_mutations"], "confirmed_slot": slot})
    return run_id


def run_case(root, case, seed_db, password):
    folder = root / case
    folder.mkdir()
    db = folder / "run.sqlite3"
    shutil.copyfile(seed_db, db)
    configure(db)
    env = {**os.environ, "STOREPILOT_BACKGROUND_MODEL": MODEL, "STOREPILOT_GROUP_INFLIGHT": "1:1",
           "STOREPILOT_TEST_PASSWORD": password, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    write(folder / "plan.json", {"origins": ORIGINS, "message": fixture.QUESTION, "fact_step": FACT_STEP})
    api, before = {}, None
    with child("serve", folder, env) as server:
        def healthy():
            if server.poll() is not None:
                raise AssertionError(f"Fixture API exited: {folder / 'serve.log'}")
            try:
                return all(request(origin, "/api/health")["status"] == 200 for origin in ORIGINS)
            except (URLError, OSError):
                return False
        wait_for(healthy, "both Envoys and the API", 60)
        with child("worker", folder, env, mode=case, label="worker-1") as worker:
            if case == "reconnect":
                with (folder / "browser.log").open("wb") as log:
                    subprocess.run(["node", "tests/storepilot_g3_browser.cjs", str(folder)],
                                   env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=100)
                api = json.loads((folder / "browser.json").read_text(encoding="utf-8"))
            else:
                token = login(ORIGINS[0], "task45-operator", password)
                other = login(ORIGINS[1], "g3-other", password)
                groups = "/api/chat/conversations"
                group = request(ORIGINS[0], groups, token=token, data={"title": case})["body"]["data"]["conversation_id"]
                path = f"{groups}/{group}/background-inspections"
                headers = {"Idempotency-Key": "g3-" + case}
                payload = {"message": fixture.QUESTION}
                if case == "lost-start":
                    (folder / "cut-start").touch()
                    api["dropped"] = request(ORIGINS[0], path, token=token, data=payload, headers=headers)
                    assert api["dropped"]["status"] in {502, 503}
                    assert (folder / "start-response-lost").exists()
                    [accepted] = snapshot(db)["jobs"]
                    run_id = accepted["run_id"]
                    api["replay"] = request(ORIGINS[1], path, token=token, data=payload, headers=headers)
                    assert api["replay"]["status"] == 200 and api["replay"]["body"]["data"]["run_id"] == run_id
                    api["conflict"] = request(ORIGINS[1], path, token=token, headers=headers,
                                              data={"message": fixture.QUESTION + " "})
                    assert api["conflict"]["status"] == 409
                else:
                    api["start"] = request(ORIGINS[0], path, token=token, data=payload, headers=headers)
                    assert api["start"]["status"] == 202
                    run_id = api["start"]["body"]["data"]["run_id"]
                    wait_for(lambda: (folder / "held-kill").exists(), "fault barrier")
                    before = snapshot(db)
                    write(folder / "sql-before-kill.json", before)
                    with child("worker", folder, env, mode="contender", label="worker-contender") as contender:
                        contender_trace = folder / "contender-traces.jsonl"
                        wait_for(lambda: contender_trace.exists() and '"lock_busy"' in contender_trace.read_text(
                            encoding="utf-8"), "contender skipped the held run")
                        assert contender.poll() is None
                    assert worker.poll() is None and snapshot(db)["jobs"][0]["generation"] == 1
                    worker.kill()
                    api["process_exit"] = {"pid": worker.pid, "returncode": worker.wait(timeout=12), "epoch_ns": time_ns()}
                    if case == "version-change":
                        with closing(sqlite3.connect(db)) as conn, conn:
                            assert conn.execute("UPDATE listing_test_facts SET version=version+1 WHERE sku=?",
                                                (base.TOTE,)).rowcount == 1
                    with child("worker", folder, env, mode="resume", label="worker-2") as resumed:
                        wait_for(lambda: (folder / "held-resume").exists(), "new executor claimed same run")
                        api["resumed_pid"] = resumed.pid
                        assert snapshot(db)["jobs"][0]["generation"] == 2
                        stale = subprocess.run([sys.executable, "-m", "scripts.storepilot_g3", "stale", str(folder)],
                                               env=env, capture_output=True, check=True, timeout=15)
                        api["stale_write_fenced"] = "stale generation rejected" in stale.stdout.decode("utf-8")
                        assert api["stale_write_fenced"]
                        (folder / "release-resume").touch()
                        wait_for(lambda: snapshot(db)["jobs"][0]["finalized"], "resumed run finalized")
                        wait_for(lambda: (folder / "executor-traces.jsonl").exists(), "resumed Trace was flushed")
                path = f"{path}/{run_id}"
                wait_for(lambda: snapshot(db)["jobs"][0]["finalized"], "run finalized")
                api["cross_account"] = [request(ORIGINS[1], path + suffix, token=other)["status"]
                                        for suffix in ("", "/events")]
                assert api["cross_account"] == [404, 404]
                first, second = [request(origin, path, token=token)["body"]["data"] for origin in ORIGINS]
                assert first == second and first["answer"]["entry_id"].startswith("entry:")
                api["answer_entry_id"] = first["answer"]["entry_id"]
            wait_for(lambda: (folder / "executor-traces.jsonl").exists(), "executor Trace was flushed")
        final = snapshot(db)
        write(folder / "sql-final.json", final)
        return verify(folder, case, before, final, api)


def audit(root):
    def read_json(path):
        return json.loads(path.read_text(encoding="utf-8"))

    def journal(path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("{") and line.endswith("}")]

    run_ids = read_json(root / "g3" / "run-ids.json")
    report = {}
    for case, run_id in run_ids.items():
        folder = root / "g3" / case
        sql_state = snapshot(folder / "run.sqlite3")
        summary = read_json(folder / "reconciliation.json")
        events = sql_state["events"]
        assert summary["run_id"] == run_id and all(row["run_id"] == run_id for row in events)
        claims = [row for row in events if row["kind"] == "claimed"]
        assert len(claims) == (1 if case in ("reconnect", "lost-start") else 2)
        traces = journal(folder / "executor-traces.jsonl")
        if len(claims) == 2:
            traces.insert(0, read_json(folder / "interrupted-trace.json"))
        assert [claim["trace_id"] for claim in claims] == [trace["trace_id"] for trace in traces]
        calls = {step["runtime_call_id"]: trace["trace_id"] for trace in traces for step in trace["steps"]
                 if step["stage"] == "tool_call"}
        completed = {row["runtime_call_id"]: row for row in events if row["kind"] == "step_completed" and row.get("tool")}
        joins = []
        for row in events:
            if row["kind"] == "step_started" and row.get("tool"):
                call_id = row["runtime_call_id"]
                assert calls[call_id] == row["trace_id"]
                checkpoint = completed.get(call_id, {})
                joins.append({**row, "completed_seq": checkpoint.get("seq"), "artifact_id": checkpoint.get("artifact_id")})
        report[run_id] = {"case": case, **summary, "trace_ids": list(dict.fromkeys(calls.values())),
                          "tool_attempts": joins, "events": events}

    if "reconnect" in run_ids:
        run = report[run_ids["reconnect"]]
        browser = run["api"]
        first, resumed = browser["watches"][:2]
        assert first["cursor"] == 0 and resumed["cursor"] == browser["disconnect_cursor"]
        assert browser["offline_checkpoint_seq"] == resumed["cursor"] + 1
        request_ids = (first["request_id"], resumed["request_id"])

        def gateway_records():
            records = {row["request_id"]: row for row in journal(root / "envoy.log")}
            return records if set(request_ids) <= records.keys() else None

        by_request = wait_for(gateway_records, "both Envoy request IDs are in the access log", 10)
        gateways = [by_request[key] for key in request_ids]
        assert [row["gateway"] for row in gateways] == ["8080", "8081"]
        assert all(row["route"] == "background-inspection-events" for row in gateways)
        assert gateways[0]["flags"] == "DC" and gateways[1]["status"] == 200
        run["envoy"] = gateways
    write(root / "g3-reconciliation.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("run", "serve", "worker", "stale", "audit"))
    parser.add_argument("folder", type=Path)
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--mode", default="normal")
    args = parser.parse_args()
    root = args.folder.resolve()
    if args.stage == "serve":
        return fixture_server(root)
    if args.stage == "worker":
        return fixture_worker(root, args.mode)
    if args.stage == "audit":
        return audit(root)
    if args.stage == "stale":
        from backend.database import background_inspections as storage

        job = json.loads((root / "claim-1.json").read_text(encoding="utf-8"))
        for write_late in (lambda: storage.checkpoint(job, "late", {}),
                           lambda: storage.finish(job, "completed", "late answer")):
            try:
                write_late()
            except storage.StaleExecution:
                continue
            raise AssertionError("Stale generation changed committed state")
        print("stale generation rejected")
        return
    root.mkdir(parents=True, exist_ok=False)
    password = token_urlsafe(24)
    seed_db = root / "seed.sqlite3"
    configure(seed_db)
    seed(seed_db, password)
    cases = (args.case,) if args.case else CASES
    run_ids = {}
    for case in cases:
        run_ids[case] = run_case(root, case, seed_db, password)
        print(case, run_ids[case], flush=True)
    write(root / "run-ids.json", run_ids)


if __name__ == "__main__":
    main()
