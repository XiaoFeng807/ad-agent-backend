"""Correlate TASK-027-T1 browser, database, model, tool and trace records."""

import json

from scripts.verify_task27_t1 import A, B, C, METRICS, OTHER_MARKER, OWNER, PLAN, WINDOW, receipts


def only(rows):
    row, = rows
    return row


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify(directory, sql, model, source, report):
    browser = load(directory / "browser.json")
    traces = {trace["trace_id"]: trace for trace in map(
        json.loads, (directory / "traces.jsonl").read_text(encoding="utf-8").splitlines())}
    groups = browser["groups"]
    assert {row["conversation_id"] for row in sql["groups"] if row["user_id"] == OWNER} == {
        *groups.values(), "legacy-17"}
    assert len(sql["runs"]) == 6
    assert set(browser["replay"]) == set(groups)
    assert source.snapshots["first"]["bindings"] == source.snapshots["second"]["bindings"]
    first_source, second_source = (source.snapshots[label]["metrics"] for label in ("first", "second"))
    assert [row["clicks"] for row in first_source] == [120, 30]
    assert [row["clicks"] for row in second_source] == [30, 120]
    assert [{key: val for key, val in row.items() if key != "clicks"} for row in first_source] == [
        {key: val for key, val in row.items() if key != "clicks"} for row in second_source]

    b_contexts = {}
    for label, sample in browser["cases"].items():
        sse = sample["sse"]
        run_id, trace_id = sse[0]["run_id"], sse[0]["trace_id"]
        group_id = groups["other" if label == "seed" else "target"]
        run = only(row for row in sql["runs"] if row["run_id"] == run_id)
        assert (run["conversation_id"], run["trace_id"], run["status"]) == (group_id, trace_id, "completed")
        assert {"run_id": run_id, "trace_id": trace_id, "status": "completed"} in sample["history"]["runs"]
        db_rows = [row for row in sql["entries"] if row["run_id"] == run_id]
        api_rows = [row for row in sample["history"]["entries"] if row["run_id"] == run_id]
        assert [row["entry_id"] for row in api_rows] == [f"entry:{row['id']}" for row in db_rows]
        for api, db in zip(api_rows, db_rows):
            assert (api["trace_id"], api["kind"], api["content"]) == (trace_id, db["kind"], db["content"])
            assert api.get("event") == (json.loads(db["details"]) if db["details"] else None)
        assert db_rows[0]["kind"] == "user" and db_rows[0]["content"] == sample["request"]["message"]
        assert db_rows[-1]["kind"] == "assistant" and db_rows[-1]["content"] == sse[1]["content"]
        assert traces[trace_id]["status"] == "completed"
        steps = traces[trace_id]["steps"]
        events = [row["event"] for row in api_rows if row["kind"] == "worker"]
        calls = [row for row in model.calls if row["case"] == label]
        worker = [row for row in calls if row["phase"] == "worker"]
        main = only(row for row in calls if row["phase"] == "main"
                    and any(item["role"] == "tool" and item["tool_call_id"] == (
                        "local-B" if label == "foreign" else "batch") for item in row["messages"]))
        handoffs = receipts(main["messages"])
        batch = handoffs["local-B" if label == "foreign" else "batch"]
        all_evidence = batch["evidence"] + handoffs.get("local-A", {}).get("evidence", [])
        actual = [row for row in model.reads if row["case"] == label]
        assert all(row["arguments"]["user_id"] == OWNER for row in actual)
        worker_ids = verify_events(events, steps, all_evidence, actual)
        for name, worker_id in worker_ids.items():
            count = sum(row["task_id"] == name for row in worker)
            assert count == 2
            assert sum(step["stage"] == "model_call" and step.get("parent_call_id") == worker_id
                       for step in steps) == count
        assert set(worker_ids) == {row["task_id"] for row in worker}

        summary = {"scenario": label, "status": "passed", "conversation_id": group_id,
                   "run_id": run_id, "trace_id": trace_id, "events": len(events),
                   "worker_model_calls": len(worker), "actual_reads": len(actual), "states": batch["states"]}
        metric_reads = [row for row in actual if row["tool"] == METRICS]
        detail, = [row for row in actual if row["tool"] == PLAN]
        if label == "seed":
            assert batch["states"] == {A: "succeeded"} and not metric_reads
            item, = batch["evidence"]
            assert item["result"]["plan_name"] == OTHER_MARKER and item["artifact_id"] == model.foreign_id
            assert detail["arguments"] == {"plan_id": 103, "user_id": OWNER}
            summary["foreign_artifact_id"] = item["artifact_id"]
        elif label == "missing":
            assert batch["status"] == "completed_with_failures"
            assert batch["states"] == {A: "failed", B: "blocked", C: "succeeded"}
            assert set(worker_ids) == {A, C}
            assert [row["arguments"] for row in metric_reads] == [
                {"plan_id": plan_id, **WINDOW, "user_id": OWNER} for plan_id in (101, 102)]
            assert metric_reads[1]["result"] == {"success": False, "error_code": "plan_history_not_mapped"}
            assert detail["arguments"] == {"plan_id": 101, "user_id": OWNER}
            gaps = {(row["task_id"], row["source"]): row for row in batch["gaps"]}
            gap = gaps[(A, METRICS)]
            assert (gap["error_code"], gap["gap_type"]) == ("plan_history_not_mapped", "permanent")
            assert gaps[(B, "scheduler")]["result"] == {"dependencies": [A]}
            assert gaps[(B, "scheduler")]["error_code"] == "dependency_failed"
            assert only(event for event in events if event["task_id"] == B)["error_code"] == "dependency_failed"
            assert "计划 #102 缺少历史指标映射" in sse[1]["content"] and "依赖详情未完成" in sse[1]["content"]
            assert "独立查询已取得计划 #101 的详情" in sse[1]["content"]
            summary.update(missing=gap, blocked_model_calls=0, blocked_tool_calls=0, independent=detail["arguments"])
        else:
            expected = 101 if label == "first" else 102
            assert batch["status"] == "completed" and not batch["gaps"]
            assert batch["states"] == ({B: "succeeded"} if label == "foreign" else {A: "succeeded", B: "succeeded"})
            a_batch = handoffs["local-A"] if label == "foreign" else batch
            metrics = [item for item in a_batch["evidence"] if item["task_id"] == A and item["source"] == METRICS]
            assert len(metrics) == 2
            assert [row["arguments"] for row in metric_reads] == [
                {"plan_id": plan_id, **WINDOW, "user_id": OWNER}
                for plan_id in ((101, 102, 102) if label == "retry" else (101, 102))]
            for item, origin in zip(metrics, source.snapshots[label]["metrics"]):
                result = item["result"]
                assert (str(result["plan_id"]), result["impressions"], result["clicks"]) == (
                    origin["source_plan_id"], origin["impressions"], origin["clicks"])
                assert result["coverage"]["complete"]
            assert detail["arguments"] == {"plan_id": expected, "user_id": OWNER}
            assert detail["result"]["id"] == expected
            b_calls = [row for row in worker if row["task_id"] == B]
            b_contexts[label] = verify_context(b_calls, a_batch, metrics, expected, exact=label == "foreign")
            assert f"计划 #{expected}" in sse[1]["content"]
            assert next(i for i, step in enumerate(steps) if step["stage"] == "receipt"
                        and step["action"] == "accepted" and step.get("runtime_call_id") == worker_ids[A]) < next(
                i for i, step in enumerate(steps) if step["stage"] == "worker"
                and step["action"] == "created" and step.get("runtime_call_id") == worker_ids[B])
            summary.update(detail_query=detail["arguments"], upstream_artifacts=[item["artifact_id"] for item in metrics])
            if label in {"first", "second"}:
                timeline = [row["event"] for row in model.timeline if row["case"] == label]
                assert timeline.index("release") < timeline.index(f"returned:{METRICS}:101") < timeline.index(f"model:{B}")
                assert {"run_id": run_id, "trace_id": trace_id, "status": "running"} in sample["beforeRelease"]["runs"]
                assert sample["barrier"]["workers"] == [A]
            if label == "retry":
                assert metric_reads[1]["exception"] == "TimeoutError"
                assert all("result" in row for row in (metric_reads[0], metric_reads[2]))
                temporary = only(row for row in events if row.get("error_code") == "temporary_tool_failure")
                first = only(step for step in steps if step["stage"] == "tool_result"
                             and step.get("runtime_call_id") == temporary["runtime_call_id"])
                retried = only(step for step in steps if step["stage"] == "tool_result"
                               and step.get("runtime_call_id") == metrics[1]["runtime_call_id"])
                assert retried["retry_of"] == first["runtime_call_id"] and retried["runtime_call_id"] != first["runtime_call_id"]
                assert retried["tool_call_id"] == first["tool_call_id"] and retried["parent_call_id"] == first["parent_call_id"]
                assert retried["evidence"]["arguments"] == first["evidence"]["arguments"] == metrics[1]["arguments"]
                assert temporary["artifact_id"] not in json.dumps(b_calls, ensure_ascii=False)
                summary["retry"] = {"plan_id": 102, "original_runtime_call_id": first["runtime_call_id"],
                                    "retry_runtime_call_id": retried["runtime_call_id"],
                                    "tool_call_id": retried["tool_call_id"], "original_args": first["evidence"]["arguments"],
                                    "accepted_artifact_id": metrics[1]["artifact_id"]}
            if label == "foreign":
                assert handoffs["foreign-B"] == {"status": "rejected", "error_code": "invalid_input_reference"}
                rejected = only(step for step in steps if step["stage"] == "tool_result"
                                and step.get("tool_call_id") == "foreign-B")
                assert rejected["error_code"] == "invalid_input_reference"
                proposal = only(call for row in main["messages"] for call in row.get("tool_calls", [])
                                if call["id"] == "foreign-B")
                foreign_task, = json.loads(proposal["function"]["arguments"])["tasks"]
                assert foreign_task["artifact_refs"] == [{"task_id": A, "artifact_id": model.foreign_id}]
                assert a_batch["states"] == {A: "succeeded"}
                assert all(marker not in json.dumps(b_calls + events, ensure_ascii=False)
                           for marker in (model.foreign_id, OTHER_MARKER))
                assert all(step.get("parent_call_id") != rejected["runtime_call_id"] for step in steps)
                summary["rejected_artifact_id"] = model.foreign_id
        report["scenarios"].append(summary)

    first, second = (b_contexts[label] for label in ("first", "second"))
    assert first["task"] == second["task"]
    initial_a = [next(row for row in model.calls if row["case"] == label and row["task_id"] == A)
                 for label in ("first", "second")]
    assert initial_a[0]["messages"] == initial_a[1]["messages"]
    assert initial_a[0]["tools"] == initial_a[1]["tools"]
    assert len({item["run_id"] for item in report["scenarios"]}) == 6
    report["chromium"] = browser["chromium"]
    report["totals"] = {"executed": 6, "passed": 6, "failed": 0, "skipped": 0}


def verify_events(events, steps, evidence, actual):
    worker_ids = {}
    linked = []
    for event in events:
        kind = event["kind"]
        name = event["task_id"]
        if kind in {"dispatched", "reclaimed"}:
            step = only(step for step in steps if step["stage"] == "worker"
                        and step["action"] == ("created" if kind == "dispatched" else "reclaimed")
                        and step.get("runtime_call_id") == event["worker_id"])
            assert (step["detail"], step["parent_call_id"]) == (name, event["dispatch_id"])
            if kind == "dispatched":
                assert name not in worker_ids
                worker_ids[name] = event["worker_id"]
            else:
                assert worker_ids[name] == event["worker_id"]
        elif event.get("source"):
            assert kind in {"receipt", "failed"}
            assert worker_ids[name] == event["worker_id"]
            result = only(step for step in steps if step["stage"] == "tool_result"
                          and step.get("runtime_call_id") == event["runtime_call_id"])
            assert (result["action"], result.get("error_code"), result["parent_call_id"]) == (
                event["source"], event["error_code"], event["worker_id"])
            assert result["status"] == ("ok" if kind == "receipt" else "error")
            admitted = only(step for step in steps if step["stage"] == "receipt"
                            and step.get("runtime_call_id") == event["runtime_call_id"])
            assert (admitted["action"], admitted["detail"]) == ("accepted", name)
            linked.append(result)
            if kind == "receipt":
                row = only(row for row in evidence if row["artifact_id"] == event["artifact_id"])
                assert (row["runtime_call_id"], row["task_id"], row["source"]) == (
                    event["runtime_call_id"], name, event["source"])
        elif "worker_id" in event:
            assert kind == "failed" and worker_ids[name] == event["worker_id"]
            row = only(step for step in steps if step["stage"] == "receipt"
                       and step.get("runtime_call_id") == event["worker_id"])
            assert row["error_code"] == event["error_code"]
        else:
            assert kind == "failed" and event["error_code"] == "dependency_failed"
            assert name not in worker_ids
            assert any(step["stage"] == "batch" and step["action"] == "completed_with_failures"
                       and step["parent_call_id"] == event["dispatch_id"] for step in steps)
    for name in worker_ids:
        own = [event["kind"] for event in events if event["task_id"] == name]
        assert (own[0], own[-1]) == ("dispatched", "reclaimed") and own.count("reclaimed") == 1
    assert len(linked) == len(actual) == sum(step["stage"] == "tool_result" and step["role"] == "team_worker"
                                            for step in steps)
    assert len(evidence) == sum(event["kind"] == "receipt" for event in events)
    return worker_ids


def verify_context(calls, upstream, metrics, selected, *, exact):
    initial, final = calls
    assert len(initial["messages"]) == 2 and initial["messages"][0]["role"] == "system"
    assert initial["messages"][1]["role"] == "user"
    context = json.loads(initial["messages"][1]["content"])
    assert set(context) == {"task", "inputs"} and set(context["inputs"]) == {A}
    task = context["task"]
    assert task["dependencies"] == [A] and not task["direct_refs"]
    if exact:
        assert not task["input_refs"]
        assert task["artifact_refs"] == [{"task_id": A, "artifact_id": item["artifact_id"]} for item in metrics]
    else:
        assert task["input_refs"] == [A] and not task["artifact_refs"]
    assert task["capabilities"] == [PLAN]
    for request in calls:
        assert request["response_format"] == {"type": "json_object"}
        assert [tool["function"]["name"] for tool in request["tools"]] == [PLAN]
        assert "user_id" not in request["tools"][0]["function"]["parameters"]["properties"]
        assert request["messages"][:2] == initial["messages"]
    expected = [
        {"artifact_id": item["artifact_id"], "source": METRICS, "status": "succeeded",
         "tool_call_id": item["runtime_call_id"], "arguments": item["arguments"], "result": item["result"]}
        for item in metrics
    ]
    if not exact:
        summary = only(row for row in upstream["summaries"] if row["task_id"] == A)
        expected.append({"artifact_id": summary["artifact_id"], "source": "worker_model", "status": "succeeded",
                         "tool_call_id": summary["runtime_call_id"],
                         "result": {"status": summary["status"], "output": summary["output"]}})
    assert context["inputs"][A] == expected
    assert only(receipts(final["messages"]).values())["arguments"] == {"plan_id": selected}
    return context
