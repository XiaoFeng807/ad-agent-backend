"""Score the T6 archive offline against the frozen SQL and real SDK inputs."""

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from pydantic import ValidationError

from backend.agent.team_contracts import TeamTask
from scripts.audit_task27_t3 import sdk_calls
from scripts.audit_task27_t5 import evaluate, object_or_empty
from scripts.audit_task27_t5a import linked_detail, trace_steps
from scripts.run_real_diagnosis import require
from scripts.run_task27_t5 import CASE_CAP, DETAIL, ENDPOINT, METRICS, MODEL, RULE, read
from scripts.run_task27_t6 import LEDGER, TARGETS, T6Audit, frozen_fixture
from scripts.task27_t3_fixture import write

RECEIPT_FIELDS = ("artifact_id", "source", "status", "tool_call_id", "arguments", "result")


def submitted_task(task, dispatch, score, sample, normalize_task):
    if not dispatch:
        return None
    trace_calls = {step.get("tool_call_id") for step in trace_steps(sample, "tool_call", "delegate_tasks")
                   if step.get("runtime_call_id") == dispatch}
    for call in score["task_sheets"]:
        if call["id"] not in trace_calls:
            continue
        for item in call["task_sheet"].get("tasks", []):
            try:
                payload = normalize_task(item, call) if normalize_task else item
                normalized = TeamTask.model_validate(payload).model_dump(mode="json")
            except ValidationError:
                continue
            if normalized == task:
                return call
    return None


def worker_transfers(sample, score, sdk, *, normalize_task=None):
    accepted = {(step.get("parent_call_id"), step.get("runtime_call_id"), step["evidence"]["artifact_id"])
                for step in trace_steps(sample, "receipt", "accepted")}
    evidence = {(row["task_id"], row["artifact_id"]): row for row in sample["admission"]
                if row.get("worker_id") and row["accepted"] and row["status"] == "succeeded"
                and (row["worker_id"], row["tool_call_id"], row["artifact_id"]) in accepted}
    workers = {row["worker_id"]: row for row in sample["workers"]}
    dispatches = {step["runtime_call_id"]: step.get("parent_call_id")
                  for step in trace_steps(sample, "worker", "created")}
    plans = {key: submitted_task(row["task"], dispatches.get(key), score, sample, normalize_task)
             for key, row in workers.items()}
    valid_ids = {row["artifact_id"] for w in score["window_evidence"] for row in w["receipts"]}
    valid_rules = {row["artifact_id"] for row in score["rule_receipts"]}
    for key, worker in workers.items():
        task = worker["task"]
        first = next((row for row in sdk if row.get("worker_id") == key), None)
        context = next((object_or_empty(msg.get("content")) for msg in (first or {}).get("body", {}).get("messages", [])
                        if msg["role"] == "user"), {})
        planned = bool(plans[key] and context.get("task") == task and first.get("task_id") == task["task_id"])
        exact = {(ref["task_id"], ref["artifact_id"]) for ref in task["artifact_refs"]}
        matched = []
        for dependency, items in context.get("inputs", {}).items():
            for item in items:
                artifact = item.get("artifact_id")
                row = evidence.get((dependency, artifact))
                if not (planned and dependency in task["dependencies"]
                        and (dependency in task["input_refs"] or (dependency, artifact) in exact)
                        and row and row["worker_id"] != key and plans.get(row["worker_id"])
                        and workers[row["worker_id"]]["end_ns"] <= first["start_ns"]
                        and row["ns"] <= first["start_ns"]
                        and all(item.get(field) == row.get(field) for field in RECEIPT_FIELDS)):
                    continue
                matched.append({**{field: row[field] for field in RECEIPT_FIELDS if field != "result"},
                                "producer_worker_id": row["worker_id"], "producer_task_id": dependency,
                                "producer_task_sheet_call_id": plans[row["worker_id"]]["id"], "accepted_ns": row["ns"],
                                "reference": "artifact_refs" if (dependency, artifact) in exact else "input_refs"})
        metrics = {tuple(sorted(row["arguments"].items())) for row in matched
                   if row["source"] == METRICS and row["artifact_id"] in valid_ids}
        rules = [row["artifact_id"] for row in matched if row["source"] == RULE and row["artifact_id"] in valid_rules]
        yield {"task": task, "worker_id": key, "task_sheet_call_id": (plans[key] or {}).get("id"),
               "first_sdk_attempt": (first or {}).get("attempt"), "first_request_ns": (first or {}).get("start_ns"),
               "first_input": context, "matching_artifacts": matched, "verified_windows": len(metrics),
               "verified_rule_artifact_ids": rules, "complete_evidence": len(metrics) == 4 and bool(rules),
               "error_code": worker.get("error_code")}


def score_round(sample, case, sql, frozen, sdk, *, normalize_task=None):
    score = evaluate(sample, case, sql, frozen, sdk)
    chain = linked_detail(sample, score, sdk)
    transfers = list(worker_transfers(sample, score, sdk, normalize_task=normalize_task))
    business = bool(score["business_pass"] and score["admission_pass"] and chain)
    detail_worker = chain["call"].get("worker_id") if chain else None
    detail_attempt = chain["sdk_attempt"] if chain else None
    downstream = next((row for row in transfers if row["worker_id"] == detail_worker), None)
    handoff = bool(business and downstream and downstream["complete_evidence"]
                   and downstream["first_sdk_attempt"] <= detail_attempt
                   and chain["receipt"].get("worker_id") == detail_worker
                   and score["detail_reads"][0].get("parent_call_id") == detail_worker)
    lifecycle = bool(score["lifecycle"]["pass"] and sample.get("stream_posts") == 1 and not score["failure"])
    history = sample.get("history") or {}
    terminals = [event for event in sample["sse"] if event["type"] in {"done", "error"}]
    terminal = terminals[0] if len(terminals) == 1 and terminals[0] == sample["sse"][-1] else {}
    committed = [{"kind": row["kind"], "content": row["content"], "event": object_or_empty(row["details"])}
                 for row in sample.get("committed_entries", [])]
    wire = [{"kind": row["kind"], "content": row["content"], "event": row.get("event", {})}
            for row in history.get("entries", [])]
    runs = history.get("runs", [])
    history_matches = bool(terminal and len(runs) == 1 and committed == wire
                           and sample.get("committed_runs") == runs
                           and terminal.get("run_id") == runs[0]["run_id"]
                           and terminal.get("trace_id") == runs[0]["trace_id"]
                           and runs[0]["status"] == {"done": "completed", "error": "error"}[terminal["type"]])
    return {**score, "detail_chain": chain, "business_verified": business, "lifecycle_verified": lifecycle,
            "single_sse_terminal": terminal.get("type"), "history_matches_terminal": history_matches,
            "actual_detail_executor": chain["call"]["role"] if chain else None,
            "actual_detail_worker": detail_worker, "worker_transfers": transfers,
            "actual_detail_queries": [{key: row.get(key) for key in
                                        ("role", "parent_call_id", "arguments", "runtime_call_id", "start_ns")}
                                      for row in score["detail_reads"]],
            "worker_handoff_verified": handoff, "worker_handoff_required": case["id"] != "swap-1",
            "no_http_response": [{k: row.get(k) for k in
                                  ("attempt", "trace_id", "step", "task_id", "worker_id", "model_call_id", "response")}
                                 for row in sdk if row["response"].get("failure_reason") == "no_http_response"]}


def first_deviation(score, sample, case, sdk):
    if (score["business_verified"] and score["lifecycle_verified"]
            and (not score["worker_handoff_required"] or score["worker_handoff_verified"])):
        return None
    errors = {}
    for step in score["trace_errors"]:
        errors.setdefault(step.get("tool_call_id") or step.get("runtime_call_id"), step)
    calls = sdk_calls(sdk)
    steps = (sample.get("trace") or {}).get("steps", [])
    for row in sdk:
        if row["response"].get("status") != 200:
            return {"reason": "sdk_request_failed", "action": row,
                    "trace_step": errors.get(row.get("model_call_id"))}
        for call in (call for call in calls if call["attempt"] == row["attempt"]):
            if call["id"] in errors:
                return {"reason": "model_tool_action_rejected", "action": call, "trace_step": errors[call["id"]],
                        "model_messages": row["body"]["messages"]}
    if score["worker_handoff_required"] and not score["worker_handoff_verified"]:
        candidates = [task for task in score["worker_transfers"]
                      if DETAIL in task["task"]["capabilities"] or task["task"]["dependencies"]]
        first_task = candidates[0] if candidates else None
        sheet = next((call for call in score["task_sheets"]
                      if first_task and call["id"] == first_task["task_sheet_call_id"]), None)
        return {"reason": "missing_verified_worker_detail_chain", "task": first_task,
                "action": sheet or next(iter(score["task_sheets"]), None),
                "detail_attempts": score["detail_attempts"], "first_trace_error": score["first_trace_error"]}
    return {"reason": "business_or_lifecycle_incomplete", "failure": score["failure"],
            "last_model_action": calls[-1] if calls else None,
            "trace_step": score["first_trace_error"] or (steps[-1] if steps else None)}


def ledger_rows():
    with closing(sqlite3.connect(LEDGER.as_uri() + "?mode=ro", uri=True)) as conn:
        return [{"attempt": key, "run": run, "case": case, **json.loads(request),
                 "response": json.loads(response) if response else {"failure_reason": "no_http_response"}}
                for key, run, case, request, response in conn.execute(
                    "SELECT id,run,sample,request,response FROM sdk ORDER BY id")]


def archive(run, ledger):
    frozen, sql, samples, exported, summary = (read(run / name) for name in
                                               ("frozen.json", "sql-oracle.json", "rounds.json", "sdk.json", "summary.json"))
    require(summary["preflight_sdk_attempts"] == 0 and summary["storage_before"] == summary["storage_after"],
            "preflight_or_protected_storage_failed")
    persisted = [row for row in ledger if row["run"] == summary["run_id"]]
    require(len(persisted) == len(exported) == summary["sdk_attempts"], "sdk_export_count_differs")
    reconciled = []
    browser_failures = {row["id"] for row in samples if row.get("browser_failed")}
    for snapshot, row in zip(exported, persisted, strict=True):
        if snapshot == row:
            continue
        require(snapshot == {**row, "response": {"failure_reason": "no_http_response"}}
                and row["case"] in browser_failures and row["response"].get("end_ns"), "sdk_export_differs_from_ledger")
        reconciled.append({"attempt": row["attempt"], "case": row["case"], "snapshot_response": snapshot["response"],
                           "ledger_response": {key: value for key, value in row["response"].items() if key != "body"}})
    return frozen, sql, samples, persisted, reconciled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--recovery", type=Path)
    args = parser.parse_args()
    ledger = ledger_rows()
    counts = Counter(row["case"] for row in ledger)
    require(len(ledger) <= T6Audit.run_cap and counts.keys() <= TARGETS.keys()
            and all(count <= CASE_CAP for count in counts.values()), "t6_ledger_limits_failed")
    require(all(row["endpoint"] == ENDPOINT + "/chat/completions" and row["body"]["model"] == MODEL and
                (row["response"].get("status") != 200 or row["response"].get("body", {}).get("model") == MODEL)
                for row in ledger), "sdk_model_drift")
    original, sql, samples, sdk, reconciled = archive(args.archive, ledger)
    require({case: oracle["target"] for case, oracle in sql.items()} == TARGETS, "sql_targets_drift")
    require([row["id"] for row in samples] == list(TARGETS), "round_matrix_incomplete")
    frozen = frozen_fixture()
    cases = {case["id"]: case for case in frozen["cases"]}
    scored_sdk = sdk
    interrupted = {}
    if args.recovery:
        recovered, recovery_sql, recovery_samples, recovery_sdk, changes = archive(args.recovery, ledger)
        require(recovered == frozen and original["cases"][0] == frozen["cases"][0], "frozen_matrix_drift")
        review_names = list(TARGETS)[1:]
        require([row["id"] for row in recovery_samples] == review_names
                and all(sql[name] == recovery_sql[name] for name in review_names), "recovery_oracle_drift")
        for sample in samples[1:]:
            name = sample["id"]
            original_case = next(case for case in original["cases"] if case["id"] == name)
            require(sample["request"]["message"] == cases[name]["message"]
                    and original_case["message"].replace("\n", " ") == cases[name]["message"]
                    and sample.get("browser_failed") and not sample["sse"], "browser_recovery_mismatch")
            interrupted[name] = {"reason": "browser_assertion_after_input_normalized_newlines",
                                 "frozen_message": original_case["message"], "posted": sample["request"],
                                 "terminal_types_observed": [], "sdk_attempts": sample["sdk_attempts"]}
        samples = samples[:1] + recovery_samples
        scored_sdk = [row for row in sdk if row["case"] == "swap-1"] + recovery_sdk
        sdk += recovery_sdk
        reconciled += changes
    else:
        require(original == frozen, "frozen_matrix_drift")
    require(sorted(row["attempt"] for row in sdk) == [row["attempt"] for row in ledger], "unaccounted_sdk_attempt")
    rows, repro = [], {}
    for sample in samples:
        name = sample["id"]
        case = cases[name]
        requests = [row for row in scored_sdk if row["case"] == name]
        score = score_round(sample, case, sql[name], frozen, requests)
        rows.append(score)
        deviation = first_deviation(score, sample, case, requests)
        if deviation:
            repro[name] = {"message": case["message"], "bindings": case["bindings"], "sql_target": sql[name]["target"],
                           "sse_terminal": [event for event in sample["sse"] if event["type"] in {"done", "error"}],
                           "first_deviation": deviation}
            if score["worker_handoff_required"] and not score["worker_handoff_verified"]:
                dispatched = {worker["task_sheet_call_id"] for worker in score["worker_transfers"]}
                first_sheet = next((sheet for sheet in score["task_sheets"] if sheet["id"] in dispatched), None)
                if first_sheet:
                    repro[name]["first_dispatched_task_sheet"] = first_sheet
    group_ids = [row["lifecycle"]["group_id"] for row in rows if row["lifecycle"]["group_id"]]
    require(len(group_ids) == len(set(group_ids)), "group_reused")
    output = args.recovery or args.archive
    write(output / "audit.json", {"rounds": rows, "ledger": {"attempts": len(ledger), "by_case": counts,
                                                            "reconciled_sdk_snapshots": reconciled},
                                  "http_statuses": Counter(str(row["response"].get("status", "no_response")) for row in sdk),
                                  "sdk_retries": [row["attempt"] for row in sdk if row["sdk_retry"]],
                                  "interrupted_browser_rounds": interrupted, "passed": not repro})
    write(output / "repro.json", {"interrupted_browser_rounds": interrupted, "scored_rounds": repro})
    return int(bool(repro))


if __name__ == "__main__":
    raise SystemExit(main())
