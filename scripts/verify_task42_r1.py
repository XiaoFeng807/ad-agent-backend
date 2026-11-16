"""Replay T3 Listing questions with a per-reader phase audit."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from functools import wraps
from hashlib import sha256
import json
from pathlib import Path
from secrets import token_urlsafe
import sys
from threading import Barrier
from unittest.mock import patch

from scripts import verify_task42_t3 as t3
from scripts.accept_task42_t1 import configure, read, write


ROOT = Path("logs/task42-r1")
FROZEN = Path("logs/task42-t3/frozen.json")
PHASES = ("business_read", "batch_revalidation", "worker_handoff", "proposal_transaction",
          "approve", "action_get")
PREVIEW = PHASES[:4]


def race_case(client, model, listings, database, folder, password, case, boundary, change):
    from backend import observability
    from backend.agent.team_runtime import DelegationRun

    path = folder / "sandbox.sqlite3"
    captures = {"reads": [], "receipts": [], "batches": [], "sdk": []}
    arrived, mutated = Barrier(2, timeout=20), Barrier(2, timeout=20)
    at_barrier = {}

    def pause(**evidence):
        at_barrier.update(evidence)
        arrived.wait()
        mutated.wait()

    original_check = DelegationRun._block_unavailable_inputs

    def handoff(run, task, *args):
        if task.task_id == "inspect":
            pause(input_receipts=run._board.input_receipts(task))
        return original_check(run, task, *args)

    original_save = listings.create_group_proposal

    def persist(conn, run, entry_id, evidence):
        if evidence.get("proposal"):
            at_barrier["proposal_attempt"] = evidence["proposal"]
        try:
            return original_save(conn, run, entry_id, evidence)
        except (listings.ListingConflict, listings.ListingNotFound) as exc:
            at_barrier["persistence_rejection"] = {"class": type(exc).__name__, "reason": str(exc)}
            raise

    completions = model.chat.completions
    original_create = completions.create

    def draft(**payload):
        if payload["messages"][0]["content"].startswith("Produce only a JSON object"):
            pause(draft_input=json.loads(payload["messages"][-1]["content"]))
        return original_create(**payload)

    with ExitStack() as stack:
        stack.enter_context(patch.object(database.settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(path)))
        for attr, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                               ("FUNC_LOG_FILE", "functions.jsonl")):
            stack.enter_context(patch.object(observability, attr, folder / filename))
        t3.seeded(database, listings, password, case, None)
        headers = t3.t1.login(client, t3.t2.OWNER, password)
        model.mode = "delegate"
        model.requests.clear()
        if boundary == "handoff":
            stack.enter_context(patch.object(DelegationRun, "_block_unavailable_inputs", handoff))
        else:
            stack.enter_context(patch.object(completions, "create", draft))
        stack.enter_context(patch.object(listings, "create_group_proposal", persist))
        t3.t1.capture_runtime(stack, captures, listings, model, None)
        with ThreadPoolExecutor(max_workers=1) as executor:
            stream = executor.submit(t3.t1.http_round, client, headers, folder.name, case["message"])
            arrived.wait()
            before = t3.t1.snapshot(path)
            params = tuple(t3.TARGET.values())
            where = " WHERE shop_id=? AND marketplace_id=? AND sku=?"
            with closing(database.get_db()) as conn, conn:
                if change == "listing-version":
                    sql = "UPDATE listing_test_products SET version=version+1" + where
                elif change == "facts":
                    sql = "UPDATE listing_test_facts SET facts=replace(facts, '500 mL', '750 mL')" + where
                else:
                    sql = ("UPDATE listing_test_shops SET user_id=(SELECT id FROM users WHERE username='lisi')"
                           " WHERE shop_id=? AND marketplace_id=?")
                    params = params[:2]
                if conn.execute(sql, params).rowcount != 1:
                    raise AssertionError("Mutation did not target exactly one row")
            after_mutation = t3.t1.snapshot(path)
            mutated.wait()
            response = stream.result(timeout=20)
        history = t3.t1.edge_history(client, headers, response["base"])
        after = t3.t1.snapshot(path)

    traces = t3.t1.read_traces(folder)
    rows = captures.pop("sdk")
    contexts, issues = t3.handoff(captures, rows)
    stale_sources = {"listing-version": {t3.t2.LISTING}, "facts": {t3.t2.FACTS},
                     "shop-owner": set(t3.t1.READ_KEYS)}
    upstream_ids = {row["artifact_id"] for row in captures["receipts"]
                    if row.get("task_id") == "evidence" and row["source"] in t3.t1.READ_KEYS}
    blocked = [step for trace in traces for step in trace["steps"] if step["stage"] == "dependency_input"
               and step["action"] == "blocked" and step.get("error_code") == "listing_evidence_changed"]
    terminal = [event["type"] for event in response["events"] if event["type"] in ("done", "error", "stopped")]
    checks = {"mutation_committed": before != after_mutation, "no_additional_listing_writes": after_mutation == after,
              "zero_proposals_and_receipts": not after["listing_change_proposals"] and not after["listing_change_receipts"],
              "zero_history_actions": history["history_status"] == 200 and all(
                  not entry.get("listing_actions") for entry in history["history"]["entries"]),
              "three_upstream_artifacts": len(upstream_ids) == 3, "handoff_integrity": not issues}
    if boundary == "handoff":
        stale_ids = {row["artifact_id"] for row in at_barrier["input_receipts"] if row["source"] in stale_sources[change]}
        blocked_ids = {step["evidence"]["unavailable_artifact_id"] for step in blocked}
        checks.update(inspect_never_received=not any(row.get("task_id") == "inspect" for row in rows),
                      stale_artifact_blocked=bool(stale_ids & blocked_ids),
                      draft_never_called=not any(row.get("step") == "listing_draft" for row in rows),
                      terminal_gap=terminal == ["done"])
    else:
        checks.update(inspect_received_exact_artifacts=set(contexts.get("inspect", {}).get("evidence_ids", [])) == upstream_ids,
                      commit_rechecked_snapshot=set(at_barrier.get("proposal_attempt", {}).get("artifact_refs", {}).values())
                      == upstream_ids and at_barrier.get("persistence_rejection", {}).get("class")
                      == ("ListingNotFound" if change == "shop-owner" else "ListingConflict"),
                      reply_rolled_back=terminal == ["error"] and all(
                          entry["kind"] != "assistant" for entry in history["history"]["entries"]))
    report = {"passed": all(checks.values()), "checks": checks, "trace_id": next(
        (event.get("trace_id") for event in reversed(response["events"]) if event.get("trace_id")), None)}
    write(folder / "audit.json", {"summary": report, "case": case, "barrier": at_barrier, "mutation": [sql, params],
                                 "response": response, "history": history, "runtime": captures, "sdk": rows,
                                 "handoff": contexts, "handoff_issues": issues, "blocked": blocked,
                                 "sql_before": before, "sql_after_mutation": after_mutation, "sql_after": after})
    return report


def run_races(output):
    from fastapi.testclient import TestClient

    configure(output / "bootstrap.sqlite3")
    from backend.agent.llm_provider import ProviderFactory
    from backend.database import database, listing_operations as listings
    from backend.memory import conversation_memory
    from tests.test_listing_workgroup import ListingModel

    model = ListingModel()
    password = token_urlsafe(24)
    case = next(case for case in read(FROZEN)["spot"] if case["id"] == "browser-complete")
    results = {}
    with (patch.object(ProviderFactory, "create", return_value=model),
          patch.object(conversation_memory, "store_conversation")):
        from fast_server import app

        with TestClient(app) as client:
            for boundary in ("handoff", "commit"):
                for change in ("listing-version", "facts", "shop-owner"):
                    folder = output / f"{boundary}-{change}"
                    folder.mkdir()
                    results[folder.name] = race_case(client, model, listings, database, folder, password,
                                                     case, boundary, change)
    passed = all(row["passed"] for row in results.values())
    write(output / "report.json", {"passed": passed, "cases": results})
    return int(not passed)


def measure_reads(original, name, audit, observations):
    @wraps(original)
    def observed(*args, **kwargs):
        request = t3.REQUEST.get() or {}
        phase = request.get("phase", "workgroup")
        if phase == "workgroup":
            phase = "business_read"
            caller = sys._getframe(1)
            while caller:
                if caller.f_code.co_name in ("_require_snapshot", "basis", "_block_unavailable_inputs"):
                    phase = {"_require_snapshot": "proposal_transaction", "basis": "batch_revalidation",
                             "_block_unavailable_inputs": "worker_handoff"}[caller.f_code.co_name]
                    break
                caller = caller.f_back
        observations.append({"index": len(observations) + 1, "read": name, "phase": phase,
                             "method": request.get("method"), "path": request.get("path"),
                             "arguments": list(args[1:]), "tool": audit.tool_link.get()})
        return original(*args, **kwargs)
    return observed


def run(stage, root, name):
    frozen = read(FROZEN)
    if frozen != t3.freeze():
        raise RuntimeError("T3 frozen questions or scoring rules changed")
    if stage == "live":
        for path in (root / "after/reading-counts.json", root / "fixed/summary.json", root / "races/report.json"):
            if not read(path)["passed"]:
                raise RuntimeError(f"Fixed replay gate failed: {path}")
    output = root / name
    output.mkdir(parents=True, exist_ok=False)
    if stage == "races":
        return run_races(output)
    (output / "frozen.json").write_bytes(FROZEN.read_bytes())
    natural, _, _, complete = frozen["spot"]
    gap = next(case for case in frozen["paired"] if case["id"] == "conditional-gap")
    schedule = [("cases", case, "production") for case in (natural, complete, gap)]
    result = t3.execute("live" if stage == "live" else "fixed", output, frozen, output,
                        [path for path in root.rglob("glm-sdk-ledger.sqlite3") if stage == "live"],
                        schedule=schedule, reader_probe=measure_reads)
    counts = {}
    for _, case, _ in schedule:
        path = output / "cases" / case["id"] / "audit.json"
        if not path.exists():
            continue
        audit = read(path)
        tally = Counter(item["phase"] for item in audit["reader_invocations"])
        summary = audit["summary"]
        counts[case["id"]] = {**{phase: tally[phase] for phase in PHASES},
                              "preview": sum(tally[phase] for phase in PREVIEW), "all": tally.total(),
                              "trace_id": summary["trace_id"], "sdk_attempts": summary["attempts"],
                              "tokens": summary["tokens"], "seconds": summary["seconds"],
                              "message_sha256": sha256(case["message"].encode()).hexdigest()}
    preview = counts.get(complete["id"], {}).get("preview")
    passed = result == 0 and preview is not None and (preview == 12 if stage == "before" else preview < 12)
    write(output / "reading-counts.json", {"passed": passed, "cases": counts})
    return int(not passed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("before", "after", "races", "live"))
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--run-name")
    args = parser.parse_args()
    return run(args.stage, args.output_dir.resolve(), args.run_name or args.stage)


if __name__ == "__main__":
    raise SystemExit(main())
