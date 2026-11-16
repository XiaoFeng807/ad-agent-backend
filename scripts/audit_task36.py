"""Audit TASK-036 from browser, SDK ledger, scheduler, trace and SQLite exports."""

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from traceback import format_exc

from scripts.run_task27_t5 import ENDPOINT, MODEL, read
from scripts.task27_t3_fixture import OWNER, write
from scripts.verify_task28 import DETAIL, METRICS, RULE


def action_records(calls, name):
    def actions(call):
        if "tool_calls" in call:
            return call["tool_calls"]
        return [action for choice in call.get("response", {}).get("body", {}).get("choices", [])
                for action in choice["message"].get("tool_calls", [])]

    return [(call, action) for call in calls for action in actions(call) if action["function"]["name"] == name]


def audit_batches(sheets, steps, calls, run, *, evidence_only_first=True):
    raw = action_records(calls, "delegate_tasks")
    batches = []
    for sheet in sheets:
        dispatch = sheet["dispatch_id"]
        began, = [(i, row) for i, row in enumerate(steps) if row["stage"] == "batch"
                  and row["action"] == "started" and row.get("parent_call_id") == dispatch]
        assert began[1]["detail"] == run
        invocation, = [row for row in steps if row["stage"] == "tool_call" and row["action"] == "delegate_tasks"
                       and row.get("runtime_call_id") == dispatch]
        call, action = next((call, action) for call, action in raw if action["id"] == invocation["tool_call_id"])
        assert call["role"] == "orchestrator"
        proposed = json.loads(action["function"]["arguments"])
        for actual, task in zip(sheet["tasks"], proposed["tasks"], strict=True):
            assert all(actual[key] == task[key] for key in ("task_id", "objective", "capabilities"))
        batches.append({**sheet, "run_id": run, "trace_index": began[0], "sdk_attempt": call.get("attempt"),
                        "sdk_tool_call_id": action["id"], "proposed": proposed})
    if evidence_only_first:
        assert batches, "no accepted evidence batch"
        capabilities = {cap for task in batches[0]["tasks"] for cap in task["capabilities"]}
        assert capabilities == {METRICS, RULE}, ("first batch is not evidence-only", capabilities)
    return batches


def audit_case(folder, frozen, trace, sample, calls):
    name = folder.name
    complete = name == "complete"
    terminal = sample["sse"][-1]
    run, group, trace_id = terminal["run_id"], terminal["conversation_id"], terminal["trace_id"]
    assert sample["request"] == {"message": frozen["message"], "conversation_id": group}
    assert all(row["trace_id"] == trace_id for row in calls) and calls
    assert all(row["trace_id"] == trace_id and row["run_id"] == run for row in sample["sse"])
    ui = sample["refreshed"]
    assert ui == sample["initial"]
    basis = ui["basis"]
    assert (basis["conversation_id"], basis["run_id"]) == (group, run)
    history = ui["history"]
    stored_run, = history["runs"]
    assert (stored_run["run_id"], stored_run["status"]) == (run, "completed")
    answer, = [row for row in history["entries"] if row["kind"] == "assistant"]
    assert (answer["entry_id"], answer["run_id"], answer["trace_id"]) == (basis["entry_id"], run, trace_id)
    assert [row["content"] for row in sample["sse"] if row["type"] == "text"] == [answer["content"]]
    steps = trace["steps"]
    decision, = [row["evidence"] for row in steps if (row["stage"], row["action"]) == ("conditional_detail", "reply")]
    assert decision["target_plan_ids"] == ([103] if complete else [])
    assert decision["independent_review_status"] == basis["review_status"] == (
        "completed" if complete else "evidence_pending")
    assert ("独立复核已完成" if complete else "独立复核未完成") in ui["answer"]
    assert all(basis["rule"][key] == value for key, value in frozen["rule"].items())
    assert all(decision["policy"][key] == value for key, value in {
        "chunk_id": frozen["rule"]["chunk_id"], "min_impressions": 1000, "min_relative_decline_pct": 20,
    }.items())

    scheduled = read(folder / "accepted-task-sheets.json")
    batches = audit_batches(scheduled, steps, calls, run)
    receipts = [row for row in read(folder / "receipts.json") if row["accepted"]]
    by_id = {row["artifact_id"]: row for row in receipts}
    reads = read(folder / "reads.json")
    assert all(row["trace_id"] == trace_id for row in [*receipts, *reads, *scheduled])
    events = [row["event"] for row in history["entries"] if row["kind"] == "worker"]
    evidence_tasks = {row["task_id"] for row in batches[0]["tasks"]}
    window_keys = ("start_date", "end_date")
    windows = {tuple(window[key] for key in window_keys): label for label, window in frozen["dates"].items()}
    expected_windows = {(plan, *dates) for plan in frozen["plans"] for dates in windows}
    assert {(row["plan_id"], *(row[key] for key in window_keys)) for row in basis["metrics"]} == expected_windows
    comparison = frozen["complete"]["sql"]["metrics"]
    evidence = []
    for metric in basis["metrics"]:
        plan = metric["plan_id"]
        dates = tuple(metric[key] for key in window_keys)
        gap = "plan_history_not_mapped" if not complete and plan == 101 else None
        assert metric["gap"] == gap
        if gap:
            matches = [row for row in receipts if row["source"] == METRICS and row["error_code"] == gap
                       and row["arguments"]["plan_id"] == plan
                       and tuple(row["arguments"][key] for key in window_keys) == dates]
            producer = next(row for row in matches if row["task_id"] in evidence_tasks)
            assert producer["status"] == "failed"
            assert "未绑定历史来源" in answer["content"] and all(date in answer["content"] for date in dates)
        else:
            producer = by_id[metric["artifact_id"]]
            assert producer["source"] == METRICS and producer["status"] == "succeeded"
            oracle = next(row for row in comparison[str(plan)]["windows"]
                          if tuple(row[key] for key in window_keys) == dates)
            for key in ("impressions", "clicks"):
                assert producer["result"][key] == metric[key] == oracle[key]
                assert f"{oracle[key]:,}" in ui["answer"]
            assert metric["coverage"]["observed_days"] == oracle["days"] == 7 and metric["coverage"]["complete"]
            ctr = 100 * oracle["clicks"] / oracle["impressions"]
            assert abs(metric["ctr_pct"] - ctr) < 1e-8 and f"{ctr:.2f}%" in ui["answer"]
            assert f"{comparison[str(plan)]['relative_decline_pct']:.2f}%" in ui["answer"]
        actual, = [row for row in reads if row["runtime_call_id"] == producer["tool_call_id"]
                   and row["tool"] == "query_plan_metrics"]
        assert actual["parent_call_id"] == producer["worker_id"]
        assert all(actual["arguments"][key] == metric[key] == producer["arguments"][key]
                   for key in ("plan_id", *window_keys))
        assert actual["result"].get("error_code") == gap and actual["result"]["success"] == (gap is None)
        evidence.append({**producer, "plan_id": plan, "window": windows[dates]})
    rule = by_id[basis["rule"]["artifact_id"]]
    hit, = [hit for hit in rule["result"]["evidence"] if hit["chunk_id"] == frozen["rule"]["chunk_id"]]
    assert rule["source"] == RULE and rule["status"] == "succeeded"
    assert all(hit[key] == value for key, value in frozen["rule"].items())
    evidence.append(rule)
    assert {row["task_id"] for row in evidence} <= evidence_tasks
    for row in evidence:
        event, = [event for event in events if event.get("artifact_id") == row["artifact_id"]]
        assert (event["worker_id"], event["runtime_call_id"]) == (row["worker_id"], row["tool_call_id"])
    assert frozen["rule"]["chunk_id"] in ui["answer"]

    details = [row for row in reads if row["tool"] == "query_plan_detail"]
    detail_receipts = [row for row in receipts if row["source"] == DETAIL and row["status"] == "succeeded"]
    admissions = [row for row in steps if row["stage"] == "conditional_detail" and row["action"] in {"allowed", "blocked"}]
    allowed = [row for row in admissions if row["action"] == "allowed"]
    detail_traces = [row for row in steps if row["action"] == DETAIL and row["stage"] == "tool_result"]
    approvals = answer.get("pause_actions", [])
    scoped = sample["scoped"]
    before, after = sample["before"], sample["after"]
    for key in ("plans", "receipts", "diagnosis_receipts", "updates"):
        assert after[key] == before[key]
    assert not sample["pause_requests"] and not scoped["receipts"]
    assert len(scoped["proposals"]) == len(approvals) == ui["approvable"] == int(complete)
    assert after["proposals"] == before["proposals"] + scoped["proposals"]
    result = {"run_id": run, "trace_id": trace_id, "conversation_id": group, "entry_id": basis["entry_id"],
              "chromium": sample["chromium"], "batches": batches, "evidence": evidence,
              "detail_reads": details, "success_detail_receipts": detail_receipts, "admissions": admissions,
              "detail_tool_results": detail_traces,
              "proposal_count": len(scoped["proposals"]), "execution_receipt_count": len(scoped["receipts"]),
              "decision": decision}
    if complete:
        audit_append(result, calls, steps, evidence, basis, approvals, scoped, frozen)
    else:
        assert not details and not detail_receipts and not allowed and not basis["details"]
        assert all(row["status"] == "error" for row in detail_traces)
        assert all(event.get("source") != DETAIL or event.get("receipt_status") != "succeeded" for event in events)
        plan = next(row for row in before["plans"] if row["id"] == 103)
        assert plan["plan_name"] not in ui["answer"] and plan["plan_name"] not in json.dumps(history, ensure_ascii=False)
        assert not ui["pause"]
        attempts = [{"sdk_attempt": call.get("attempt"), "role": call["role"], "action": action}
                    for call, action in action_records(calls, DETAIL)
                    + [(call, action) for call, action in action_records(calls, "delegate_tasks")
                       if any(DETAIL in task.get("capabilities", [])
                              for task in json.loads(action["function"]["arguments"])["tasks"])]]
        first_state, = [row for row in steps if row["stage"] == "independent_review"
                        and row.get("runtime_call_id") == batches[0]["dispatch_id"]]
        assert first_state["action"] == "evidence_pending"
        result.update(gap_action="attempted_and_blocked" if attempts else "stopped_after_gaps",
                      detail_attempts=attempts, first_batch_status=first_state["evidence"],
                      detail_trace=[row for row in steps if row["action"] == DETAIL
                                    or row.get("error_code") == "dependency_failed"])
    return result


def audit_append(result, calls, steps, evidence, basis, approvals, scoped, frozen):
    first, append = result["batches"]
    reviewer, = append["tasks"]
    task, = append["proposed"]["tasks"]
    assert reviewer["capabilities"] == task["capabilities"] == [DETAIL]
    refs = [{key: row[key] for key in ("task_id", "artifact_id")} for row in evidence]
    def pairs(rows):
        return Counter((row["task_id"], row["artifact_id"]) for row in rows)

    assert pairs(task["artifact_refs"]) == pairs(refs)
    assert set(reviewer["dependencies"]) == {row["task_id"] for row in refs}
    call, = [call for call, action in action_records(calls, "delegate_tasks")
             if action["id"] == append["sdk_tool_call_id"]]
    schema, = [tool["function"]["parameters"] for tool in call["body"]["tools"]
               if tool["function"]["name"] == "delegate_tasks"]
    assert "artifact_refs" in schema["$defs"]["TeamTask"]["required"]
    receipt, = [json.loads(message["content"]) for message in call["body"]["messages"]
                if message["role"] == "tool" and message["tool_call_id"] == first["sdk_tool_call_id"]]
    state = receipt["independent_review"]
    assert state["status"] == "review_required" and pairs(state["artifact_refs"]) == pairs(refs)
    rejected = [row for row in steps if row["action"] == "delegate_tasks"
                and row.get("error_code") == "invalid_input_reference"]
    if "response" not in call:
        rejection, = rejected
        assert rejection["evidence"]["errors"][0]["field"] == "tasks[0].artifact_refs"
        assert pairs(rejection["evidence"]["artifact_refs"]) == pairs(refs)
    for rejection in rejected:
        assert not any(row["stage"] == "batch" and row.get("parent_call_id") == rejection["runtime_call_id"]
                       for row in steps)
    server = {row["artifact_id"]: row for row in receipt["evidence"]}
    for row in evidence:
        assert all(server[row["artifact_id"]][key] == row[key] for key in ("task_id", "source", "arguments", "result"))
        assert server[row["artifact_id"]]["runtime_call_id"] == row["tool_call_id"]
    index, = [i for i, row in enumerate(steps) if row["stage"] == "independent_review"
              and row.get("runtime_call_id") == first["dispatch_id"] and row["evidence"] == state]
    model_index, = [i for i, row in enumerate(steps) if row["stage"] == "model_call"
                    and row.get("runtime_call_id") == call["model_call_id"]]
    assert first["trace_index"] < index < model_index < append["trace_index"]
    if "start_ns" in call:
        assert max(row["ns"] for row in evidence) < call["start_ns"]
        assert first["sdk_attempt"] < append["sdk_attempt"]

    worker_calls = [row for row in calls if row["task_id"] == reviewer["task_id"] and row["role"] == "team_worker"]
    worker = worker_calls[0]
    context = json.loads(next(row["content"] for row in worker["body"]["messages"] if row["role"] == "user"))
    inputs = [row for rows in context["inputs"].values() for row in rows]
    assert pairs(inputs) == pairs(refs) and not context.get("direct_inputs")
    by_id = {row["artifact_id"]: row for row in inputs}
    for row in evidence:
        assert all(by_id[row["artifact_id"]][key] == row[key] for key in (
            "task_id", "worker_id", "source", "status", "tool_call_id", "arguments", "result"))
    detail, = result["detail_reads"]
    detail_receipt, = result["success_detail_receipts"]
    admission, = [row for row in result["admissions"] if row["action"] == "allowed"]
    runtime = detail["runtime_call_id"]
    worker_id = worker["worker_id"]
    assert detail["role"] == "team_worker" and detail["arguments"] == {"plan_id": 103, "user_id": OWNER}
    assert detail["parent_call_id"] == detail_receipt["worker_id"] == admission["parent_call_id"] == worker_id
    assert detail_receipt["tool_call_id"] == admission["runtime_call_id"] == runtime
    tool_result, = [row for row in result["detail_tool_results"] if row["runtime_call_id"] == runtime]
    assert (tool_result["status"], tool_result["parent_call_id"], tool_result["tool_call_id"]) == (
        "ok", worker_id, detail["tool_call_id"])
    proof = admission["evidence"]["review_input"]
    assert (proof["worker_id"], proof["run_id"], proof["conversation_id"], proof["model_call_id"]) == (
        worker_id, result["run_id"], result["conversation_id"], worker["model_call_id"])
    assert proof["input_artifact_ids"] == [row["artifact_id"] for row in inputs]
    action_call, action = next((call, action) for call, action in action_records(worker_calls, DETAIL)
                               if action["id"] == detail["tool_call_id"])
    assert action["id"] == admission["tool_call_id"] and action_call["model_call_id"] == proof["action_model_call_id"]
    assert json.loads(action["function"]["arguments"]) == {"plan_id": 103}
    shown, = basis["details"]
    plan = next(row for row in frozen["complete"]["fixture"]["ad_plans"] if row["id"] == 103)
    for key in ("id", "plan_name", "status", "daily_budget"):
        assert shown[key] == detail_receipt["result"][key] == detail["result"][key] == plan[key]
    assert shown["artifact_id"] == detail_receipt["artifact_id"] and shown["review_input"] == proof
    assert shown["runtime_call_id"] == runtime
    proposal, = scoped["proposals"]
    pending, = approvals
    assert pending["state"] == "pending" and pending["target"]["plan_id"] == 103
    assert proposal["action_id"] == pending["action_id"] and proposal["plan_id"] == 103
    assert proposal["registration_id"] == frozen["complete"]["fixture"]["diagnosis_sandbox_targets"][0]["registration_id"]
    snapshot = json.loads(proposal["snapshot"])
    assert all(snapshot[key] == result[key] for key in ("run_id", "conversation_id", "entry_id"))
    assert snapshot["source_trace_id"] == result["trace_id"]
    assert snapshot["evidence"] == {key: value for key, value in basis.items()
                                    if key not in {"run_id", "conversation_id", "entry_id"}}
    result.update(first_batch_receipt=receipt, append_request=call, rejected_append_attempts=rejected,
                  review_worker_first_input=worker,
                  detail_action=action, pending_proposal=proposal)


def audit_run(output):
    report = read(output / "report.json")
    frozen = read(output / "frozen.json")
    traces = [json.loads(line) for line in (output / "trace_log_file.jsonl").read_text(encoding="utf-8").splitlines()]
    traces = {trace["trace_id"]: trace for trace in traces}
    sdk = []
    if report["mode"] == "live":
        with closing(sqlite3.connect(Path(report["ledger"]).as_uri() + "?mode=ro", uri=True)) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            sdk = [{"attempt": attempt, "run": run, "case": name, **json.loads(request),
                    "response": json.loads(response) if response else {"failure_reason": "no_http_response"}}
                   for attempt, run, name, request, response in conn.execute(
                       "SELECT id,run,sample,request,response FROM sdk WHERE run=? ORDER BY id", (report["ledger_run"],))]
        assert len(sdk) == report["sdk_attempts"] and sdk
        assert all(row["body"]["model"] == MODEL and row["endpoint"] == ENDPOINT + "/chat/completions" for row in sdk)
    cases = {}
    for name in ("complete", "missing"):
        folder = output / name
        try:
            assert report["cases"][name]["browser"] == "pass"
            calls = read(folder / "sdk.json")
            if sdk:
                assert calls == [row for row in sdk if row["case"] == name]
            sample = read(folder / "browser.json")
            trace = traces[sample["sse"][-1]["trace_id"]]
            proof = audit_case(folder, frozen, trace, sample, calls)
            write(folder / "audit.json", proof)
            if name == "complete":
                write(folder / "cross-batch.json", {key: proof[key] for key in (
                    "run_id", "trace_id", "batches", "first_batch_receipt", "append_request",
                )})
                write(folder / "review-worker-first-input.json", proof["review_worker_first_input"])
            cases[name] = {"passed": True, "sdk_attempts": len(calls) if sdk else 0,
                           **{key: proof[key] for key in ("run_id", "trace_id", "conversation_id", "entry_id")}}
        except Exception:
            cases[name] = {"passed": False}
            write(folder / "audit-deviation.json", {"error": format_exc()})
    if all(row["passed"] for row in cases.values()):
        assert len({row["conversation_id"] for row in cases.values()}) == 2
    write(output / "audit.json", {"passed": all(row["passed"] for row in cases.values()), "cases": cases,
                                  "sdk_attempts": len(sdk), "storage_unchanged": report["storage_before"] == report["storage_after"]})
    return all(row["passed"] for row in cases.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    raise SystemExit(int(not audit_run(parser.parse_args().output.resolve())))
