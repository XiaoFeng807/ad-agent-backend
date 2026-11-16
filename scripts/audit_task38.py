"""Match independent SQL to the actual alert, work, UI, and SDK receipts."""

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from traceback import format_exc

from scripts.audit_task36 import action_records, audit_batches
from scripts.audit_task37 import path_proof
from scripts.run_task27_t5 import ENDPOINT, MODEL, read
from scripts.task27_t3_fixture import write
from scripts.verify_task38 import ALERT, DETAIL, FOREIGN_NAME, GAP, METRICS, PROMPT, RULE, TRANSFER_DURING


def check_case(name, case, calls, reads, receipts, sheets, selects, browser, steps, transfer=None):
    terminal = browser["sse"][-1]
    run, group = terminal["run_id"], terminal["conversation_id"]
    assert terminal["type"] == "done" and browser["chromium"]
    assert browser["request"] == {"message": PROMPT, "conversation_id": group}
    assert all(row["trace_id"] == terminal["trace_id"] for row in [*calls, *reads, *sheets])
    ui = browser["refreshed"]
    assert ui == browser["initial"]
    evidence = ui["basis"]
    answer, = [row for row in ui["history"]["entries"] if row["kind"] == "assistant"]
    assert answer["entry_id"] == evidence["entry_id"] and answer["run_id"] == evidence["run_id"] == run
    assert [row["content"] for row in browser["sse"] if row["type"] == "text"] == [answer["content"]]
    assert len(ui["history"]["runs"]) == 1 and ui["history"]["runs"][0]["status"] == "completed"
    before = browser["before"]["plans"]
    if transfer:
        change = case["oracle"]["transfer"]
        plan, = [row for row in before if row["id"] == change["plan_id"]]
        assert plan["user_id"] == change["from_owner"]
        before = [dict(row, user_id=change["to_owner"]) if row["id"] == change["plan_id"] else row for row in before]
    assert browser["after"]["plans"] == before
    for key in ("updates", "receipts", "diagnosis_receipts"):
        assert browser["after"][key] == browser["before"][key]
    assert not browser["pause_requests"] and not browser["scoped"]["receipts"]
    payload = json.dumps([ui, calls, receipts], ensure_ascii=False)
    assert FOREIGN_NAME not in payload and not re.search(r'#201\b|"plan_id"\s*:\s*201\b', payload)
    assert not any(row["arguments"].get("plan_id") == 201 for row in reads)

    receipt = evidence["alert_review"]
    trace, = [row for row in steps if row["stage"] == "tool_result" and row["action"] == ALERT]
    call, action = next((call, action) for call, action in action_records(calls, ALERT)
                        if action["id"] == trace["tool_call_id"])
    assert call["role"] == "orchestrator" and call is calls[0]
    assert json.loads(action["function"]["arguments"]) == {}
    assert trace["runtime_call_id"] == receipt["runtime_call_id"]
    assert len([row for row in reads if row["tool"] == ALERT]) == 1
    if name == TRANSFER_DURING:
        return check_transfer(case, calls, reads, receipts, sheets, selects, browser, steps, trace, call, transfer)
    assert trace["evidence"] == receipt
    oracle = case["oracle"]
    ids = oracle["candidate_plan_ids"]
    assert receipt["candidate_plan_ids"] == ids
    assert receipt["associations"] == [
        {key: row[key] for key in ("alert_id", "created_at", "plan_id", "ctr_check") if key in row}
        for row in oracle["alerts"] if not row["gap"]]
    assert receipt["association_gaps"] == [
        {"alert_id": row["alert_id"], "created_at": row["created_at"], "error_code": row["gap"]}
        for row in oracle["alerts"] if row["gap"]]
    assert receipt["alert_period"] == {"timezone": "Asia/Shanghai", "start": "2026-11-09T00:00:00+08:00",
                                      "end": "2026-11-12T10:30:00+08:00"}
    for item in receipt["associations"]:
        assert f"告警 #{item['alert_id']} → 计划 #{item['plan_id']}" in answer["content"]
    blocked = [row for row in steps if row.get("error_code") and
               (row["stage"] == "alert_review" or row["action"] in ("delegate_tasks", DETAIL, METRICS))]
    if name != "normal":
        assert not any(row["role"] == "team_worker" for row in calls)
        alert_id, _, explanation = GAP[name]
        assert f"告警 #{alert_id}，{explanation}" in answer["content"]
        assert "暂缓出具复核清单" in answer["content"]
        assert "<tool_call>" not in answer["content"] and "delegate_tasks" not in answer["content"]
        assert receipt["status"] == "incomplete" and receipt["review_scope"] is None
        assert not evidence["metrics"] and not evidence["details"] and not evidence["rule"]
        assert not sheets and not selects and not receipts
        assert [row["tool"] for row in reads] == [ALERT]
        assert not browser["scoped"]["proposals"] and browser["before"]["proposals"] == browser["after"]["proposals"]
        if name == "missing":
            assert "#101" not in payload and "北岸搜索" not in payload
        elif name in ("transfer", "deleted"):
            invalid_plan = 102 if name == "transfer" else 202
            assert not re.search(rf'#{invalid_plan}\b|"plan_id"\s*:\s*{invalid_plan}\b', payload)
            assert not any(row["arguments"].get("plan_id") == invalid_plan for row in reads)
        last = calls[-1]
        final = last.get("assistant_content")
        if final is None:
            choices = last.get("response", {}).get("body", {}).get("choices", [])
            final = choices[0]["message"].get("content") if choices else None
        return {"path": "stopped_after_alert_receipt", "blocked_attempts": blocked,
                "alert_tool_call": {"attempt": call.get("attempt"), "receipt": receipt},
                "model_stop": {"attempt": last.get("attempt"), "reply": final,
                               "unstructured_tool_text": "<tool_call>" in (final or "")}}

    assert receipt["status"] == "ready"
    assert {key: evidence["scope"][key] for key in oracle["dates"]} == oracle["dates"]
    assert evidence["scope"] == receipt["review_scope"] and evidence["rule"]["version"] == "v1"
    assert "ctr-review-v1-low-ctr-01" in answer["content"]
    for item in oracle["dates"].values():
        assert all(value in answer["content"] for value in item.values())
    expected = {(row["plan_id"], row["start_date"], row["end_date"]): row
                for plan in oracle["metrics"] for row in plan["windows"]}
    metric_reads = [row for row in reads if row["tool"] == "query_plan_metrics"]
    assert len(metric_reads) == len(evidence["metrics"]) == len(expected) == 2 * len(ids)
    assert sum(row["tool"] == RULE for row in reads) == 1
    for row in evidence["metrics"]:
        facts = expected[row["plan_id"], row["start_date"], row["end_date"]]
        assert all(row[key] == facts[key] for key in ("impressions", "clicks"))
        assert row["coverage"]["complete"] and row["coverage"]["observed_days"] == facts["days"]
        source, = [item for item in receipts if item["artifact_id"] == row["artifact_id"]]
        sql, = [item for item in selects if item["runtime_call_id"] == source["tool_call_id"]]
        assert "mode=ro" in sql["uri"] and sql["uri_enabled"]
        assert all(value in sql["sql"] for value in facts["sql_params"])
    coverage = [item for item in selects if item["runtime_call_id"] == receipt["runtime_call_id"]]
    assert len(coverage) == len(ids)
    assert all("SELECT metric_date FROM plan_daily_metrics" in row["sql"] and "mode=ro" in row["uri"]
               for row in coverage)
    assert len(selects) == len(coverage) + len(expected)
    assert [row["id"] for row in evidence["details"]] == oracle["targets"] == [103]
    assert evidence["review_status"] == "completed"
    detail, = [row for row in reads if row["tool"] == "query_plan_detail"]
    success, = [row for row in receipts if row["source"] == DETAIL and row["status"] == "succeeded"]
    allowed, = [row for row in steps if (row["stage"], row["action"]) == ("conditional_detail", "allowed")]
    assert detail["arguments"]["plan_id"] == 103 and detail["role"] == "team_worker"
    assert allowed["runtime_call_id"] == detail["runtime_call_id"] == success["tool_call_id"]
    assert allowed["evidence"]["target_plan_ids"] == [103]
    assert [row["plan_id"] for row in browser["scoped"]["proposals"]] == [103]
    proposal, = browser["scoped"]["proposals"]
    assert browser["after"]["proposals"] == [*browser["before"]["proposals"], proposal]
    assert json.loads(proposal["snapshot"])["evidence"]["alert_review"] == receipt
    batches = audit_batches(sheets, steps, calls, run, evidence_only_first=False)
    for batch in batches:
        main, _ = next((main, action) for main, action in action_records(calls, "delegate_tasks")
                       if action["id"] == batch["sdk_tool_call_id"])
        receipt_input, = [json.loads(row["content"]) for row in main["body"]["messages"]
                          if row.get("tool_call_id") == trace["tool_call_id"]]
        assert receipt_input == receipt
        assert call["start_ns"] < main["start_ns"]
    workers = [row for row in steps if (row["stage"], row["action"]) == ("worker", "created")]
    proof = path_proof(batches, steps, calls, reads, receipts, workers, expected_metrics=len(expected))
    assert proof["path"] in {"same_batch", "appended_review"}
    return {**proof, "accepted_batches": batches, "blocked_attempts": blocked,
            "alert_tool_call": {"attempt": call.get("attempt"), "receipt": receipt},
            "detail_admission": allowed, "pause_action_id": proposal["action_id"]}


def check_transfer(case, calls, reads, receipts, sheets, selects, browser, steps, discovery, call, transfer):
    oracle = case["oracle"]
    plan = oracle["transfer"]["plan_id"]
    initial = discovery["evidence"]
    assert initial["status"] == "ready" and initial["candidate_plan_ids"] == oracle["candidate_plan_ids"]
    assert not initial["association_gaps"] and oracle["targets"] == [plan]
    assert initial["associations"] == [{key: row[key] for key in ("alert_id", "created_at", "plan_id")}
                                       for row in oracle["alerts"]]

    run = browser["sse"][-1]["run_id"]
    batch, = audit_batches(sheets, steps, calls, run, evidence_only_first=False)
    dispatch, _ = next((model, action) for model, action in action_records(calls, "delegate_tasks")
                       if action["id"] == batch["sdk_tool_call_id"])
    seen, = [json.loads(row["content"]) for row in dispatch["body"]["messages"]
             if row.get("tool_call_id") == discovery["tool_call_id"]]
    assert seen == initial
    marker, = [index for index, row in enumerate(steps) if (row["stage"], row["action"]) ==
               ("fixed_fixture", "plan_transferred") and row["evidence"] == transfer]
    created = [index for index, row in enumerate(steps) if (row["stage"], row["action"]) == ("worker", "created")]
    assert steps.index(discovery) < batch["trace_index"] < marker < min(created)
    assert call["start_ns"] < dispatch["start_ns"] < transfer["committed_ns"]
    assert transfer["dispatch_id"] == batch["dispatch_id"]
    assert transfer["update_params"] == [oracle["transfer"][key] for key in ("to_owner", "plan_id", "from_owner")]
    assert all(row["start_ns"] > transfer["committed_ns"] for row in calls if row["role"] == "team_worker")

    attempts = [(worker, action, json.loads(action["function"]["arguments"]))
                for worker, action in action_records(calls, METRICS)]
    plan_attempts = [(worker, action, args) for worker, action, args in attempts if args["plan_id"] == plan]
    assert len(plan_attempts) == 2
    assert {(args["start_date"], args["end_date"]) for _, _, args in plan_attempts} == {
        (window["start_date"], window["end_date"]) for window in oracle["dates"].values()}
    blocked = [step for step in steps if step["stage"] == "tool_result" and step["action"] == METRICS]
    assert len(blocked) == len(attempts) == 4
    assert all(step["error_code"] == "alert_review_scope_incomplete" for step in blocked)
    assert {row["tool_call_id"] for row in blocked} == {action["id"] for _, action, _ in attempts}
    assert not any(worker["task_id"] == "review" for worker in calls)
    assert [row["tool"] for row in reads] == [ALERT]
    assert not any(row["status"] == "succeeded" and row["source"] in (METRICS, RULE, DETAIL) for row in receipts)
    assert len(selects) == len(oracle["candidate_plan_ids"])
    assert all(row["runtime_call_id"] == discovery["runtime_call_id"] and row["ns"] < transfer["committed_ns"]
               and "SELECT metric_date FROM plan_daily_metrics" in row["sql"] for row in selects)
    assert not browser["scoped"]["proposals"] and browser["before"]["proposals"] == browser["after"]["proposals"]

    ui = browser["refreshed"]
    final = ui["basis"]["alert_review"]
    assert final["status"] == "incomplete" and final["review_scope"] is None
    assert final["candidate_plan_ids"] == [101]
    assert not ui["basis"]["details"] and not ui["basis"]["metrics"]
    matches = transfer["current_alerts"]
    assert final["associations"] == [{key: row[key] for key in ("alert_id", "created_at", "plan_id")}
                                     for row in matches if not row["gap"]]
    assert [row["alert_id"] for row in final["association_gaps"]] == [4103, 4102]
    assert final["association_gaps"] == [{"alert_id": row["alert_id"], "created_at": row["created_at"],
                                          "error_code": row["gap"]} for row in matches if row["gap"]]
    assert all(row["error_code"] == "alert_plan_unverified" for row in final["association_gaps"])
    assert all(f"告警 #{item['alert_id']}，计划关联无法核验" in ui["answer"] for item in final["association_gaps"])
    assert "暂缓出具复核清单" in ui["answer"] and "#103" not in ui["answer"] and "东港搜索" not in ui["answer"]
    return {"path": "transfer_after_discovery_before_worker", "blocked_attempts": blocked,
            "initial_alert_receipt": initial, "final_alert_receipt": final, "transfer": transfer,
            "accepted_batches": [batch], "transferred_plan_metric_bottom_reads": 0,
            "transferred_plan_detail_bottom_reads": 0, "criteo_selects_after_transfer": 0}


def audit(output, selected=None):
    report, frozen = (read(output / file) for file in ("report.json", "frozen.json"))
    trace_file = output / "trace_log_file.jsonl"
    traces = {row["trace_id"]: row["steps"] for row in map(
        json.loads, trace_file.read_text(encoding="utf-8").splitlines() if trace_file.exists() else []
    )}
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
        calls, reads, selects, receipts, sheets = (read(folder / file) for file in
            ("sdk.json", "reads.json", "criteo-selects.json", "receipts.json", "accepted-task-sheets.json"))
        if report["mode"] == "live":
            assert calls == [row for row in sdk if row["case"] == name]
        receipts = [row for row in receipts if row["accepted"]]
        browser = read(folder / "browser.json") if (folder / "browser.json").exists() else {}
        terminal = (browser.get("sse") or [{}])[-1]
        steps = traces.get(terminal.get("trace_id"), [])
        usage = Counter()
        for row in calls:
            usage.update({key: value for key, value in (row.get("response", {}).get("body", {}).get("usage") or {}).items()
                          if type(value) is int})
        scores[name] = score = {
            "result": "fail", "run_id": terminal.get("run_id"), "trace_id": terminal.get("trace_id"),
            "conversation_id": terminal.get("conversation_id"), "turn_ms": browser.get("turn_ms"),
            "sdk_attempts": len(calls) if report["mode"] == "live" else 0,
            "sdk_retries": sum(row.get("sdk_retry", 0) > 0 for row in calls), "usage": dict(usage),
            "bottom_reads": dict(Counter(row["tool"] for row in reads)),
            "criteo_selects": len(selects),
            "out_of_scope_bottom_reads": sum(row["arguments"].get("plan_id") not in
                                             frozen["cases"][name]["oracle"]["candidate_plan_ids"]
                                             for row in reads if "plan_id" in row["arguments"]),
            "detail_receipts": sum(row["source"] == DETAIL and row["status"] == "succeeded" for row in receipts),
            "proposals": len(browser.get("scoped", {}).get("proposals", [])),
            "execution_receipts": len(browser.get("scoped", {}).get("receipts", [])),
        }
        try:
            assert report["cases"][name]["browser"] == "pass"
            transfer = read(folder / "transfer.json") if name == TRANSFER_DURING else None
            proof = check_case(name, frozen["cases"][name], calls, reads, receipts, sheets, selects, browser, steps, transfer)
            score.update(result="pass", path=proof["path"], blocked_attempts=len(proof["blocked_attempts"]))
            if "model_stop" in proof:
                score["unstructured_tool_text"] = proof["model_stop"]["unstructured_tool_text"]
            write(folder / "path.json", proof)
        except (AssertionError, KeyError, TypeError, ValueError, StopIteration):
            write(folder / "audit-deviation.json", {"error": format_exc()})
    write(output / "audit.json", {"mode": report["mode"], "scores": scores})
    return all(row["result"] == "pass" for row in scores.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    raise SystemExit(int(not audit(parser.parse_args().output.resolve())))
