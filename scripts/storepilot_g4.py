"""Exercise two StorePilot executors against one isolated SQLite behind the G2 Envoys."""

import argparse
from contextlib import ExitStack, closing, contextmanager
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import signal
import sqlite3
import subprocess
import sys
from time import time_ns
from unittest.mock import patch
from urllib.error import URLError

from scripts import storepilot_g3 as g3
from scripts.accept_task42_t1 import configure, write
from scripts.storepilot_gateway import Journal


RACE_CASES = ("race", "race-stale")
CASES = (*RACE_CASES, "kill-before", "kill-after", "final-commit")
FACT = g3.FACT_STEP
SALES = f"{g3.base.TOTE}/{g3.sql.SALES}"
CURRENT = f"{g3.base.TOTE}/{g3.sql.CURRENT}"
AUDIT = {"releases": "g4_releases", "final_inserts": "g4_final_inserts"}
EXECUTION = ("generation", "executor_id", "trace_id")
SCAN_ACTIONS = {"claim_attempt", "claimed", "lock_busy", "no_longer_pending"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as log:
        return [json.loads(line) for line in log if line.endswith("\n")]


def snapshot(folder, name=None):
    db = folder / "run.sqlite3"
    with closing(g3.sql.sql_connection(db)) as conn:
        claim_writes = conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='g4_claim_writes'").fetchone()
    tables = {**AUDIT, "claim_writes": "g4_claim_writes ORDER BY rowid"} if claim_writes else AUDIT
    result = g3.snapshot(db, extra_tables=tables)
    if name:
        write(folder / f"sql-{name}.json", result)
    return result


def seed(db, password):
    g3.seed(db, password)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript("""
            CREATE TABLE g4_releases (run_id TEXT, released_ns INTEGER);
            CREATE TRIGGER g4_release AFTER UPDATE OF released_ns ON chat_group_run_slots
            WHEN new.released_ns IS NOT NULL
            BEGIN INSERT INTO g4_releases VALUES (new.run_id, new.released_ns); END;
            CREATE TABLE g4_final_inserts (run_id TEXT, seq INTEGER);
            CREATE TRIGGER g4_final_insert AFTER INSERT ON background_inspection_events
            WHEN new.kind='finished'
            BEGIN INSERT INTO g4_final_inserts VALUES (new.run_id, new.seq); END;
            CREATE TABLE g4_claim_writes (run_id TEXT, previous_generation INTEGER, generation INTEGER, executor_id TEXT);
            CREATE TRIGGER g4_claim_write AFTER UPDATE OF generation, executor_id ON background_inspections
            BEGIN INSERT INTO g4_claim_writes VALUES (new.run_id, old.generation, new.generation, new.executor_id); END;
        """)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def fixture_worker(folder, case, label):
    from backend import observability
    from backend.agent import background_inspection as worker
    from backend.database import background_inspections as storage, listing_operations

    here = folder / label
    journal = Journal(here)
    active = None
    receipt = None
    race_ready = False
    followup_scanned = False
    plan = read(folder / "plan.json")
    first = plan["first_run_id"]
    original_lock, original_execute = worker.executor_lock, worker.execute
    original_pending, original_checkpoint, original_finish = storage.pending, storage.checkpoint, storage.finish
    original_claim = storage.claim

    def record(event, **fields):
        journal.log(event, label=label, pid=os.getpid(),
                    **{key: active[key] for key in ("run_id", "trace_id", "executor_id", "generation")}, **fields)

    def save_trace():
        write(here / "interrupted-trace.json", observability.get_trace(active["trace_id"]))

    @contextmanager
    def lock(directory, run_id):
        nonlocal race_ready
        if case in RACE_CASES and not race_ready:
            assert run_id == first
            race_ready = True
            journal.mark("race-ready", pid=os.getpid(), run_id=run_id, label=label)
            g3.wait_for(lambda: (folder / "release-race").exists(), "shared A/B barrier")
        with original_lock(directory, run_id) as acquired:
            yield acquired
        if case in RACE_CASES and acquired and run_id == first and active and active["run_id"] == first:
            journal.mark("race-first-unlocked", label=label, pid=os.getpid(), run_id=first,
                         trace_id=active["trace_id"])

    def pending():
        nonlocal followup_scanned
        # Let the other process complete its own run before controlling its next scan of the first run.
        if case in RACE_CASES and active and active["run_id"] != first and not followup_scanned:
            followup_scanned = True
            stale = case == "race-stale"
            rows = original_pending() if stale else None
            journal.mark("race-scan-gated", label=label, pid=os.getpid(), run_id=first, pending=rows)
            g3.wait_for(lambda: journal.signal("release-race-scan"), "release follow-up scan")
            if not stale:
                rows = original_pending()
            journal.mark("race-scan-returned", label=label, pid=os.getpid(), run_id=first, pending=rows)
            return rows
        probe = (here / "probe-scan").exists() and not journal.signal("scan-observed")
        rows = original_pending()
        if probe:
            journal.mark("scan-observed", label=label, pid=os.getpid(), run_id=plan["first_run_id"], pending=rows)
        return rows

    def claim(run_id, executor_id, trace_id):
        job = original_claim(run_id, executor_id, trace_id)
        if case == "race-stale" and run_id == first and followup_scanned:
            journal.log("race-claim-rechecked", label=label, pid=os.getpid(), run_id=run_id, trace_id=trace_id,
                        executor_id=executor_id, generation=job["generation"] if job else None)
        return job

    def execute(job, provider):
        nonlocal active
        active = job
        write(here / f"claim-{job['run_id']}-{job['generation']}.json", job)
        record("execute_entered")
        if label == "B" and case in {"kill-before", "kill-after"}:
            g3.hold(here, "takeover")
        original_execute(job, provider)

    def wrap(tool, reader):
        def run(conn, owner, **arguments):
            nonlocal receipt
            step = f"{arguments['sku']}/{tool}"
            tool_calls = observability.get_trace(active["trace_id"])["steps"]
            call_id = next(row["runtime_call_id"] for row in reversed(tool_calls) if row["stage"] == "tool_call")
            record("bottom-tool-start", step=step, runtime_call_id=call_id)
            result = reader(conn, owner, **arguments)
            record("bottom-tool-return", step=step, runtime_call_id=call_id)
            if case in RACE_CASES and active["run_id"] == first and step == SALES:
                g3.hold(here, "race-tool")
            if label == "A" and step == FACT and case in {"kill-before", "kill-after"}:
                receipt = {"step": step, "tool": tool, "runtime_call_id": call_id, "result": result}
                write(here / "receipt.json", receipt)
                if case == "kill-before":
                    save_trace()
                    g3.hold(here, "fault")
            return result
        return run

    def checkpoint(job, step, result, **link):
        artifact = original_checkpoint(job, step, result, **link)
        record("checkpoint_committed", step=step, artifact_id=artifact, **link)
        if label == "A" and case == "kill-after" and step == FACT:
            assert receipt["runtime_call_id"] == link["runtime_call_id"]
            save_trace()
            g3.hold(here, "fault")
        return artifact

    def finish(job, status, *args, **kwargs):
        if label == "A" and case == "final-commit":
            assert status == "completed"
            g3.hold(here, "before-final")
        result = original_finish(job, status, *args, **kwargs)
        record("final_committed", status=result[0], released_ns=result[1])
        if label == "A" and case == "final-commit":
            save_trace()
            g3.hold(here, "final-committed")
        return result

    with ExitStack() as stack:
        for obj, name, value in (
            (observability, "TRACE_LOG_FILE", here / "traces.jsonl"),
            (worker, "executor_lock", lock), (worker, "execute", execute),
            (storage, "pending", pending), (storage, "claim", claim),
            (storage, "checkpoint", checkpoint), (storage, "finish", finish),
            (worker.ProviderFactory, "create", lambda: g3.fixed_provider(record)),
            (listing_operations, "read_sales", wrap(g3.sql.SALES, listing_operations.read_sales)),
            (listing_operations, "read_listing", wrap(g3.sql.CURRENT, listing_operations.read_listing)),
            (listing_operations, "read_facts", wrap(g3.sql.FACTS, listing_operations.read_facts)),
        ):
            stack.enter_context(patch.object(obj, name, value))
        journal.mark("worker-online", pid=os.getpid(), label=label, run_id=plan["first_run_id"])
        worker.serve()


def inject(folder):
    from backend import observability
    from backend.database import background_inspections as storage

    run_id = read(folder / "plan.json")["first_run_id"]
    job = read(folder / "A" / f"claim-{run_id}-1.json")
    receipt = read(folder / "A" / "receipt.json")
    calls = {
        "checkpoint": lambda: storage.checkpoint(job, receipt["step"], receipt["result"],
                                                 tool=receipt["tool"], runtime_call_id=receipt["runtime_call_id"]),
        "finish": lambda: storage.finish(job, "completed", "注入旧代次最终答案", {"receipt": receipt}),
    }
    with patch.object(observability, "TRACE_LOG_FILE", folder / "injection-traces.jsonl"):
        for name, call in calls.items():
            try:
                call()
            except storage.StaleExecution as error:
                assert str(error) == "generation_mismatch"
                Journal(folder).log("fault_injection_rejected", run_id=run_id, operation=name,
                                    executor_id=job["executor_id"], generation=job["generation"], pid=os.getpid(),
                                    runtime_call_id=receipt["runtime_call_id"], reason=str(error))
            else:
                raise AssertionError(f"Unexpected success: {name}")


@contextmanager
def start_worker(folder, case, label, env):
    here = folder / label
    here.mkdir()
    with (here / "worker.log").open("wb") as log:
        proc = subprocess.Popen([sys.executable, "-m", "scripts.storepilot_g4", "worker", str(folder),
                                 "--case", case, "--label", label], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            yield proc
        finally:
            if proc.poll() is None:
                online = lines(here / "transport.jsonl")
                terminate(proc, online[0]["pid"] if online else proc.pid)
            proc.wait(timeout=12)


def terminate(proc, pid):
    if proc.pid == pid:
        proc.kill()
    else:
        # On Windows the venv launcher waits for its Python child and forwards its exit code.
        os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)


def kill(folder, label, proc, run_id, job):
    [execution] = [row for row in lines(folder / label / "transport.jsonl")
                   if row["event"] == "execute_entered" and row["trace_id"] == job["trace_id"]]
    fields = {"pid": execution["pid"], "launcher_pid": proc.pid, "label": label,
              **{key: job[key] for key in ("executor_id", "generation", "trace_id")}}
    journal = Journal(folder)
    journal.log("kill_requested", run_id=run_id, **fields)
    terminate(proc, execution["pid"])
    journal.log("process_exit_observed", run_id=run_id, returncode=proc.wait(timeout=12), **fields)


def skipped(folder, label, run_id, action):
    return any(trace["status"] == "skipped" and any(
        step["action"] == action and step.get("runtime_call_id") == run_id for step in trace["steps"])
        for trace in lines(folder / label / "traces.jsonl"))


def run_case(root, case, seed_db, password):
    folder = root / case
    folder.mkdir()
    db = folder / "run.sqlite3"
    shutil.copyfile(seed_db, db)
    configure(db)
    env = {**os.environ, "STOREPILOT_BACKGROUND_MODEL": g3.MODEL, "STOREPILOT_GROUP_INFLIGHT": "2:2",
           "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    origin_a, origin_b = g3.ORIGINS
    wait_for = g3.wait_for
    with g3.child("serve", folder, env) as server, ExitStack() as workers:
        def healthy():
            assert server.poll() is None, f"Fixture server exited: {folder / 'serve.log'}"
            try:
                return all(g3.request(origin, "/api/health")["status"] == 200 for origin in g3.ORIGINS)
            except (URLError, OSError):
                return False
        wait_for(healthy, "both Envoys and API", 60)
        token = g3.login(origin_a, "task45-operator", password)

        def start(origin, name):
            groups = "/api/chat/conversations"
            group = g3.request(origin, groups, token=token, data={"title": f"G4 {name}"})["body"]["data"]["conversation_id"]
            path = f"{groups}/{group}/background-inspections"
            accepted = g3.request(origin, path, token=token, data={"message": g3.fixture.QUESTION},
                                  headers={"Idempotency-Key": f"g4-{name}"})
            assert accepted["status"] == 202, accepted
            run_id = accepted["body"]["data"]["run_id"]
            return run_id, f"{path}/{run_id}", group, accepted["request_id"]

        run_id, path, group, request_id = start(origin_a, case)
        write(folder / "plan.json", {"first_run_id": run_id, "group": group, "case": case,
                                     "origins": g3.ORIGINS, "path": path, "start_request_id": request_id,
                                     "model": g3.MODEL})
        a = workers.enter_context(start_worker(folder, case, "A", env))
        if case in RACE_CASES:
            workers.enter_context(start_worker(folder, case, "B", env))
            wait_for(lambda: all((folder / label / "race-ready").exists() for label in ("A", "B")), "both race arrivals")
            (folder / "release-race").touch()
            winner = wait_for(lambda: next((label for label in ("A", "B")
                                           if (folder / label / "held-race-tool").exists()), None), "winner in tool")
            loser = "A" if winner == "B" else "B"
            wait_for(lambda: skipped(folder, loser, run_id, "lock_busy"), "loser Trace")
            second, second_path, _, second_request = start(origin_b, "overlap")
            wait_for(lambda: (folder / loser / "race-scan-gated").exists(), "loser completed overlap and reached follow-up scan")
            snapshot(folder, "overlap")
            (folder / winner / "release-race-tool").touch()
            wait_for(lambda: (folder / winner / "race-first-unlocked").exists(), "winner completed and released the process lock")
            before_scan = snapshot(folder, "before-followup-scan")
            (folder / loser / "release-race-scan").touch()
            wait_for(lambda: (folder / loser / "race-scan-returned").exists(), "loser returned from follow-up scan")
            if case == "race-stale":
                wait_for(lambda: skipped(folder, loser, run_id, "no_longer_pending"), "loser safely skipped stale candidate")
            assert before_scan == snapshot(folder, "after-followup-scan")
            second_get = g3.request(origin_a, second_path, token=token)
            assert second_get["status"] == 200
            write(folder / "api-overlap-answer.json", {"origin": origin_a, "request_id": second_get["request_id"],
                  "start_request_id": second_request, "run_id": second, "status": second_get["status"],
                  "answer_id": second_get["body"]["data"]["answer"]["entry_id"]})
        else:
            barrier = "before-final" if case == "final-commit" else "fault"
            wait_for(lambda: (folder / "A" / f"held-{barrier}").exists(), "A reached the fault boundary")
            b = workers.enter_context(start_worker(folder, case, "B", env))
            wait_for(lambda: skipped(folder, "B", run_id, "lock_busy"), "B polling while A holds the lock")
            before = snapshot(folder, "before")
            [job] = before["jobs"]
            assert (a.poll(), b.poll(), job["generation"], before["slots"][0]["released_ns"]) == (None, None, 1, None)
            assert not before["answers"]
            if case == "final-commit":
                (folder / "A" / "release-before-final").touch()
                wait_for(lambda: (folder / "A" / "held-final-committed").exists(), "A returned from final commit")
            kill(folder, "A", a, run_id, read(folder / "A" / f"claim-{run_id}-1.json"))
            if case == "final-commit":
                (folder / "B" / "probe-scan").touch()
                wait_for(lambda: (folder / "B" / "scan-observed").exists(), "B scanned after A exited")
            else:
                wait_for(lambda: (folder / "B" / "held-takeover").exists(), "B automatically claimed generation 2")
                after = snapshot(folder, "after-takeover")
                subprocess.run([sys.executable, "-m", "scripts.storepilot_g4", "inject", str(folder)],
                               env=env, check=True, timeout=15)
                assert after == snapshot(folder, "after-injection")
                (folder / "B" / "release-takeover").touch()

        wait_for(lambda: all(row["finalized"] for row in snapshot(folder)["jobs"]), "all jobs completed")
        if case != "final-commit":
            wait_for(lambda: any(trace["status"] == "completed" for trace in lines(folder / "B" / "traces.jsonl")),
                     "B flushed its completion Trace")
            if case in RACE_CASES:
                wait_for(lambda: any(trace["status"] == "completed" for trace in lines(folder / "A" / "traces.jsonl")),
                         "A flushed its completion Trace")
        answer_get = g3.request(origin_b, path, token=token)
        assert answer_get["status"] == 200
        answer_id = answer_get["body"]["data"]["answer"]["entry_id"]
        write(folder / "api-answer.json", {"origin": origin_b, "request_id": answer_get["request_id"],
                                            "status": answer_get["status"], "answer_id": answer_id})
        snapshot(folder, "final")
        if case == "final-commit":
            with (folder / "browser.log").open("wb") as log:
                subprocess.run(["node", "tests/storepilot_g4_browser.cjs", str(folder)],
                               env={**env, "STOREPILOT_TEST_PASSWORD": password}, stdout=log, stderr=subprocess.STDOUT,
                               check=True, timeout=60)


def execution_key(row):
    return tuple(row[key] for key in EXECUTION)


def scan_traces(worker_traces, worker_pids, run_ids):
    scans = {}
    executor_ids = {}
    for trace, label in worker_traces:
        assert trace["source"] == "background_inspection"
        assert len(trace["steps"]) >= 2, "scan Trace is missing its outcome"
        attempt, outcome = trace["steps"][:2]
        run_id = attempt["runtime_call_id"]
        trace_id = trace["trace_id"]
        assert run_id in run_ids, f"unknown scan run: {run_id}"
        scan_steps = [step for step in trace["steps"] if step["action"] in SCAN_ACTIONS]
        assert scan_steps == [attempt, outcome], f"unexpected scan steps: {trace_id}"
        assert attempt["action"] == "claim_attempt" and attempt["epoch_ns"] <= outcome["epoch_ns"]
        assert all(step["stage"] == "background_inspection" and step["runtime_call_id"] == run_id
                   and step["status"] == "ok" for step in scan_steps), f"unpaired scan: {trace_id}"
        evidence = attempt["evidence"]
        pid, executor = evidence["pid"], evidence["executor_id"]
        assert pid == worker_pids[label] and executor
        assert executor_ids.setdefault(label, executor) == executor
        action = outcome["action"]
        assert action in {"claimed", "lock_busy", "no_longer_pending"}
        if action != "claimed":
            assert trace["status"] == "skipped" and trace["steps"] == scan_steps, f"skip has execution steps: {trace_id}"
        scans.setdefault(run_id, []).append({"trace_id": trace_id, "action": action, "label": label,
                                             "pid": pid, "executor_id": executor, "attempt": attempt, "outcome": outcome})
    assert len(set(executor_ids.values())) == len(executor_ids)
    return scans


def reconcile_scans(scans, claims, finished, events, transport):
    successful = {scan["trace_id"]: scan for scan in scans if scan["action"] == "claimed"}
    assert set(successful) == {claim["trace_id"] for claim in claims}, "Trace and durable claims disagree"
    for claim in claims:
        scan = successful[claim["trace_id"]]
        outcome = scan["outcome"]
        assert scan["attempt"]["evidence"]["observed_generation"] == claim["generation"] - 1
        assert outcome["evidence"] == {"executor_id": claim["executor_id"], "generation": claim["generation"],
                                       "pid": scan["pid"]}
        assert scan["executor_id"] == claim["executor_id"]
        assert scan["attempt"]["epoch_ns"] <= claim["epoch_ns"] <= outcome["epoch_ns"]

    busy = [scan for scan in scans if scan["action"] == "lock_busy"]
    late = [scan for scan in scans if scan["action"] == "no_longer_pending"]
    assert len({scan["executor_id"] for scan in late}) == len(late), "repeated terminal skip by the same executor"
    for scan in busy + late:
        trace_id = scan["trace_id"]
        observed = scan["attempt"]["evidence"]["observed_generation"]
        assert type(observed) is int and 0 <= observed <= finished["generation"]
        assert not any(row.get("trace_id") == trace_id for row in events), f"skip owns durable events: {trace_id}"
        assert not any(row.get("trace_id") == trace_id and row["event"] != "race-claim-rechecked"
                       for row in transport), f"skip owns process work: {trace_id}"
        if scan["action"] == "lock_busy":
            assert scan["outcome"]["epoch_ns"] <= finished["epoch_ns"], f"lock contention outside execution: {trace_id}"
        else:
            assert scan["executor_id"] != finished["executor_id"], f"terminal skip by finisher: {trace_id}"
            assert finished["epoch_ns"] <= scan["outcome"]["epoch_ns"], f"skip preceded terminal commit: {trace_id}"
            assert any(row["executor_id"] == scan["executor_id"] and
                       row["outcome"]["epoch_ns"] <= scan["attempt"]["epoch_ns"] for row in busy), (
                f"terminal skip is missing the preceding contention: {trace_id}")
    return busy, late


def reconcile_race_probe(folder, plan, journal, claims, finished, busy, late, state):
    [contender] = busy
    assert contender["executor_id"] != claims[0]["executor_id"]
    if "model" not in plan:
        # Historical runs predate the scan barrier and recheck journal.
        return
    [gated] = [row for row in journal if row["event"] == "race-scan-gated"]
    [unlocked] = [row for row in journal if row["event"] == "race-first-unlocked"]
    [returned] = [row for row in journal if row["event"] == "race-scan-returned"]
    assert gated["pid"] == returned["pid"] == contender["pid"]
    assert unlocked["trace_id"] == claims[0]["trace_id"] and unlocked["pid"] != contender["pid"]
    assert int(gated["epoch_ns"]) <= finished["epoch_ns"] <= int(unlocked["epoch_ns"]) <= int(returned["epoch_ns"])
    assert read(folder / "sql-before-followup-scan.json") == read(folder / "sql-after-followup-scan.json") == state
    rechecks = [row for row in journal if row["event"] == "race-claim-rechecked"]
    if plan["case"] == "race":
        assert gated["pending"] is None and returned["pending"] == []
        assert not late and not rechecks, "fresh scan must omit the completed run"
        return
    [skip] = late
    [checked] = rechecks
    assert gated["pending"] == returned["pending"] == [[plan["first_run_id"], finished["generation"]]]
    assert checked["trace_id"] == skip["trace_id"] and checked["pid"] == skip["pid"] == contender["pid"]
    assert checked["executor_id"] == skip["executor_id"] and checked["generation"] is None
    assert skip["attempt"]["evidence"]["observed_generation"] == finished["generation"]
    assert int(returned["epoch_ns"]) <= skip["attempt"]["epoch_ns"] <= int(checked["epoch_ns"]) <= skip["outcome"]["epoch_ns"]


def reconcile(folder, case):
    plan = read(folder / "plan.json")
    run_id = plan["first_run_id"]
    assert plan["case"] == case and plan.get("model", g3.MODEL) == g3.MODEL
    state = snapshot(folder)
    assert state == read(folder / "sql-final.json"), "saved and independent SQL snapshots disagree"
    race = case in RACE_CASES
    assert len(state["jobs"]) == (2 if race else 1)
    assert all(not state[key] for key in ("listing_proposals", "listing_receipts", "pause_proposals",
                                         "pause_receipts", "change_proposals", "product_mutations"))
    transport = lines(folder / "transport.jsonl")
    injections = lines(folder / "injection-traces.jsonl")
    worker_traces = []
    worker_pids = {}
    for label in ("A", "B"):
        worker_log = lines(folder / label / "transport.jsonl")
        [online] = [row for row in worker_log if row["event"] == "worker-online"]
        worker_pids[label] = online["pid"]
        assert all(row["pid"] == online["pid"] and row["label"] == label for row in worker_log)
        transport.extend(worker_log)
        worker_traces.extend((trace, label) for trace in lines(folder / label / "traces.jsonl"))
        active = folder / label / "interrupted-trace.json"
        if active.exists():
            worker_traces.append((read(active), label))
    traces = [trace for trace, _ in worker_traces] + injections
    by_trace = {trace["trace_id"]: trace for trace in traces}
    assert len(by_trace) == len(traces), "duplicate Trace ID"
    run_ids = {job["run_id"] for job in state["jobs"]}
    assert run_id in run_ids and len(run_ids) == len(state["jobs"])
    assert all(row["run_id"] in run_ids for table in state.values() for row in table)
    assert all(row.get("run_id") in run_ids for row in transport if "trace_id" in row)
    assert all(trace["source"] == "background_inspection_fence" and trace["status"] == "rejected"
               and len(trace["steps"]) == 1 and trace["steps"][0]["action"] == "write_rejected"
               and trace["steps"][0]["runtime_call_id"] == run_id for trace in injections)
    assert len(injections) == (2 if case in {"kill-before", "kill-after"} else 0)
    scans = scan_traces(worker_traces, worker_pids, run_ids)
    api = read(folder / "api-answer.json")
    report = {}
    for job in state["jobs"]:
        current = job["run_id"]
        assert json.loads(job["spec"])["model"] == g3.MODEL
        rows = {key: [row for row in state[key] if row["run_id"] == current] for key in state}
        for table in ("runs", "slots", "answers", "answer_inserts", "releases", "final_inserts"):
            assert len(rows[table]) == 1, f"{current}: expected one row in {table}"
        run, slot, answer, insert, release, final_insert = (
            rows[key][0] for key in ("runs", "slots", "answers", "answer_inserts", "releases", "final_inserts"))
        events = rows["events"]
        finals = [row for row in events if row["kind"] == "finished"]
        assert len(finals) == 1, f"{current}: expected one durable terminal event"
        [finished] = finals
        claims = [row for row in events if row["kind"] == "claimed"]
        assert [row["seq"] for row in events] == list(range(1, len(events) + 1))
        assert job["finalized"] == 1 and run["status"] == finished["status"] == "completed"
        assert slot["acquired_ns"] < slot["released_ns"] == finished["released_ns"] == release["released_ns"]
        assert answer["id"] == insert["answer_id"] == finished["answer_id"] == job["answer_id"]
        assert final_insert["seq"] == finished["seq"] == events[-1]["seq"]
        expected_attempts = 2 if case in {"kill-before", "kill-after"} else 1
        assert [row["generation"] for row in claims] == list(range(1, expected_attempts + 1)), (
            f"{current}: unexpected persistent claim generations")
        assert job["generation"] == expected_attempts and job["executor_id"] == claims[-1]["executor_id"], (
            f"{current}: final job does not match its last claim")
        if "model" in plan:
            assert rows["claim_writes"] == [
                {"run_id": current, "previous_generation": row["generation"] - 1, "generation": row["generation"],
                 "executor_id": row["executor_id"]} for row in claims
            ], f"{current}: unexpected physical claim updates"
        assert len({row["executor_id"] for row in claims}) == expected_attempts
        assert len({row["trace_id"] for row in claims}) == expected_attempts
        assert execution_key(finished) == execution_key(claims[-1])
        [accepted] = [row for row in events if row["kind"] == "accepted"]
        assert accepted["seq"] == 1 and accepted["acquired_ns"] == slot["acquired_ns"]
        assert accepted["trace_id"] == run["trace_id"]
        execution_events = [row for row in events if row["kind"] != "accepted"]
        claims_by_generation = {row["generation"]: row for row in claims}
        assert [row["generation"] for row in execution_events] == sorted(row["generation"] for row in execution_events)
        assert all(execution_key(row) == execution_key(claims_by_generation[row["generation"]]) and
                   row["seq"] >= claims_by_generation[row["generation"]]["seq"] for row in execution_events)
        run_scans = scans[current]
        busy, late = reconcile_scans(run_scans, claims, finished, state["events"], transport)
        claim_steps = [(scan["trace_id"], step) for scan in run_scans for step in (scan["attempt"], scan["outcome"])]
        release_steps = [(claim["trace_id"], step) for claim in claims for step in by_trace[claim["trace_id"]]["steps"]
                         if step["stage"] == "admission" and step["action"] == "released"]
        if case == "final-commit":
            assert not release_steps and by_trace[finished["trace_id"]]["status"] == "running"
        else:
            [(trace_id, release_step)] = release_steps
            assert trace_id == finished["trace_id"] and by_trace[trace_id]["status"] == "completed"
            assert release_step["runtime_call_id"] == current and release_step["evidence"]["released_ns"] == slot["released_ns"]
        checkpoints = {row["step"]: row["artifact_id"] for row in rows["checkpoints"]}
        artifacts = {row["artifact_id"]: row for row in rows["artifacts"]}
        completed = {row["step"]: row for row in events if row["kind"] == "step_completed"}
        assert len(checkpoints) == len(artifacts) == len(completed) == 10
        assert set(checkpoints.values()) == set(artifacts)
        assert checkpoints == {step: row["artifact_id"] for step, row in completed.items()}
        receipts = {}
        for product in json.loads(answer["details"])["products"]:
            assert product["review_complete"]
            for tool, receipt in product["receipts"].items():
                name = f"{product['sku']}/{tool}"
                assert name not in receipts
                receipts[name] = receipt
                committed = completed[name]
                artifact = artifacts[receipt["artifact_id"]]
                saved = json.loads(artifact["result"])
                assert artifact["generation"] == committed["generation"]
                assert receipt["artifact_id"] == committed["artifact_id"]
                assert receipt["tool_call_id"] == committed["runtime_call_id"] == saved["tool_call_id"]
                assert receipt["result"] == saved["result"]
        journal = [row for row in transport if row.get("run_id") == current]
        if case != "race-stale" or current != run_id:
            assert not any(row["event"] == "race-claim-rechecked" for row in journal)
        entered = [row for row in journal if row["event"] == "execute_entered"]
        assert [execution_key(row) for row in entered] == [execution_key(row) for row in claims]
        [committed] = [row for row in journal if row["event"] == "final_committed"]
        assert execution_key(committed) == execution_key(finished)
        assert committed["status"] == finished["status"] and committed["released_ns"] == slot["released_ns"]
        actual = [row for row in journal if row["event"] == "bottom-tool-start"]
        counts = {step: sum(row["step"] == step for row in actual) for step in sorted({row["step"] for row in actual})}
        assert len(counts) == 9 and set(counts) == set(receipts)
        assert {step: count for step, count in counts.items() if count != 1} == (
            {FACT: 2} if case == "kill-before" else {}), f"{current}: unexpected bottom-tool reads"
        [model] = [row for row in journal if row["event"] == "fixed-model-review"]
        assert execution_key(model) == execution_key(claims[-1])
        dispatched = [row for row in events if row["kind"] == "step_started" and row.get("tool")]
        dispatch = {row["runtime_call_id"]: row for row in dispatched}
        returns = [row for row in journal if row["event"] == "bottom-tool-return"]
        returned = {row["runtime_call_id"]: row for row in returns}
        assert len({row["runtime_call_id"] for row in actual}) == len(actual) == len(dispatch) == len(dispatched)
        assert len(returns) == len(returned) == len(actual)
        for call in actual:
            call_id = call["runtime_call_id"]
            started, result = dispatch[call_id], returned[call_id]
            assert execution_key(call) == execution_key(started) == execution_key(result)
            assert call["step"] == started["step"] == result["step"]
            assert started["epoch_ns"] <= int(call["epoch_ns"]) <= int(result["epoch_ns"])
            tool_steps = [step for step in by_trace[call["trace_id"]]["steps"]
                          if step["stage"] in {"tool_call", "tool_result"} and step.get("runtime_call_id") == call_id]
            interrupted = case == "kill-before" and call["generation"] == 1 and call["step"] == FACT
            assert [step["stage"] for step in tool_steps] == (["tool_call"] if interrupted else ["tool_call", "tool_result"])
            assert all(step["action"] == started["tool"] for step in tool_steps)
            if not interrupted:
                evidence = tool_steps[-1]["evidence"]
                assert evidence["run_id"] == current and evidence["step"] == call["step"]
                assert execution_key(evidence) == execution_key(call)
        assert all(dispatch[receipt["tool_call_id"]]["step"] == name for name, receipt in receipts.items())
        reused = {row["step"]: row["artifact_id"] for row in events if row["kind"] == "reused"}
        rejections = [(trace["trace_id"], step) for trace in injections for step in trace["steps"]
                      if step.get("runtime_call_id") == current and step["stage"] == "background_inspection"
                      and step["action"] == "write_rejected"]
        claim_steps.extend(rejections)
        if current == run_id:
            assert api["answer_id"] == f"entry:{answer['id']}"
            if race:
                reconcile_race_probe(folder, plan, journal, claims, finished, busy, late, state)
                first_executor = model["executor_id"]
                [second_job] = [row for row in state["jobs"] if row["run_id"] != run_id]
                assert second_job["executor_id"] != first_executor
                assert all(row["executor_id"] == first_executor for row in actual)
                overlap = read(folder / "sql-overlap.json")["jobs"]
                assert {row["run_id"]: row["finalized"] for row in overlap} == {run_id: 0, second_job["run_id"]: 1}
                second_accepted = next(row for row in state["events"] if row["run_id"] != run_id and row["kind"] == "accepted")
                assert int(min(actual, key=lambda row: int(row["epoch_ns"]))["epoch_ns"]) < second_accepted["acquired_ns"]
            else:
                [exit_row] = [row for row in journal if row["event"] == "process_exit_observed"]
                [kill_row] = [row for row in journal if row["event"] == "kill_requested"]
                [original_attempt] = [step for trace_id, step in claim_steps
                                      if trace_id == claims[0]["trace_id"] and step["action"] == "claim_attempt"]
                assert exit_row["pid"] == kill_row["pid"] == original_attempt["evidence"]["pid"]
                assert exit_row["returncode"] != 0
                assert any(step["action"] == "lock_busy" and step["epoch_ns"] < int(kill_row["epoch_ns"])
                           for _, step in claim_steps)
                if case == "final-commit":
                    [scan] = [row for row in journal if row["event"] == "scan-observed"]
                    assert all(candidate[0] != run_id for candidate in scan["pending"])
                    assert int(scan["epoch_ns"]) >= int(exit_row["epoch_ns"])
                    assert finished["epoch_ns"] < int(kill_row["epoch_ns"])
                    assert read(folder / "browser.json")["after_reload"] == api["answer_id"]
                else:
                    before = read(folder / "sql-before.json")
                    after = read(folder / "sql-after-takeover.json")
                    assert after == read(folder / "sql-after-injection.json")
                    assert before["slots"] == after["slots"] == [{**slot, "released_ns": None}]
                    for table in ("checkpoints", "artifacts", "answers", "answer_inserts", "releases", "final_inserts"):
                        assert before[table] == after[table]
                    assert not after["answers"] and len(after["events"]) == len(before["events"]) + 1
                    assert int(kill_row["epoch_ns"]) < claims[1]["epoch_ns"]
                    assert {row["label"] for row in actual if row["generation"] == 1} == {"A"}
                    assert {row["label"] for row in actual if row["generation"] == 2} == {"B"}
                    expected_reuse = {SALES, CURRENT} | ({FACT} if case == "kill-after" else set())
                    assert reused == {row["step"]: row["artifact_id"] for row in before["checkpoints"]}
                    assert set(reused) == expected_reuse and all(checkpoints[step] == artifact for step, artifact in reused.items())
                    fault_receipt = read(folder / "A" / "receipt.json")
                    [fault_call] = [row for row in actual if row["step"] == FACT and row["generation"] == 1]
                    assert fault_receipt["runtime_call_id"] == fault_call["runtime_call_id"]
                    assert len(rejections) == 2
                    assert {step["detail"]: step["error_code"] for _, step in rejections} == {
                        "checkpoint": "generation_mismatch", "finish": "generation_mismatch"}
                    assert all(execution_key(step["evidence"]) == execution_key(claims[0]) for _, step in rejections)
        elif "model" in plan:
            api_second = read(folder / "api-overlap-answer.json")
            assert api_second["run_id"] == current and api_second["answer_id"] == f"entry:{answer['id']}"
            assert api_second["start_request_id"] == accepted["request_id"] and api_second["status"] == 200

        timeline = [{**row, "source": "process"} for row in journal]
        timeline.extend({**row, "epoch_ns": str(row.get("epoch_ns", row.get("acquired_ns"))), "source": "sqlite"}
                        for row in events)
        timeline.extend({**step, "epoch_ns": str(step["epoch_ns"]), "trace_id": trace_id, "source": "trace"}
                        for trace_id, step in claim_steps)
        report[current] = {"case": case, "run_id": current, "claims": claims, "reused": reused,
                           "model": g3.MODEL,
                           "scan_traces": sorted(run_scans, key=lambda scan: scan["attempt"]["epoch_ns"]),
                           "tool_counts": counts, "rerun_steps": [step for step, count in counts.items() if count > 1],
                           "checkpoints": checkpoints, "slot": slot, "answer_id": answer["id"],
                           "audit_counts": {"claim_updates": len(rows["claim_writes"]) if "claim_writes" in rows else None,
                                            "assistant_inserts": len(rows["answer_inserts"]),
                                            "final_events": len(rows["final_inserts"]), "slot_releases": len(rows["releases"])},
                           "timeline": sorted(timeline, key=lambda row: int(row["epoch_ns"]))}
        if current == run_id:
            report[current]["api"] = api
        elif "model" in plan:
            report[current]["api"] = api_second
    write(folder / "reconciliation.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("run", "worker", "inject", "audit", "snapshot"))
    parser.add_argument("folder", type=Path)
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--label", choices=("A", "B"))
    args = parser.parse_args()
    root = args.folder.resolve()
    if args.stage == "worker":
        return fixture_worker(root, args.case, args.label)
    if args.stage == "inject":
        return inject(root)
    if args.stage == "snapshot":
        print(json.dumps(snapshot(root), ensure_ascii=False, indent=2))
        return
    cases = (args.case,) if args.case else CASES
    if args.stage == "audit" and args.case is None and not (root / "race-stale").exists():
        if "model" not in read(root / "race" / "plan.json"):
            cases = tuple(case for case in CASES if case != "race-stale")
    if args.stage == "run":
        root.mkdir(parents=True, exist_ok=False)
        password = token_urlsafe(24)
        seed_db = root / "seed.sqlite3"
        configure(seed_db)
        seed(seed_db, password)
    report = {}
    for case in cases:
        if args.stage == "run":
            run_case(root, case, seed_db, password)
        report.update(reconcile(root / case, case))
        print(f"{case}: passed", flush=True)
    write(root / "g4-reconciliation.json", report)


if __name__ == "__main__":
    main()
