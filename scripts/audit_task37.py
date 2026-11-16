"""Score business outcomes and observed workgroup paths independently."""

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from traceback import format_exc

from backend.rag.rule_reference import canonical_rule_query
from scripts.audit_task36 import action_records, audit_batches
from scripts.run_task27_t5 import ENDPOINT, MODEL, read
from scripts.task27_t3_fixture import OWNER, write
from scripts.verify_task28 import DETAIL, METRICS, RULE


def pairs(rows):
    return Counter((row["task_id"], row["artifact_id"]) for row in rows)


def business(frozen, scenario, sample, steps, reads, receipts, selects):
    terminal = sample["sse"][-1]
    assert terminal["type"] == "done"
    run, group = terminal["run_id"], terminal["conversation_id"]
    assert sample["request"] == {"message": scenario["message"], "conversation_id": group}
    ui = sample["refreshed"]
    assert ui == sample["initial"]
    answer, = [row for row in ui["history"]["entries"] if row["kind"] == "assistant" and row["run_id"] == run]
    assert answer["trace_id"] == terminal["trace_id"]
    assert [row["content"] for row in sample["sse"] if row["type"] == "text"] == [answer["content"]]
    content = answer["content"]
    for key in ("plans", "updates", "receipts", "diagnosis_receipts"):
        assert sample["before"][key] == sample["after"][key], key
    scoped = sample["scoped"]
    assert not scoped["receipts"] and not sample["pause_requests"]
    assert sample["after"]["proposals"] == sample["before"]["proposals"] + scoped["proposals"]
    detail = [row for row in reads if row["tool"] == "query_plan_detail"]
    detail_receipts = [row for row in receipts if row["source"] == DETAIL and row["status"] == "succeeded"]
    if scenario["kind"] == "status":
        row, = detail
        assert row["arguments"] == {"plan_id": 101, "user_id": OWNER}
        assert all(row["result"][key] == value for key, value in frozen["status"].items())
        assert re.search(r"投放中|正在投放|仍在投放|还在投放|继续投放|\bactive\b", content, re.I), content
        assert not re.search(r"未在投放|没有在投放|未投放|不在投放|已暂停|停止投放", content), content
        assert not scoped["proposals"] and len(reads) == 1 and not selects
        return answer

    missing = scenario["fixture"] == "missing"
    basis = ui["basis"]
    assert basis, "answer has no verified comparison basis"
    assert (basis["entry_id"], basis["run_id"], basis["conversation_id"]) == (answer["entry_id"], run, group)
    scope = basis["scope"]
    assert scope["plan_ids"] == [101, 103] and all(scope[key] == value for key, value in frozen["dates"].items())
    assert scope["rule"] == "ctr-review-v1#low-ctr" and scope["independent_review"]
    windows = [(plan, dates["start_date"], dates["end_date"]) for plan in (101, 103)
               for dates in frozen["dates"].values()]
    metrics = [row for row in reads if row["tool"] == "query_plan_metrics"]

    def window(row):
        return tuple(row[key] for key in ("plan_id", "start_date", "end_date"))

    assert set(map(window, basis["metrics"])) == set(windows) and len(basis["metrics"]) == 4
    assert set(map(lambda row: window(row["arguments"]), metrics)) == set(windows)
    expected_sql = set()
    for row in basis["metrics"]:
        plan, start, end = window(row)
        actual = [item for item in metrics if window(item["arguments"]) == (plan, start, end)]
        assert all(item["arguments"]["user_id"] == OWNER for item in actual)
        if missing and plan == 101:
            assert row["gap"] == "plan_history_not_mapped" and not row["artifact_id"]
            assert all(item["result"]["error_code"] == row["gap"] for item in actual)
            assert any(start in line and end in line and "未绑定历史来源" in line for line in content.splitlines())
            continue
        oracle = frozen["complete"]["sql"]["metrics"][str(plan)]
        expected = next(item for item in oracle["windows"] if item["start_date"] == start and item["end_date"] == end)
        expected_sql.add(tuple(expected["sql_params"]))
        assert row["gap"] is None and row["coverage"]["complete"] and row["coverage"]["observed_days"] == expected["days"]
        for key in ("impressions", "clicks"):
            assert row[key] == expected[key] and all(item["result"][key] == expected[key] for item in actual)
        assert abs(row["ctr_pct"] - 100 * expected["clicks"] / expected["impressions"]) < 1e-8
        assert f"{oracle['relative_decline_pct']:.2f}%" in content
        receipt, = [item for item in receipts if item["artifact_id"] == row["artifact_id"]]
        assert receipt["source"] == METRICS and receipt["status"] == "succeeded"
        assert receipt["tool_call_id"] in {item["runtime_call_id"] for item in actual}
    assert {tuple(re.findall(r"'([^']+)'", row["sql"])) for row in selects} == expected_sql
    assert all(row["uri_enabled"] and row["uri"].endswith("?mode=ro") for row in selects)
    assert len(selects) == sum(row["result"]["success"] for row in metrics)
    assert all(basis["rule"][key] == value for key, value in frozen["rule"].items())
    assert frozen["rule"]["chunk_id"] in content
    rule, = [row for row in receipts if row["artifact_id"] == basis["rule"]["artifact_id"]]
    assert rule["source"] == RULE and rule["status"] == "succeeded"
    assert any(row["tool"] == RULE and row["runtime_call_id"] == rule["tool_call_id"] for row in reads)
    decision, = [row["evidence"] for row in steps if (row["stage"], row["action"]) == ("conditional_detail", "reply")]
    assert decision["policy"]["min_impressions"] == 1000 and decision["policy"]["min_relative_decline_pct"] == 20
    assert decision["target_plan_ids"] == ([] if missing else [103])
    if missing:
        assert not detail and not detail_receipts and not scoped["proposals"] and not basis["details"]
        assert "暂缓出具复核清单" in content and "独立复核未完成" in content
        assert basis["review_status"] == "evidence_pending"
        assert "东港搜索" not in content and "北岸搜索" not in content
        return answer

    assert "独立复核已完成" in content and basis["review_status"] == "completed"
    row, = detail
    receipt, = detail_receipts
    shown, = basis["details"]
    plan = next(item for item in frozen["complete"]["fixture"]["ad_plans"] if item["id"] == 103)
    assert row["arguments"] == {"plan_id": 103, "user_id": OWNER}
    assert row["runtime_call_id"] == shown["runtime_call_id"] == receipt["tool_call_id"]
    assert shown["artifact_id"] == receipt["artifact_id"]
    for key in ("id", "plan_name", "status", "daily_budget"):
        assert shown[key] == row["result"][key] == receipt["result"][key] == plan[key]
    assert f"#103，名称 {plan['plan_name']}" in content
    proposal, = scoped["proposals"]
    action, = answer["pause_actions"]
    assert action["state"] == "pending" and action["target"]["plan_id"] == proposal["plan_id"] == 103
    assert proposal["action_id"] == action["action_id"] and ui["approvable"] == 1
    assert proposal["registration_id"] == frozen["complete"]["fixture"]["diagnosis_sandbox_targets"][0]["registration_id"]
    snapshot = json.loads(proposal["snapshot"])
    assert snapshot["source_trace_id"] == terminal["trace_id"]
    assert (snapshot["run_id"], snapshot["conversation_id"], snapshot["entry_id"]) == (run, group, answer["entry_id"])
    assert snapshot["evidence"] == {key: value for key, value in basis.items()
                                    if key not in {"run_id", "conversation_id", "entry_id"}}
    return answer


def path_proof(batches, steps, calls, reads, receipts, workers, *, expected_metrics=4):
    if not batches:
        if not reads:
            return {"path": "no_read"}
        assert not workers and all(row["role"] == "orchestrator" for row in reads)
        for row in reads:
            tool = {"query_plan_detail": DETAIL, "query_plan_metrics": METRICS}.get(row["tool"], row["tool"])
            _, action = next((call, action) for call, action in action_records(calls, tool)
                             if action["id"] == row["tool_call_id"])
            args = json.loads(action["function"]["arguments"])
            if tool == RULE:
                args["query"] = canonical_rule_query(args["query"])
            assert all(row["arguments"][key] == value for key, value in args.items())
        return {"path": "direct"}

    reviews = [(batch, task) for batch in batches for task in batch["tasks"] if DETAIL in task["capabilities"]]
    if not reviews:
        batch = batches[-1]
        state, = [row for row in steps if row["stage"] == "independent_review"
                  and row["runtime_call_id"] == batch["dispatch_id"]]
        return {"path": "evidence_only", "last_batch_review_state": state["evidence"]}
    batch, task = reviews[-1]
    worker_calls = [row for row in calls if row["task_id"] == task["task_id"] and row["role"] == "team_worker"]
    in_batch = {row["task_id"] for row in batch["tasks"]}
    if not worker_calls:
        assert set(task["dependencies"]) and set(task["dependencies"]) < in_batch
        assert not any(row["tool"] == "query_plan_detail" for row in reads)
        assert (any(row.get("error_code") == "dependency_failed" for row in steps)
                or any(row["source"] == "scheduler" and row.get("error_code") == "dependency_failed"
                       and row["task_id"] == task["task_id"] for row in receipts))
        return {"path": "same_batch_dependency_blocked"}
    worker = worker_calls[0]
    created, = [row for row in workers if row["runtime_call_id"] == worker["worker_id"]]
    assert created["parent_call_id"] == batch["dispatch_id"]
    context = json.loads(next(row["content"] for row in worker["body"]["messages"] if row["role"] == "user"))
    inputs = [row for rows in context["inputs"].values() for row in rows]
    assert Counter(row["source"] for row in inputs) == {METRICS: expected_metrics, RULE: 1}
    assert not context.get("direct_inputs")
    by_id = {row["artifact_id"]: row for row in receipts}
    for row in inputs:
        producer = by_id[row["artifact_id"]]
        assert producer["ns"] < worker["start_ns"]
        assert all(producer[key] == row[key] for key in (
            "task_id", "worker_id", "source", "status", "tool_call_id", "arguments", "result"))
    producers = {row["task_id"] for row in inputs}
    assert producers == set(task["dependencies"])
    finished = {row["task_id"]: row["ns"] for row in receipts
                if row["source"] == "worker_model" and row["status"] == "succeeded" and row["task_id"] in producers}
    assert set(finished) == producers and max(finished.values()) < worker["start_ns"]
    result = {"path": "same_batch", "review_worker_first_input": worker, "producer_refs": [
        {key: row[key] for key in ("task_id", "artifact_id", "worker_id", "source")} for row in inputs],
        "producer_finished_ns": finished}
    if producers <= in_batch:
        assert producers == in_batch - {task["task_id"]}
        assert len(batches) == 1
        return result

    assert len(batches) == 2 and batch is batches[-1]
    first = batches[0]
    assert producers == {row["task_id"] for row in first["tasks"]}
    call, action = next((call, action) for call, action in action_records(calls, "delegate_tasks")
                        if action["id"] == batch["sdk_tool_call_id"])
    append = next(row for row in json.loads(action["function"]["arguments"])["tasks"] if row["task_id"] == task["task_id"])
    receipt, = [json.loads(row["content"]) for row in call["body"]["messages"] if row["role"] == "tool"
                and row["tool_call_id"] == first["sdk_tool_call_id"]]
    state = receipt["independent_review"]
    assert state["status"] == "review_required" and pairs(append["artifact_refs"]) == pairs(state["artifact_refs"]) == pairs(inputs)
    published = {row["artifact_id"]: row for row in receipt["evidence"]}
    for row in inputs:
        actual = published[row["artifact_id"]]
        assert by_id[row["artifact_id"]]["ns"] < call["start_ns"] < worker["start_ns"]
        assert all(actual[key] == row[key] for key in ("task_id", "source", "arguments", "result"))
        assert actual["runtime_call_id"] == row["tool_call_id"]
    return {**result, "path": "appended_review", "first_batch_receipt": receipt, "append_request": call}


def audit_run(output, selected=None):
    report, frozen = (read(output / name) for name in ("report.json", "frozen.json"))
    traces = {row["trace_id"]: row["steps"] for row in
              map(json.loads, (output / "trace_log_file.jsonl").read_text(encoding="utf-8").splitlines())}
    with closing(sqlite3.connect(Path(report["ledger"]).as_uri() + "?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        sdk = [{"attempt": attempt, "run": run, "case": name, **json.loads(request),
                "response": json.loads(response) if response else {"failure_reason": "no_http_response"}}
               for attempt, run, name, request, response in conn.execute(
                   "SELECT id,run,sample,request,response FROM sdk WHERE run=? ORDER BY id", (report["ledger_run"],))]
    assert len(sdk) == report["sdk_attempts"]
    assert all(row["body"]["model"] == MODEL and row["endpoint"] == ENDPOINT + "/chat/completions" for row in sdk)
    scores = {}
    for name in selected or report["cases"]:
        folder = output / name
        scenario = (frozen["cases"] | frozen["holdout"])[name]
        calls, reads, selects, receipts, sheets = (read(folder / file) for file in
            ("sdk.json", "reads.json", "criteo-selects.json", "receipts.json", "accepted-task-sheets.json"))
        live = report["mode"] == "live"
        if live:
            assert calls == [row for row in sdk if row["case"] == name]
        receipts = [row for row in receipts if row["accepted"]]
        browser = read(folder / "browser.json") if (folder / "browser.json").exists() else {}
        terminal = (browser.get("sse") or [{}])[-1]
        steps = traces.get(terminal.get("trace_id"), [])
        workers = [row for row in steps if (row["stage"], row["action"]) == ("worker", "created")]
        usage = Counter()
        for row in calls:
            usage.update({key: value for key, value in (row.get("response", {}).get("body", {}).get("usage") or {}).items()
                          if type(value) is int})
        score = {"business": "fail", "path": "unverified", "path_evidence": "fail",
                 "sdk_attempts": len(calls) if live else 0,
                 "sdk_retries": sum(row.get("sdk_retry", 0) > 0 for row in calls),
                 "usage": dict(usage), "bottom_reads": dict(Counter(row["tool"] for row in reads)),
                 "criteo_selects": len(selects), "workers": len(workers), "turn_ms": browser.get("turn_ms"),
                 "success_detail_receipts": sum(row["source"] == DETAIL and row["status"] == "succeeded" for row in receipts),
                 "pause_proposals": len(browser.get("scoped", {}).get("proposals", [])),
                 "trace_id": terminal.get("trace_id"), "run_id": terminal.get("run_id"),
                 "conversation_id": terminal.get("conversation_id")}
        if not live:
            score["fixed_model_calls"] = len(calls)
        relevant = [row for row in steps if row.get("error_code") and (
            row["stage"] in {"conditional_detail", "tool_result", "model_call"} or row["action"] == "delegate_tasks")
            and not (scenario["fixture"] == "missing" and row["error_code"] == "plan_history_not_mapped"
                     and ((row.get("evidence") or {}).get("arguments") or {}).get("plan_id") == 101)]
        score["first_runtime_deviation"] = relevant[0] if relevant else None
        try:
            assert browser.get("chromium") and report["cases"][name]["browser"] == "pass"
            answer = business(frozen, scenario, browser, steps, reads, receipts, selects)
            score.update(business="pass", entry_id=answer["entry_id"])
            if scenario["kind"] == "status":
                plan = frozen["status"]
                score["status_only_reply"] = answer["content"] == f"#{plan['id']} {plan['plan_name']} 当前投放中。"
                if not live:
                    assert score["status_only_reply"] and len(calls) == 1
        except (AssertionError, KeyError, TypeError, ValueError, StopIteration):
            score["business"] = "fail"
            write(folder / "business-deviation.json", {"error": format_exc(), "first_runtime_deviation": score["first_runtime_deviation"]})
        try:
            assert steps and terminal.get("type") == "done"
            assert all(row["trace_id"] == terminal["trace_id"] for row in [*calls, *reads, *sheets])
            trace_reads = {row.get("runtime_call_id") for row in steps if row["stage"] == "tool_result"}
            assert all(row.get("trace_id") == terminal["trace_id"] or row["tool_call_id"] in trace_reads
                       for row in receipts)
            batches = audit_batches(sheets, steps, calls, terminal["run_id"], evidence_only_first=False)
            proof = path_proof(batches, steps, calls, reads, receipts, workers)
            write(folder / "path.json", {**proof, "batches": batches})
            score.update(path=proof["path"], path_evidence="pass")
        except (AssertionError, KeyError, TypeError, ValueError, StopIteration):
            write(folder / "path-deviation.json", {"error": format_exc()})
        scores[name] = score
        write(folder / "audit.json", score)
    result = {"mode": report["mode"], "sdk_attempts": len(sdk), "http_statuses": report["http_statuses"],
              "storage_unchanged": report["storage_before"] == report["storage_after"],
              "business_passed": sum(row["business"] == "pass" for row in scores.values()),
              "path_counts": dict(Counter(row["path"] for row in scores.values())), "cases": scores}
    write(output / "audit.json", result)
    return result["business_passed"] == len(scores) and all(row["path_evidence"] == "pass" for row in scores.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    raise SystemExit(int(not audit_run(parser.parse_args().output.resolve())))
