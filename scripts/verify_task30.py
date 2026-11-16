"""Replay work-group progress, evidence and isolation in Chromium with a fixed model."""

import json
import subprocess
from threading import Event

from scripts.verify_task28 import DATES, DETAIL, MESSAGE, METRICS, RULE, ROOT, STATUS_REQUEST, main, t5, write


def audit_completed(basis, progress, calls, receipts, oracle):
    trace_id = next(row["trace_id"] for row in receipts if row["artifact_id"] == basis["details"][0]["artifact_id"])
    selected = {row["artifact_id"]: row for row in receipts if row["accepted"] and row["trace_id"] == trace_id}
    first, = [call for call in calls if call.get("trace_id") == trace_id and call.get("role") == "team_worker"
              and json.loads(call["messages"][1]["content"])["task"]["task_id"] == "review"]
    inputs = json.loads(first["messages"][1]["content"])["inputs"]
    handed_off = {row["artifact_id"]: row for rows in inputs.values() for row in rows}
    transfer, = [entry for entry in progress["entries"] if entry["kind"] == "review_inputs"]
    assert set(transfer["artifact_ids"]) == set(handed_off)
    assert transfer["model_call_id"] == first["model_call_id"]
    assert transfer["worker_id"] == first["worker_id"]
    for receipt in [row for row in progress["entries"] if row["kind"] == "receipt"]:
        actual, = [selected[artifact] for artifact in receipt["artifact_ids"]]
        start, = [row for row in progress["entries"] if row["kind"] == "tool_started"
                  and row["runtime_call_id"] == actual["tool_call_id"]]
        assert actual["worker_id"] == start["worker_id"] == receipt["worker_id"]
        assert actual["tool_call_id"] == receipt["runtime_call_id"]
        assert int(start["entry_id"].split(":")[1]) < int(receipt["entry_id"].split(":")[1])
    assert {row["artifact_id"] for row in (*basis["metrics"], basis["rule"])} == set(handed_off)
    assert all(entry["run_id"] == basis["run_id"] for entry in progress["entries"])
    for metric in basis["metrics"]:
        receipt = handed_off[metric["artifact_id"]]
        assert receipt["source"] == METRICS
        assert all(metric[key] == receipt["arguments"][key] for key in ("plan_id", "start_date", "end_date"))
        expected = next(row for row in oracle["old"]["metrics"][str(metric["plan_id"])]["windows"]
                        if row["start_date"] == metric["start_date"])
        for key in ("start_date", "end_date", "impressions", "clicks"):
            assert metric[key] == expected[key]
        assert abs(metric["ctr_pct"] - 100 * expected["clicks"] / expected["impressions"]) < 1e-8
        assert all(metric[key] == receipt["result"][key] for key in ("impressions", "clicks", "ctr_pct"))
    rule = basis["rule"]
    receipt = handed_off[rule["artifact_id"]]
    assert receipt["source"] == RULE
    hit, = [hit for hit in receipt["result"]["evidence"] if hit["chunk_id"] == "ctr-review-v1-low-ctr-01"]
    assert all(rule[key] == hit[key] for key in ("version", "source", "chunk_id", "excerpt"))
    detail, = basis["details"]
    receipt = selected[detail["artifact_id"]]
    proof = detail["review_input"]
    assert receipt["source"] == DETAIL and receipt["status"] == "succeeded"
    assert receipt["worker_id"] == proof["worker_id"] == first["worker_id"]
    assert receipt["tool_call_id"] == detail["runtime_call_id"]
    assert proof["model_call_id"] == first["model_call_id"] and set(proof["input_artifact_ids"]) == set(handed_off)
    for key in ("id", "plan_name", "status", "daily_budget"):
        assert detail[key] == receipt["result"][key]


def audit_glm_archive(output):
    archive = {}
    for label, source in (("date", "task28/live-final"), ("plan", "task29/live")):
        evidence = t5.read(ROOT / "logs" / source / "late/evidence.json")
        review = evidence["review_input"]
        inputs = review["context"]["inputs"]
        handoff = [row for rows in inputs.values() for row in rows]
        rule, = [row for row in handoff if row["source"] == RULE]
        metrics = [row for row in handoff if row["source"] == METRICS]
        decision = evidence["decision"]
        assert len(metrics) == 4 and len(decision["detail_artifact_ids"]) == 1
        for metric in metrics:
            scope = metric["arguments"]
            row = next(row for row in decision["rows"] if row["plan_id"] == scope["plan_id"])
            window = next(window for window in row["windows"] if window["start_date"] == scope["start_date"])
            assert metric["artifact_id"] in window["artifact_ids"]
            assert all(metric["result"][key] == window[key] for key in ("impressions", "clicks", "ctr_pct"))
        read = evidence["review_detail_read"]
        assert read["parent_call_id"] == review["worker_id"]
        assert read["arguments"]["plan_id"] in decision["target_plan_ids"]
        hit, = [hit for hit in rule["result"]["evidence"] if hit["chunk_id"] == decision["policy"]["chunk_id"]]
        archive[label] = {"archive": source, "metrics": metrics, "rule": {**hit, "artifact_id": rule["artifact_id"]},
                          "detail": {"artifact_id": decision["detail_artifact_ids"][0], "read": read},
                          "worker_id": review["worker_id"]}
    write(output / "glm-archive-check.json", archive)


def extra_browser(url, output, password, sync, provider, audit, sql, oracle, redactor, report):
    audit_glm_archive(output)
    for mode in ("handoff-isolation", "gap"):
        folder = output / mode
        folder.mkdir()
        sync.reset("late" if mode == "handoff-isolation" else mode)
        audit.case = mode
        provider.split_old_metrics = True
        provider.probe_old_dates = provider.probe_old_plan = False
        if mode == "handoff-isolation":
            sync.metric_filter = lambda args: args[:3] == (101, *DATES["old"]["baseline"].values())
            sync.stages = {name: (Event(), Event()) for name in ("before-read", "review")}

            def gate(task_id, link):
                if task_id == "evidence-101" and not sync.stages["before-read"][0].is_set():
                    name = "before-read"
                elif task_id == "review" and sync.release.is_set() and not sync.stages["review"][0].is_set():
                    name = "review"
                else:
                    return
                waiting, release = sync.stages[name]
                sync.mark(name + "_waiting", **link)
                waiting.set()
                assert release.wait(45), name

            provider.worker_gate = gate
        if mode == "gap":
            sql("DELETE FROM plan_metric_bindings WHERE plan_id=102")
        start = len(provider.calls)
        request = folder / "request.json"
        result = folder / "browser.json"
        write(request, {"mode": mode, "message": MESSAGE, "status_request": STATUS_REQUEST})
        try:
            completed = subprocess.run(["node", "tests/task30_browser.cjs", url, str(request), str(result)], cwd=ROOT,
                                       env={**t5.browser_env(), "AD_TASK28_PASSWORD": password}, capture_output=True,
                                       text=True, encoding="utf-8", timeout=150)
            if completed.returncode:
                (folder / "browser-error.txt").write_text(completed.stderr, encoding="utf-8")
            assert completed.returncode == 0, completed.stderr
            sample = t5.read(result)
            calls = provider.calls[start:]
            if mode == "gap":
                assert not any(row["tool"] == "query_plan_detail" for row in audit.reads if row["case"] == mode)
                assert not any(row.get("role") == "team_worker" and
                               json.loads(row["messages"][1]["content"])["task"]["task_id"] == "review" for row in calls)
            else:
                for group in ("a", "b"):
                    audit_completed(sample[group], sample[group + "_progress"], calls, audit.receipts, oracle)
                held, = [row for row in sync.timeline if row["event"] == "old_metric_sent"]
                started, = [row for row in sample["waiting"]["entries"]
                            if row["kind"] == "tool_started" and row["worker_id"] == held["parent_call_id"]]
                assert started["runtime_call_id"] == held["runtime_call_id"]
                assert any(row["text"].endswith("等待 #101 基准窗") for row in sample["waiting"]["tasks"])
                when = {row["event"]: row["ns"] for row in sync.timeline}
                b_commit, = [row for row in sync.timeline if row.get("run_id") == sample["b"]["run_id"]]
                assert held["ns"] < b_commit["ns"] < when["release_old_metric"]
                simple = sample["simple"]["trace_id"]
                assert not any(row.get("trace_id") == simple and row.get("role") == "team_worker" for row in calls)
                read, = [row for row in audit.reads if row["trace_id"] == simple]
                assert read["tool"] == "query_plan_detail" and read["role"] == "orchestrator"
            report["cases"][mode] = "pass"
        except Exception:
            report["cases"][mode] = "fail"
            raise
        finally:
            provider.worker_gate = None
            write(folder / "timeline.json", redactor.clean(sync.timeline))
            write(folder / "model.json", redactor.clean(provider.calls[start:]))
            reads = [{key: row[key] for key in ("trace_id", "tool", "role", "runtime_call_id", "parent_call_id")
                      if key in row} for row in audit.reads if row["case"] == mode]
            write(folder / "reads.json", reads)
            write(folder / "receipts.json", redactor.clean([row for row in audit.receipts if row["case"] == mode]))


if __name__ == "__main__":
    main(description=__doc__, extra_browser=extra_browser)
