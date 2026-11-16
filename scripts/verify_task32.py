"""Verify live GLM planning, progress and evidence in the plan replacement browser case."""

import json

from scripts.verify_task28 import ChangeCase, DATES, DETAIL, MESSAGE, METRICS, RULE, main, write


CASE = ChangeCase(
    request="保留日期和规则，把 #102 换成 #103",
    dates={version: DATES["old"] for version in ("old", "new")},
    bindings={101: "27589150", 102: "26998434", 103: "13289562"},
    targets={"old": 102, "new": 103},
    plan_change=True,
    message=MESSAGE + " 告警条件满足后，先在同一份任务单安排取证和独立复核，再开始取证。"
                      "复核任务依赖全部取证任务，等待证据齐备后才执行。",
    capture_wait=True,
)


def audit_progress(sample, sync, calls, receipts, evidence, sql, folder):
    steps = {}
    output = {"checkpoints": steps, "task_sheets": []}
    try:
        check_progress(sample, sync, calls, receipts, evidence, sql, output)
    finally:
        write(folder / "checkpoints.json", output)


def check_progress(sample, sync, calls, receipts, evidence, sql, output):
    from backend.agent.multi_window_review import parse_review_goal

    steps = output["checkpoints"]
    old = sample["old_sse"][-1]
    new = sample["new_sse"][-1]
    history = sample["history"]["entries"]
    sheets = output["task_sheets"]
    for terminal in (old, new):
        trace_id, run_id = terminal["trace_id"], terminal["run_id"]
        trace = evidence["traces"][trace_id]["steps"]
        entries = [row["event"] for row in history if row["kind"] == "worker" and row["run_id"] == run_id]
        for sheet in [row for row in sync.task_sheets if row["trace_id"] == trace_id]:
            dispatch_id = sheet["dispatch_id"]
            started, = [step for step in trace if step["stage"] == "tool_call"
                        and step.get("runtime_call_id") == dispatch_id and step["action"] == "delegate_tasks"]
            sdk_call, action = next((call, action) for call in calls if call["trace_id"] == trace_id
                                    for action in actions(call) if action["id"] == started["tool_call_id"])
            proposed = json.loads(action["function"]["arguments"])
            queued = [row for row in entries if row.get("dispatch_id") == dispatch_id
                      and row["kind"] in ("queued", "waiting")]
            labels = {task["task_id"]: row["task_label"] for task, row in zip(sheet["tasks"], queued, strict=True)}
            labels.update({key: value for prior in sheets if prior["run_id"] == run_id
                           for key, value in prior["labels"].items()})
            assert [task["task_id"] for task in proposed["tasks"]] == [task["task_id"] for task in sheet["tasks"]]
            for task, row in zip(sheet["tasks"], queued, strict=True):
                assert task["capabilities"] == row["capabilities"]
                assert set(row.get("dependencies", [])) <= {labels[key] for key in task["dependencies"]}
            for row in entries:
                if row["kind"] == "dispatched" and row.get("dispatch_id") == dispatch_id:
                    created, = [step for step in trace if step["stage"] == "worker" and step["action"] == "created"
                                and step["runtime_call_id"] == row["worker_id"]]
                    assert created["parent_call_id"] == dispatch_id and labels[created["detail"]] == row["task_label"]
            sheets.append({**sheet, "run_id": run_id, "labels": labels, "sdk_attempt": sdk_call.get("attempt"),
                           "sdk_tool_call_id": action["id"], "proposed": proposed})
    steps["1_dispatch"] = "pass"

    waiting = sample["waiting"]
    held = sample["held"]
    read, = [row for row in evidence["reads"] if row["runtime_call_id"] == held["runtime_call_id"]]
    assert read["trace_id"] == old["trace_id"] and read["parent_call_id"] == held["parent_call_id"]
    start, = [row for row in waiting["entries"] if row.get("runtime_call_id") == held["runtime_call_id"]]
    assert start["kind"] == "tool_started" and start["worker_id"] == read["parent_call_id"]
    assert all(str(held[key]) in start["text"] for key in ("plan_id", "start", "end"))
    running, = [row for row in waiting["tasks"] if row["task_label"] == start["task_label"]]
    assert running["state"] == "running" and "正在查 #102" in running["text"]
    assert compact_window(held["start"], held["end"]) in running["text"]
    review = [row for row in waiting["tasks"] if row["state"] == "waiting"]
    if not review:
        steps["2_wait"] = "uncovered"
    else:
        reviewer, = review
        sheet, task = next((sheet, task) for sheet in sheets if sheet["run_id"] == old["run_id"]
                           for task in sheet["tasks"] if DETAIL in task["capabilities"])
        assert reviewer["task_label"] == sheet["labels"][task["task_id"]]
        event, = [row for row in waiting["entries"] if row["task_label"] == reviewer["task_label"]
                  and row["kind"] == "waiting"]
        assert event["dispatch_id"] == sheet["dispatch_id"]
        dependency_labels = [sheet["labels"][dep] for dep in task["dependencies"]]
        assert start["task_label"] in dependency_labels
        assert not any(row["task_label"] == reviewer["task_label"] and row["kind"] in {"dispatched", "review_inputs"}
                       for row in waiting["entries"])
        assert not any(row["trace_id"] == old["trace_id"] and row.get("task_id") == task["task_id"] for row in calls)
        output["wait"] = {"held": held, "dependency_labels": dependency_labels,
                          "visible_receipts": [row["artifact_ids"] for row in waiting["entries"] if row["kind"] == "receipt"],
                          "review_summary": reviewer["text"]}
        check_wait(sample, history, dependency_labels, reviewer, receipts)
        steps["2_wait"] = "pass"

    preview = sample["preview_response"]["data"]
    change, = [json.loads(row["details"]) for row in evidence["sql_entries"] if row["kind"] == "change"]
    stored, = sql("SELECT * FROM chat_group_date_proposals WHERE proposal_id=?", (preview["proposal_id"],), rows=True)
    expected = {key: preview[key] for key in ("remove_plan_id", "add_plan_id")}
    assert change["proposal_trace_id"] == stored["trace_id"] == preview["trace_id"]
    assert change["proposal_id"] == preview["proposal_id"] and stored["replacement_run_id"] == new["run_id"]
    assert json.loads(stored["payload"]) == expected
    interpreters = [call for call in calls if call["trace_id"] == preview["trace_id"] and
                    call["step"] == "interpret_group_change" and call.get("response", {}).get("status") == 200]
    if interpreters:
        interpreter, = interpreters
        assert json.loads(interpreter["response"]["body"]["choices"][0]["message"]["content"]) == expected
    elif any("response" in call for call in calls):
        raise AssertionError("missing successful GLM plan change interpretation")
    assert parse_review_goal(sample["old_request"]["message"])[1].independent
    steps["3_replace"] = "pass"

    basis = sample["refreshed"]["basis"]
    chain = {row["artifact_id"]: row for row in evidence["handoff_chain"]}
    for metric in basis["metrics"]:
        row = chain[metric["artifact_id"]]
        assert row["source"] == METRICS
        assert all(metric[key] == row["arguments"][key] for key in ("plan_id", "start_date", "end_date"))
        assert all(metric[key] == row["result"][key] for key in ("impressions", "clicks", "ctr_pct"))
    rule = basis["rule"]
    row = chain[rule["artifact_id"]]
    assert row["source"] == RULE
    hit, = [hit for hit in row["result"]["evidence"] if hit["chunk_id"] == rule["chunk_id"]]
    assert all(rule[key] == hit[key] for key in ("version", "source", "excerpt"))
    detail, = basis["details"]
    receipt = evidence["review_detail_receipt"]
    assert detail["runtime_call_id"] == receipt["tool_call_id"]
    assert all(detail[key] == receipt["result"][key] for key in ("id", "plan_name", "status", "daily_budget"))
    steps["4_handoff"] = "pass"

    old_progress, new_progress = (sample["refreshed"][f"{version}_progress"]["entries"] for version in ("old", "new"))
    for key in ("worker_id", "runtime_call_id", "dispatch_id", "model_call_id"):
        old_ids = {row[key] for row in old_progress if key in row}
        assert old_ids.isdisjoint(row[key] for row in new_progress if key in row)
    assert not any(step.get("runtime_call_id") == held["runtime_call_id"]
                   for step in evidence["traces"][new["trace_id"]]["steps"])
    steps["5_refresh"] = "pass"
    assert steps["2_wait"] == "pass", "review had not been scheduled while the old #102 read was held"


def actions(call):
    if "body" in call:
        return [action for choice in call.get("response", {}).get("body", {}).get("choices", [])
                for action in choice["message"].get("tool_calls", [])]
    return call.get("tool_calls", [])


def compact_window(start, end):
    return f"{int(start[5:7])} 月 {int(start[-2:])}–{int(end[-2:])} 日"


def check_wait(sample, history, labels, reviewer, receipts):
    visible = sample["waiting"]["entries"]
    entries = {row["entry_id"]: row["event"] for row in history if row["kind"] == "worker"}
    artifacts = {row["artifact_id"] for row in receipts if row["accepted"]}
    scopes = []
    for label in labels:
        events = [entries[row["entry_id"]] for row in visible if row["task_label"] == label]
        received = {row["runtime_call_id"] for row in events if row["kind"] == "receipt"}
        assert all(row["artifact_id"] in artifacts for row in events if row["kind"] == "receipt")
        if any(row["kind"] == "succeeded" for row in events):
            continue
        active = [row for row in events if row["kind"] == "tool_started" and row["runtime_call_id"] not in received]
        for row in active:
            args = row["arguments"]
            if row["source"] == METRICS:
                scopes.append(f"#{args['plan_id']} " + {"baseline": "基准窗", "current": "当前窗"}[args["window"]])
            elif row["source"] == RULE:
                scopes.append("规则")
        if not active and not received:
            scopes.append(label)
    assert reviewer["text"].endswith(" · 等待 " + "、".join(scopes)), (reviewer, scopes)


if __name__ == "__main__":
    main(CASE, __doc__, audit_browser=audit_progress)
