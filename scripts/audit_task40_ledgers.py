"""Independently join archived SDK SQLite rows to actual TASK-040 reads and commits."""

import argparse
from contextlib import closing
from hashlib import file_digest
import json
from pathlib import Path
import sqlite3


DETAIL = "get_plan_detail"
PLAN_IDS = list(range(101, 107))
COMMIT = """
    SELECT r.status, r.trace_id, r.conversation_id, e.content
    FROM chat_group_runs r JOIN chat_group_entries e ON e.run_id = r.run_id
    WHERE r.run_id = ? AND e.kind = 'assistant' ORDER BY e.id
"""


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def connect(path):
    return sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)


def one(rows):
    row, = rows
    return row


def sdk_attempts(ledger, case):
    with closing(connect(ledger)) as conn:
        rows = conn.execute(
            "SELECT id, run, request, response FROM sdk WHERE sample = ? ORDER BY id", (case,),
        ).fetchall()
    assert len({row[1] for row in rows}) == 1
    attempts = []
    for number, _, raw_request, raw_response in rows:
        request, response = json.loads(raw_request), json.loads(raw_response)
        assert request["body"]["model"] == "glm-4.5-air" and request["sdk_retry"] == 0
        assert response["status"] == 200 and request["start_ns"] <= response["end_ns"]
        message = one(response["body"]["choices"])["message"]
        attempts.append({"number": number, **request, "ended_ns": response["end_ns"], "message": message})
    return attempts


def actions(attempt):
    return attempt["message"].get("tool_calls") or []


def read_action(call, attempt, reads, steps):
    assert call["function"]["name"] == DETAIL
    read = one(row for row in reads if row["tool_call_id"] == call["id"]
               and row["parent_call_id"] == attempt["worker_id"])
    args = json.loads(call["function"]["arguments"])
    assert read["trace_id"] == attempt["trace_id"] and read["role"] == attempt["role"]
    assert read["tool"] == DETAIL and read["arguments"] == {**args, "user_id": 17}
    assert read["result"]["id"] == args["plan_id"] and read["result"]["user_id"] == 17
    assert attempt["ended_ns"] <= read["start_ns"] <= read["end_ns"]
    accepted = one(row for row in steps if row["stage"] == "receipt" and row["action"] == "accepted"
                   and row["runtime_call_id"] == read["runtime_call_id"])
    assert accepted.get("parent_call_id") == attempt["worker_id"]
    return {**read, "artifact_id": accepted["evidence"]["artifact_id"]}


def tool_reply(attempt, call_id):
    return json.loads(one(row["content"] for row in attempt["body"]["messages"]
                          if row["role"] == "tool" and row["tool_call_id"] == call_id))


def audit(directory, case, *, delegated=False):
    ledger = directory / "glm-sdk-ledger.sqlite3"
    case_path = directory / case
    attempts = sdk_attempts(ledger, case)
    trace = load(case_path / "trace.json")
    steps = trace["steps"]
    browser = load(case_path / "browser.json")
    reads = sorted(load(case_path / "reads.json"), key=lambda row: row["start_ns"])
    sse = browser["sse"]
    assert [row["type"] for row in sse] == ["thinking", "text", "done"]
    done = sse[-1]
    answer = sse[1]["content"]
    trace_id = done["trace_id"]
    assert trace["trace_id"] == trace_id and trace["status"] == "completed"
    assert browser["initial"] == browser["refreshed"]
    assert browser["refreshed"]["status"] == "回复已保存"
    assert one(row for row in steps if row["stage"] == "persistence" and row["action"] == "save_reply")["status"] == "ok"
    with closing(connect(directory / "isolated.sqlite3")) as conn:
        committed = conn.execute(COMMIT, (done["run_id"],)).fetchall()
        assert committed == [("completed", trace_id, done["conversation_id"], answer)]
        plans = dict(conn.execute("SELECT id, plan_name FROM ad_plans WHERE user_id = 17"))
    for read in reads:
        assert read["result"]["plan_name"] == plans[read["arguments"]["plan_id"]]
        assert read["result"]["plan_name"] in answer
    for attempt in attempts:
        assert attempt["trace_id"] == trace_id
        model_step = one(step for step in steps if step["stage"] == "model_call"
                         and step["runtime_call_id"] == attempt["model_call_id"])
        assert model_step.get("role", model_step["action"]) == attempt["role"]
        assert model_step.get("parent_call_id") == attempt["worker_id"]
    main_decisions = [attempt for attempt in attempts if attempt["step"] == "main" and actions(attempt)]
    stop = one(row for row in steps if row["stage"] == "react_decision" and row["action"] == "stop")
    assert stop["budget"]["model"] == 12 - len(attempts) and stop["budget"]["tool"] == 12 - len(reads)
    assert not any(step["stage"] in {"tool_reuse", "duplicate_dispatch", "duplicate_read"} for step in steps)
    result = {"trace_id": trace_id, "run_id": done["run_id"], "sdk_attempts": len(attempts),
              "main_decisions": len(main_decisions), "read_order": [read["arguments"]["plan_id"] for read in reads],
              "stop_reason": stop["evidence"]["reason"], "remaining": stop["budget"], "commit_verified": True,
              "duplicate_actions": 0}
    if delegated:
        assert len(main_decisions) == 2 and len(reads) == 3
        first, dispatch = main_decisions
        first_call = one(actions(first))
        first_read = read_action(first_call, first, reads, steps)
        assert first_read["arguments"]["plan_id"] == 101 and first_read["result"]["status"] == "active"
        source = tool_reply(dispatch, first_call["id"])
        assert source["success"] and source["artifact_id"] == first_read["artifact_id"]
        assert source["runtime_call_id"] == first_read["runtime_call_id"] and first_read["end_ns"] <= dispatch["start_ns"]
        dispatch_call = one(actions(dispatch))
        assert dispatch_call["function"]["name"] == "delegate_tasks"
        tasks = json.loads(dispatch_call["function"]["arguments"])["tasks"]
        assert len(tasks) == 2
        final = attempts[-1]
        assert final["step"] == "main" and final["message"]["content"] == answer
        returned = tool_reply(final, dispatch_call["id"])
        assert returned["status"] == "completed" and len(returned["evidence"]) == 2
        handoffs = []
        for task in tasks:
            assert task["direct_refs"] == [source["artifact_id"]] and task["capabilities"] == [DETAIL]
            worker = one(row for row in attempts if row["task_id"] == task["task_id"] and actions(row))
            context = json.loads(one(msg["content"] for msg in worker["body"]["messages"] if msg["role"] == "user"))
            assert dispatch["ended_ns"] <= worker["start_ns"]
            assert context["task"]["task_id"] == task["task_id"]
            assert context["task"]["read_scope"] == task["read_scope"]
            supplied = one(context["direct_inputs"])
            assert supplied["artifact_id"] == source["artifact_id"] and supplied["tool_call_id"] == source["runtime_call_id"]
            assert supplied["result"] == source["result"]
            read = read_action(one(actions(worker)), worker, reads, steps)
            assert task["read_scope"] == [{"tool": DETAIL, "arguments": {"plan_id": read["arguments"]["plan_id"]}}]
            assert read["end_ns"] <= final["start_ns"]
            receipt = one(row for row in returned["evidence"] if row["task_id"] == task["task_id"])
            assert receipt["artifact_id"] == read["artifact_id"] and receipt["runtime_call_id"] == read["runtime_call_id"]
            edge = one(row["evidence"] for row in steps if row["stage"] == "dependency_progress"
                       and row["detail"] == task["task_id"])
            assert edge["input_artifact_ids"] == [source["artifact_id"]] and edge["output_artifact_ids"] == [read["artifact_id"]]
            handoffs.append({"task_id": task["task_id"], "sdk_row": worker["number"], "worker_id": worker["worker_id"],
                             "plan_id": read["arguments"]["plan_id"], **edge})
        assert {row["plan_id"] for row in handoffs} == {102, 103}
        assert set(stop["evidence"]["artifact_ids"]) == {source["artifact_id"],
                                                       *(row["output_artifact_ids"][0] for row in handoffs)}
        result.update(dispatch_sdk_row=dispatch["number"], handoffs=handoffs)
    else:
        assert result["read_order"] == PLAN_IDS and len(main_decisions) == len(reads) == 6
        for index, attempt in enumerate(main_decisions):
            call = one(actions(attempt))
            read = read_action(call, attempt, reads, steps)
            assert read["arguments"]["plan_id"] == PLAN_IDS[index]
            if index < 5:
                following = main_decisions[index + 1]
                receipt = tool_reply(following, call["id"])
                assert receipt["artifact_id"] == read["artifact_id"] and receipt["runtime_call_id"] == read["runtime_call_id"]
                assert receipt["result"]["id"] == PLAN_IDS[index] and read["end_ns"] <= following["start_ns"]
        assert read["artifact_id"] in stop["evidence"]["artifact_ids"]
        result.update(sixth_sdk_row=main_decisions[-1]["number"], sixth_tool_call_id=call["id"],
                      sixth_read_runtime_id=read["runtime_call_id"], sixth_artifact_id=read["artifact_id"])
    with ledger.open("rb") as stream:
        result["ledger_sha256"] = file_digest(stream, "sha256").hexdigest()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    logs = Path(__file__).resolve().parents[1] / "logs"
    report = {
        "main_card_six": audit(logs / "task40/live-delivery", "six"),
        "loop_card_ordered": audit(logs / "task40-loop/live-ordered-final", "ordered"),
        "loop_card_handoffs": audit(logs / "task40-loop/live-final", "evidence", delegated=True),
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
