"""Score archived T3 tool execution, handoffs and lifecycle without calling the model."""

import argparse
from collections import Counter
from contextlib import closing
from itertools import combinations
import json
from pathlib import Path
import sqlite3

from scripts.task27_t3_fixture import write

DETAIL, METRICS, RULE = "get_plan_detail", "get_plan_metrics_by_date", "search_knowledge"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def rule_matches(result, rule):
    return isinstance(result, dict) and any(
        hit.get("chunk_id") == rule["chunk_id"] and hit.get("version") == rule["version"]
        and hit.get("rule_id") == rule["rule_id"] and hit.get("content", hit.get("excerpt")) == rule["content"]
        for hit in result.get("evidence", [])
    )


def metric_matches(result, expected, plan, owner, source):
    if not isinstance(result, dict):
        return False
    if plan["unbound"]:
        return result.get("error_code") == "plan_history_not_mapped"
    return (
        result.get("source_plan_id") == expected["sql_params"][0]
        and result.get("source_id") == source["source_id"] and result.get("data_version") == source["data_version"]
        and result.get("plan_id") == plan["id"] and result.get("user_id", owner) == owner
        and all(result.get("coverage", {}).get(key) == expected[key] for key in
                ("expected_days", "observed_days", "missing_dates"))
        and (expected["missing_dates"] or all(result.get(key) == expected[key] for key in ("impressions", "clicks")))
    )


def detail_matches(row, plan):
    return (row["arguments"] == {"plan_id": plan["id"], "user_id": plan["user_id"]}
            and all(row.get("result", {}).get(key) == value for key, value in plan.items()))


def sdk_calls(rows, name=None):
    return [{"attempt": row["attempt"], "role": row.get("role"), "step": row.get("step"),
             "worker_id": row.get("worker_id"), **call}
            for row in rows for choice in row["response"].get("body", {}).get("choices", [])
            for call in choice.get("message", {}).get("tool_calls") or []
            if not name or call["function"]["name"] == name]


def group_lifecycle(sample):
    trace = sample.get("trace") or {}
    steps = trace.get("steps", [])
    events = sample["sse"]
    history = sample.get("history") or {}
    entries = history.get("entries", [])
    runs = history.get("runs", [])
    terminals = [event for event in events if event["type"] in {"done", "error"}]
    group_id = sample.get("request", {}).get("conversation_id")
    run_id = runs[0]["run_id"] if len(runs) == 1 else None
    groups_match = bool(group_id and run_id and trace.get("trace_id")
                        and history.get("conversation", {}).get("conversation_id") == group_id
                        and all(entry["run_id"] == run_id for entry in entries)
                        and all(event.get("conversation_id") == group_id and event.get("run_id") == run_id
                                and event.get("trace_id") == trace["trace_id"] for event in events))
    committed = bool(sample.get("http_status") == 200 and groups_match and len(terminals) == 1
                     and events[-1] == terminals[0] and terminals[0]["type"] == "done"
                     and runs[0]["status"] == "completed" and trace["status"] == "completed"
                     and [e["content"] for e in entries if e["kind"] == "user"] == [sample["request"]["message"]]
                     and [e["content"] for e in entries if e["kind"] == "assistant"] == [sample.get("answer")])
    created = Counter(s["runtime_call_id"] for s in steps if s["stage"] == "worker" and s["action"] == "created")
    reclaimed = Counter(s["runtime_call_id"] for s in steps if s["stage"] == "worker" and s["action"] == "reclaimed")
    worker_events = [e["event"] for e in entries if e.get("event")]
    dispatched_events = Counter(e["worker_id"] for e in worker_events
                                if e["kind"] == "dispatched" and e.get("worker_id"))
    reclaimed_events = Counter(e["worker_id"] for e in worker_events
                               if e["kind"] == "reclaimed" and e.get("worker_id"))
    dom = sample.get("live", {})
    live_progress = [e["entry_id"] for e in entries if e["kind"] == "worker"
                     and dom.get(e["entry_id"], {}).get("before_stream_end")]
    blocked = [event for event in worker_events if event.get("error_code") == "dependency_failed"]
    return {"terminal_types": [e["type"] for e in terminals], "group_id": group_id, "run_id": run_id,
            "committed": committed, "trace_id": trace.get("trace_id"), "created": list(created),
            "reclaimed": list(reclaimed), "all_reclaimed": bool(trace) and created == reclaimed
            and created == dispatched_events == reclaimed_events and all(n == 1 for n in created.values()),
            "worker_events": len(worker_events), "blocked_dependencies": blocked,
            "chromium": sample.get("chromium"), "refresh_replay": bool(sample.get("dom_replay_pass"))
            and sample.get("replay_history") == history and sample.get("replay_dom") == sample.get("dom"),
            "worker_progress_before_terminal": live_progress, "trace_seconds": trace.get("total_duration")}


def overlaps(workers):
    pairs = []
    for left, right in combinations(workers, 2):
        overlap = min(left["end_ns"], right["end_ns"]) - max(left["start_ns"], right["start_ns"])
        if overlap > 0:
            pairs.append({"workers": [left["worker_id"], right["worker_id"]],
                          "tasks": [left["task"]["task_id"], right["task"]["task_id"]],
                          "overlap_ms": round(overlap / 1_000_000, 3)})
    return pairs


def handoffs(sample, sdk, case, frozen, details):
    expected = case["expected"]
    steps = (sample.get("trace") or {}).get("steps", [])
    results = {step.get("runtime_call_id"): step for step in steps if step["stage"] == "tool_result"}
    trace_accepted = {(s.get("parent_call_id"), s.get("runtime_call_id")) for s in steps
                      if s["stage"] == "receipt" and s["action"] == "accepted"}
    upstream = {(r["task_id"], r["artifact_id"]): r for r in sample["admission"]
                if r["accepted"] and r["status"] == "succeeded" and r["source"] in {METRICS, RULE}
                and (r["worker_id"], r["tool_call_id"]) in trace_accepted}
    matched = []
    for detail in details:
        worker = detail.get("parent_call_id")
        first = next((row for row in sdk if worker and row.get("worker_id") == worker), None)
        if not first or not expected.get("metrics"):
            continue
        context = json.loads(next(m["content"] for m in first["body"]["messages"] if m["role"] == "user"))
        refs = []
        proof_windows = {}
        proof_rule = False
        for task_id, items in context["inputs"].items():
            for item in items:
                record = upstream.get((task_id, item["artifact_id"]))
                if not record or record["worker_id"] == worker or record["ns"] > first["start_ns"]:
                    continue
                if any(item.get(key) != record.get(key) for key in ("source", "tool_call_id", "arguments", "result")):
                    continue
                if record["source"] == METRICS:
                    args = item["arguments"]
                    plan = expected["metrics"].get(str(args["plan_id"]))
                    window = tuple(args[key] for key in ("start_date", "end_date"))
                    if plan and any(window == (w["start_date"], w["end_date"])
                                    and metric_matches(item["result"], w, {**plan, "id": args["plan_id"]},
                                                       frozen["owner"], frozen["source"])
                                    for w in plan["windows"]):
                        proof_windows[args["plan_id"], window] = item["result"]
                else:
                    proof_rule |= rule_matches(item["result"], expected["rule"])
                refs.append({"upstream_task": task_id, "upstream_worker": record["worker_id"],
                             "artifact_id": record["artifact_id"], "source": record["source"],
                             "runtime_call_id": record["tool_call_id"],
                             "arguments": item.get("arguments")})
        target = None
        rule = expected["rule"]
        if len(proof_windows) == 4 and proof_rule:
            qualified = []
            with closing(sqlite3.connect(":memory:")) as conn:
                for plan, facts in expected["metrics"].items():
                    baseline, current = (proof_windows[int(plan), (w["start_date"], w["end_date"])]
                                         for w in facts["windows"])
                    params = [baseline["impressions"], current["impressions"],
                              100 * baseline["clicks"] / baseline["impressions"],
                              100 * current["clicks"] / current["impressions"],
                              rule["min_impressions"], rule["min_relative_decline_pct"]]
                    if conn.execute(frozen["sql"]["review"], params).fetchone()[1]:
                        qualified.append(int(plan))
            if len(qualified) == 1:
                target = qualified[0]
        matched.append({"worker_id": worker, "task_id": context["task"]["task_id"], "first_sdk_attempt": first["attempt"],
                        "first_model_call_id": first.get("model_call_id"), "upstream": refs,
                        "inputs_declared": context["task"]["input_refs"], "artifact_refs": context["task"]["artifact_refs"],
                        "dependencies": context["task"]["dependencies"],
                        "verified_windows": len(proof_windows), "verified_rule": proof_rule,
                        "recomputed_target": target, "actual_arguments": detail["arguments"],
                        "runtime_call_id": detail.get("runtime_call_id"),
                        "tool_call_id": detail.get("tool_call_id"),
                        "complete": bool(target and expected["detail"] and target == expected["detail"]["id"]
                                         and detail_matches(detail, expected["detail"])
                                         and results.get(detail.get("runtime_call_id"), {}).get("status") == "ok")})
    return matched


def evaluate(sample, case, frozen, sdk, review):
    expected = case["expected"]
    life = group_lifecycle(sample)
    reads = sample.get("reads", [])
    details = [row for row in reads if row["tool"] == DETAIL]
    target = expected["detail"]
    issues = []
    if not life["committed"]:
        issues.append("missing_committed_reply")
    if not life["all_reclaimed"]:
        issues.append("worker_reclamation_mismatch")
    if target:
        if len(details) != 1 or not detail_matches(details[0], target):
            issues.append("actual_detail_reads_differ_from_sql")
    elif details:
        issues.append("dependent_detail_read_despite_gap")
    checks = {}
    if "metrics" in expected:
        for plan, values in expected["metrics"].items():
            for w in values["windows"]:
                matching = [r for r in reads if r["tool"] == METRICS and r["arguments"] ==
                            {"plan_id": int(plan), "start_date": w["start_date"], "end_date": w["end_date"],
                             "user_id": frozen["owner"]} and
                            metric_matches(r.get("result"), w, {**values, "id": int(plan)},
                                           frozen["owner"], frozen["source"])]
                checks[f"{plan}/{w['start_date']}/{w['end_date']}"] = matching
        for plan, values in expected["metrics"].items():
            keys = [f"{plan}/{w['start_date']}/{w['end_date']}" for w in values["windows"]]
            if not (any(checks[key] for key in keys) if values["unbound"] else all(checks[key] for key in keys)):
                issues.append(f"missing_expected_history_read_{plan}")
        rule_reads = [r for r in reads if r["tool"] == RULE and rule_matches(r.get("result"), expected["rule"])]
        alert_reads = [r for r in reads if r["tool"] == "get_alerts"
                       and r["arguments"] == {"unread_only": True, "user_id": frozen["owner"]}
                       and any(x.get("level") == "critical" and x.get("is_read") == 0 for x in r.get("result", []))]
        if not rule_reads:
            issues.append("required_rule_not_retrieved")
        if not alert_reads:
            issues.append("critical_unread_alert_not_verified")
        evidence = [rows[0] for rows in checks.values() if rows] + rule_reads[:1]
        if alert_reads and evidence and min(r["start_ns"] for r in evidence) < alert_reads[0]["end_ns"]:
            issues.append("investigation_preceded_alert")
        if details and evidence and min(r["start_ns"] for r in details) < max(r["end_ns"] for r in evidence):
            issues.append("detail_preceded_required_evidence")
    routes = sdk_calls(sdk, "delegate_tasks")
    for route in routes:
        route["task_sheet"] = route["function"]["arguments"]
        try:
            route["task_sheet"] = json.loads(route["task_sheet"])
        except ValueError:
            pass
    chains = handoffs(sample, sdk, case, frozen, details)
    successes = [row for row in chains if row["complete"]]
    route_status = "direct" if not routes else "delegation_attempted" if not life["created"] else "worker_execution"
    duplicate_or_reuse = [s for s in (sample.get("trace") or {}).get("steps", [])
                          if s["stage"] in ("tool_reuse", "tool_rejected") or
                          (s["stage"] == "tool_result" and s.get("error_code"))]
    return {"id": case["id"], "pair": case.get("pair"), "kind": case["kind"],
            "business": None if review is None else not issues and review["answer_pass"],
            "machine_issues": issues, "answer_review": review, "expected_detail": target["id"] if target else None,
            "actual_detail_reads": [row["arguments"] for row in details],
            "route": route_status, "task_sheets": routes, "handoffs": chains,
            "complete_handoff": len(details) == 1 and len(successes) == 1,
            "partial_handoff": any(chain["upstream"] for chain in chains),
            "overlaps": overlaps(sample.get("workers", [])), "lifecycle": life,
            "over_team": case["kind"] == "simple" and bool(routes or life["created"]),
            "sdk_attempts": sample.get("sdk_attempts", []), "model_calls": sample.get("model_calls", 0),
            "tokens": sample.get("tokens", {}), "elapsed_seconds": sample.get("elapsed_seconds"),
            "read_count": sample.get("read_count"), "duplicate_reads": sample.get("duplicate_reads"),
            "errors_rejections_and_reuse": duplicate_or_reuse}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    frozen = read(args.run / "frozen.json")
    sdk = read(args.run / "sdk.json")
    reviews = read(args.review) if args.review else {}
    cases = {case["id"]: case for case in frozen["cases"]}
    samples = read(args.run / "rounds.json")
    rows = [evaluate(r, cases[r["id"]], frozen, [x for x in sdk if x["case"] == r["id"]], reviews.get(r["id"]))
            for r in samples]
    by_id = {row["id"]: row for row in rows}
    pairs = [i for i in range(1, 5) if all(by_id.get(f"{kind}-{i}", {}).get("complete_handoff")
                                          for kind in ("branch", "swap"))]
    tokens = Counter()
    for row in rows:
        tokens.update(row["tokens"])
    output = {"rows": rows, "complete_pairs": pairs, "sdk_attempts": len(sdk),
              "sdk_failures": [row["attempt"] for row in sdk if row["response"].get("status") != 200],
              "sdk_retries": [row["attempt"] for row in sdk if row["sdk_retry"]],
              "tokens": dict(tokens), "round_count": len(rows),
              "business_passes": sum(row["business"] is True for row in rows),
              "business_reviewed": sum(row["business"] is not None for row in rows),
              "complete_handoffs": sum(row["complete_handoff"] for row in rows),
              "parallel_rounds": sum(bool(row["overlaps"]) for row in rows)}
    write(args.run / "audit.json", output)


if __name__ == "__main__":
    main()
