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


CASES = ("race", "kill-before", "kill-after", "final-commit")
FACT = g3.FACT_STEP
SALES = f"{g3.base.TOTE}/{g3.sql.SALES}"
CURRENT = f"{g3.base.TOTE}/{g3.sql.CURRENT}"
AUDIT = {"releases": "g4_releases", "final_inserts": "g4_final_inserts"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as log:
        return [json.loads(line) for line in log if line.endswith("\n")]


def snapshot(folder, name=None):
    result = g3.snapshot(folder / "run.sqlite3", extra_tables=AUDIT)
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
    plan = read(folder / "plan.json")
    original_lock, original_execute = worker.executor_lock, worker.execute
    original_pending, original_checkpoint, original_finish = storage.pending, storage.checkpoint, storage.finish

    def record(event, **fields):
        journal.log(event, label=label, pid=os.getpid(),
                    **{key: active[key] for key in ("run_id", "trace_id", "executor_id", "generation")}, **fields)

    def save_trace():
        write(here / "interrupted-trace.json", observability.get_trace(active["trace_id"]))

    @contextmanager
    def lock(directory, run_id):
        nonlocal race_ready
        if case == "race" and not race_ready:
            assert run_id == plan["first_run_id"]
            race_ready = True
            journal.mark("race-ready", pid=os.getpid(), run_id=run_id, label=label)
            g3.wait_for(lambda: (folder / "release-race").exists(), "shared A/B barrier")
        with original_lock(directory, run_id) as acquired:
            yield acquired

    def pending():
        probe = (here / "probe-scan").exists() and not journal.signal("scan-observed")
        rows = original_pending()
        if probe:
            journal.mark("scan-observed", label=label, pid=os.getpid(), run_id=plan["first_run_id"], pending=rows)
        return rows

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
            if case == "race" and active["run_id"] == plan["first_run_id"] and step == SALES:
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
            (storage, "pending", pending), (storage, "checkpoint", checkpoint), (storage, "finish", finish),
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


def busy(folder, label, run_id):
    return any(trace["status"] == "skipped" and any(
        step["action"] == "lock_busy" and step.get("runtime_call_id") == run_id for step in trace["steps"])
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
                                     "origins": g3.ORIGINS, "path": path, "start_request_id": request_id})
        a = workers.enter_context(start_worker(folder, case, "A", env))
        if case == "race":
            b = workers.enter_context(start_worker(folder, case, "B", env))
            wait_for(lambda: all((folder / label / "race-ready").exists() for label in ("A", "B")), "both race arrivals")
            (folder / "release-race").touch()
            winner = wait_for(lambda: next((label for label in ("A", "B")
                                           if (folder / label / "held-race-tool").exists()), None), "winner in tool")
            loser = "A" if winner == "B" else "B"
            wait_for(lambda: busy(folder, loser, run_id), "loser Trace")
            second, *_ = start(origin_b, "overlap")
            wait_for(lambda: any(row["run_id"] == second and row["finalized"]
                                 for row in snapshot(folder)["jobs"]), "other executor completed overlap")
            snapshot(folder, "overlap")
            (folder / winner / "release-race-tool").touch()
        else:
            barrier = "before-final" if case == "final-commit" else "fault"
            wait_for(lambda: (folder / "A" / f"held-{barrier}").exists(), "A reached the fault boundary")
            b = workers.enter_context(start_worker(folder, case, "B", env))
            wait_for(lambda: busy(folder, "B", run_id), "B polling while A holds the lock")
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
            if case == "race":
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


def reconcile(folder, case):
    plan = read(folder / "plan.json")
    run_id = plan["first_run_id"]
    state = snapshot(folder)
    assert state == read(folder / "sql-final.json")
    assert len(state["jobs"]) == (2 if case == "race" else 1)
    assert all(not state[key] for key in ("listing_proposals", "listing_receipts", "pause_proposals",
                                         "pause_receipts", "change_proposals", "product_mutations"))
    transport = lines(folder / "transport.jsonl")
    traces = lines(folder / "injection-traces.jsonl")
    for label in ("A", "B"):
        transport.extend(lines(folder / label / "transport.jsonl"))
        traces.extend(lines(folder / label / "traces.jsonl"))
        active = folder / label / "interrupted-trace.json"
        if active.exists():
            traces.append(read(active))
    by_trace = {trace["trace_id"]: trace for trace in traces}
    api = read(folder / "api-answer.json")
    report = {}
    for job in state["jobs"]:
        current = job["run_id"]
        rows = {key: [row for row in state[key] if row["run_id"] == current] for key in state}
        [run], [slot], [answer], [insert], [release], [final_insert] = (
            rows[key] for key in ("runs", "slots", "answers", "answer_inserts", "releases", "final_inserts"))
        events = rows["events"]
        [finished] = [row for row in events if row["kind"] == "finished"]
        claims = [row for row in events if row["kind"] == "claimed"]
        assert [row["seq"] for row in events] == list(range(1, len(events) + 1))
        assert job["finalized"] == 1 and run["status"] == finished["status"] == "completed"
        assert slot["acquired_ns"] < slot["released_ns"] == finished["released_ns"] == release["released_ns"]
        assert answer["id"] == insert["answer_id"] == finished["answer_id"] == job["answer_id"]
        assert final_insert["seq"] == finished["seq"] == events[-1]["seq"]
        expected_attempts = 2 if case in {"kill-before", "kill-after"} else 1
        assert [row["generation"] for row in claims] == list(range(1, expected_attempts + 1))
        assert job["executor_id"] == claims[-1]["executor_id"]
        assert len({row["executor_id"] for row in claims}) == expected_attempts
        checkpoints = {row["step"]: row["artifact_id"] for row in rows["checkpoints"]}
        assert len(checkpoints) == 10 and set(checkpoints.values()) == {row["artifact_id"] for row in rows["artifacts"]}
        for product in json.loads(answer["details"])["products"]:
            assert product["review_complete"]
            for tool, receipt in product["receipts"].items():
                assert receipt["artifact_id"] == checkpoints[f"{product['sku']}/{tool}"]
        journal = [row for row in transport if row.get("run_id") == current]
        actual = [row for row in journal if row["event"] == "bottom-tool-start"]
        counts = {step: sum(row["step"] == step for row in actual) for step in sorted({row["step"] for row in actual})}
        assert len(counts) == 9
        assert {step: count for step, count in counts.items() if count != 1} == ({FACT: 2} if case == "kill-before" else {})
        [model] = [row for row in journal if row["event"] == "fixed-model-review"]
        assert model["trace_id"] == claims[-1]["trace_id"]
        dispatch = {row["runtime_call_id"]: row for row in events if row["kind"] == "step_started" and row.get("tool")}
        assert len({row["runtime_call_id"] for row in actual}) == len(actual) == len(dispatch)
        for call in actual:
            committed = dispatch[call["runtime_call_id"]]
            assert all(call[key] == committed[key] for key in ("generation", "trace_id", "step", "executor_id"))
            assert any(step["stage"] == "tool_call" and step.get("runtime_call_id") == call["runtime_call_id"]
                       for step in by_trace[call["trace_id"]]["steps"])
        reused = {row["step"]: row["artifact_id"] for row in events if row["kind"] == "reused"}
        claim_steps = [(trace["trace_id"], step) for trace in traces for step in trace["steps"]
                       if step.get("runtime_call_id") == current and step["stage"] == "background_inspection"
                       and step["action"] in {"claim_attempt", "lock_busy", "claimed", "write_rejected"}]
        for row in claims:
            assert any(trace_id == row["trace_id"] and step["action"] == "claimed" and
                       step["evidence"]["executor_id"] == row["executor_id"] for trace_id, step in claim_steps)
        if current == run_id:
            assert api["answer_id"] == f"entry:{answer['id']}"
            if case == "race":
                assert sorted(step["action"] for _, step in claim_steps) == [
                    "claim_attempt", "claim_attempt", "claimed", "lock_busy"]
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
                    rejections = {step["detail"]: step["error_code"] for _, step in claim_steps
                                  if step["action"] == "write_rejected"}
                    assert rejections == {"checkpoint": "generation_mismatch", "finish": "generation_mismatch"}

        timeline = [{**row, "source": "process"} for row in journal]
        timeline.extend({**row, "epoch_ns": str(row.get("epoch_ns", row.get("acquired_ns"))), "source": "sqlite"}
                        for row in events)
        timeline.extend({**step, "epoch_ns": str(step["epoch_ns"]), "trace_id": trace_id, "source": "trace"}
                        for trace_id, step in claim_steps)
        report[current] = {"case": case, "run_id": current, "claims": claims, "reused": reused,
                           "tool_counts": counts, "rerun_steps": [step for step, count in counts.items() if count > 1],
                           "checkpoints": checkpoints, "slot": slot, "answer_id": answer["id"],
                           "audit_counts": {"assistant_inserts": len(rows["answer_inserts"]),
                                            "final_events": len(rows["final_inserts"]), "slot_releases": len(rows["releases"])},
                           "timeline": sorted(timeline, key=lambda row: int(row["epoch_ns"]))}
        if current == run_id:
            report[current]["api"] = api
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
