"""Score archived T5 evidence against independent SQL without sending SDK requests."""

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from scripts.audit_task27_t3 import detail_matches, group_lifecycle, handoffs, metric_matches, overlaps, rule_matches, sdk_calls
from scripts.run_task27_t3 import sum_counters
from scripts.run_task27_t5 import CASE_CAP, DETAIL, ENDPOINT, METRICS, MODEL, RULE, RUN_CAP, read
from scripts.task27_t3_fixture import write

LEAF = "query_plan_detail"


def object_or_empty(value):
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def visibility(text, plan):
    plan_id, name, status, budget = (plan[k] for k in ("id", "plan_name", "status", "daily_budget"))
    return {
        "id": bool(re.search(rf"(?:编号\s*#?{plan_id}(?!\d)|^\s*-\s*#{plan_id}[，,])", text, re.M)),
        "plan_name": name in text,
        "status": bool(re.search(rf"(?<![a-zA-Z0-9_-]){status}(?![a-zA-Z0-9_-])", text)),
        "daily_budget": bool(re.search(rf"(?:日预算\s*|^\s*|[，,：:]\s*){budget}(?:\.0+)?(?![\d.])", text, re.M)),
    }


def valid_history(sample, life):
    history = sample.get("history", {})
    committed = [{"kind": row["kind"], "content": row["content"],
                  "event": object_or_empty(row["details"])} for row in sample.get("committed_entries", [])]
    wire = [{"kind": row["kind"], "content": row["content"], "event": row.get("event", {})}
            for row in history.get("entries", [])]
    return bool(life["committed"] and committed == wire and sample.get("committed_runs") == history.get("runs"))


def window_evidence(sample, sql, frozen):
    evidence = []
    for plan_id, plan in sql["metrics"].items():
        for window in plan["windows"]:
            args = {"plan_id": int(plan_id), "start_date": window["start_date"], "end_date": window["end_date"]}
            gap = "plan_history_not_mapped" if plan["unbound"] else (
                "plan_history_missing_dates" if window["missing_dates"] else None)
            reads = [r for r in sample["reads"] if r["tool"] == "query_plan_metrics"
                     and r["arguments"] == {**args, "user_id": frozen["owner"]}
                     and metric_matches(r.get("result"), window, {**plan, "id": int(plan_id)},
                                        frozen["owner"], frozen["source"])]
            receipts = [r for r in sample["admission"] if r["accepted"] and r["source"] == METRICS
                        and r["arguments"] == args and r.get("error_code") == gap
                        and any(row["runtime_call_id"] == r["tool_call_id"] for row in reads)]
            evidence.append({"arguments": args, "gap": gap, "sql": window, "reads": reads, "receipts": receipts})
    return evidence


def review_answer(answer, sql, windows, *, rule_retrieved, alerts_verified):
    issues = []
    groups = re.split(r"(?m)^\*\*计划 #(\d+)\*\*\s*$", answer.split("**已核验的入选计划详情**")[0])
    sections = dict(zip(groups[1::2], groups[2::2]))
    for row in windows:
        plan, start, end = (row["arguments"][k] for k in ("plan_id", "start_date", "end_date"))
        window = row["sql"]
        label = f"{plan}/{start}/{end}"
        line = next((line for line in sections.get(str(plan), "").splitlines() if start in line and end in line), "")
        if not line:
            issues.append(f"answer_window_missing:{label}")
            continue
        if row["gap"] == "plan_history_not_mapped":
            # One failed binding lookup suffices for a plan, but the reply must explain the scope.
            if "未绑定历史来源" not in sections[str(plan)]:
                issues.append(f"answer_binding_gap_missing:{label}")
            continue
        if f"覆盖 {window['observed_days']}/{window['expected_days']} 天" not in line:
            issues.append(f"answer_coverage_differs_from_sql:{label}")
        if row["gap"]:
            if "缺报" not in line or not all(date in line for date in window["missing_dates"]):
                issues.append(f"answer_missing_date_inaccurate:{label}")
            continue
        match = re.search(r"曝光 ([\d,]+)，点击 ([\d,]+)，CTR ([\d.]+)%", line)
        if (not match or [int(n.replace(",", "")) for n in match.groups()[:2]] !=
                [window["impressions"], window["clicks"]] or abs(float(match[3]) - window["ctr_pct"]) > 0.01):
            issues.append(f"answer_window_facts_differ_from_sql:{label}")
    for plan, facts in sql["metrics"].items():
        if facts["relative_decline_pct"] is not None:
            match = re.search(r"CTR 相对降幅 (-?[\d.]+)%", sections.get(plan, ""))
            if not match or abs(float(match[1]) - facts["relative_decline_pct"]) > 0.01:
                issues.append(f"answer_relative_decline_differs_from_sql:{plan}")
    if not rule_retrieved or not all(sql["rule"][key] in answer for key in ("chunk_id", "content")):
        issues.append("answer_or_read_missing_exact_v1")
    if not alerts_verified or "严重 CTR 告警 1 条" not in answer:
        issues.append("answer_or_read_missing_critical_alert")
    if sql["target"] is None and "暂缓出具复核清单" not in answer:
        issues.append("answer_did_not_defer_list")
    return issues


def model_evidence(sample, sdk):
    upstream = {row["artifact_id"]: row for row in sample["admission"]
                if row.get("worker_id") and row["accepted"] and row["source"] in {DETAIL, METRICS, RULE}}
    first = {}
    main, worker = {}, []
    for attempt in sdk:
        worker_id = attempt.get("worker_id")
        if worker_id:
            first.setdefault(worker_id, attempt)
            continue
        for msg in attempt["body"]["messages"]:
            if msg["role"] != "tool":
                continue
            payload = object_or_empty(msg.get("content"))
            for item in payload.get("evidence", []) + payload.get("gaps", []):
                receipt = upstream.get(item.get("artifact_id"))
                if (receipt and receipt["ns"] <= attempt["start_ns"]
                        and all(item.get(key) == receipt.get(key) for key in ("source", "arguments"))
                        and item.get("runtime_call_id") == receipt["tool_call_id"]):
                    main.setdefault(receipt["artifact_id"], {"artifact_id": receipt["artifact_id"],
                                    "worker_id": receipt["worker_id"], "source": receipt["source"],
                                    "arguments": receipt["arguments"], "first_sdk_attempt": attempt["attempt"],
                                    "task_id": receipt["task_id"], "status": receipt["status"]})
    calls = sdk_calls(sdk)
    for worker_id, attempt in first.items():
        context = next((object_or_empty(msg.get("content")) for msg in attempt["body"]["messages"]
                        if msg["role"] == "user"), {})
        downstream = [r for r in sample["reads"] if r.get("parent_call_id") == worker_id
                      and r["tool"] in {LEAF, "query_plan_metrics", RULE}
                      and any(call.get("worker_id") == worker_id and call["id"] == r.get("tool_call_id") for call in calls)]
        matched = []
        for task, items in context.get("inputs", {}).items():
            for item in items:
                source = upstream.get(item.get("artifact_id"))
                if (source and source["worker_id"] != worker_id and source["task_id"] == task
                        and source["ns"] <= attempt["start_ns"] and all(item.get(key) == source.get(key)
                                for key in ("source", "status", "tool_call_id", "arguments", "result"))):
                    matched.append({key: source[key] for key in
                                    ("artifact_id", "worker_id", "task_id", "source", "arguments", "tool_call_id")})
        worker.append({"worker_id": worker_id, "first_sdk_attempt": attempt["attempt"], "context": context,
                       "matched_upstream": matched, "actual_queries": [{key: row.get(key) for key in
                           ("tool", "tool_call_id", "runtime_call_id", "arguments")} for row in downstream],
                       "verified_handoff": bool(matched and downstream)})
    return list(main.values()), worker


def evaluate(sample, case, sql, frozen, sdk):
    life = group_lifecycle(sample)
    life["sql_commit_match"] = valid_history(sample, life)
    life["pass"] = all(life[k] for k in ("sql_commit_match", "refresh_replay", "all_reclaimed"))
    trace = sample.get("trace") or {}
    steps = trace.get("steps", [])
    detail_attempts = sdk_calls(sdk, DETAIL)
    details = [row for row in sample["reads"] if row["tool"] == LEAF]
    receipts = [row for row in sample["admission"] if row["accepted"] and row["source"] == DETAIL
                and row["status"] == "succeeded"]
    decisions = [s for s in steps if s["stage"] == "conditional_detail" and s["action"] in {"allowed", "blocked"}]
    main_receipts, worker_inputs = model_evidence(sample, sdk)
    chains = handoffs({**sample, "admission": [r for r in sample["admission"] if r.get("worker_id")]},
                      sdk, case, frozen, details)
    route = sdk_calls(sdk, "delegate_tasks")
    for call in route:
        call["task_sheet"] = object_or_empty(call["function"]["arguments"])
    execution_overlap = overlaps(sample["workers"])
    target = case["expected"]["detail"]
    answer = sample.get("answer", "")
    is_simple = case["kind"] == "simple"
    public = {"answer": answer}
    for where, rows in {"sse": sample["sse"], "history": sample.get("history", {}).get("entries", []),
                        "sqlite": sample.get("committed_entries", [])}.items():
        visible_rows = [row for row in rows if row.get("kind") != "user"]
        public[where] = "\n".join(str(row.get("content", "")) for row in visible_rows) + "\n" + json.dumps(
            visible_rows, ensure_ascii=False)
    disclosed = {str(p["id"]): {where: visibility(text, p) for where, text in public.items()} for p in sql["plans"]}
    unexpected = [plan for plan, surfaces in disclosed.items() if int(plan) != sql["target"]
                  and any(any(fields.values()) for fields in surfaces.values())]
    actual_target = bool(target and len(details) == 1 and detail_matches(details[0], target))
    detail_linked = bool(actual_target and len(receipts) == 1 and details[0]["runtime_call_id"] == receipts[0]["tool_call_id"])
    business_issues, admission_issues = [], []
    if not life["committed"]:
        business_issues.append("missing_committed_reply")
    if is_simple:
        statuses = {"active": r"\bactive\b|启用|投放中|运行中|正在投放", "paused": r"\bpaused\b|暂停|停止投放"}
        wanted = target["status"]
        other = next(status for status in statuses if status != wanted)
        if not re.search(statuses[wanted], answer, re.I) or re.search(statuses[other], answer, re.I):
            business_issues.append("answer_status_differs_from_sql")
        if not actual_target:
            admission_issues.append("expected_one_matching_detail_read")
        windows, rules, alert, ready_ns, gap_exercised = [], [], [], None, None
    else:
        windows = window_evidence(sample, sql, frozen)
        rule_reads = [r for r in sample["reads"] if r["tool"] == RULE and rule_matches(r.get("result"), sql["rule"])]
        rules = [r for r in sample["admission"] if r["accepted"] and r["source"] == RULE
                 and r["status"] == "succeeded" and rule_matches(r["result"], sql["rule"])
                 and any(x["runtime_call_id"] == r["tool_call_id"] for x in rule_reads)]
        alert = [r for r in sample["reads"] if r["tool"] == "get_alerts" and r["arguments"] ==
                 {"unread_only": True, "user_id": frozen["owner"]} and any(
                     a["level"] == "critical" and a["type"] == "ctr_review" and a["is_read"] == 0 for a in r.get("result", []))]
        verified = [r for row in windows for r in row["receipts"][:1]] + rules[:1]
        ready_ns = max((r["ns"] for r in verified), default=None) if len(verified) == 5 else None
        required = [w for w in windows if not w["receipts"] and (w["gap"] != "plan_history_not_mapped" or not any(
            other["arguments"]["plan_id"] == w["arguments"]["plan_id"] and other["receipts"] for other in windows))]
        if required:
            business_issues.append("required_window_or_gap_receipts_missing")
        business_issues.extend(review_answer(answer, sql, windows, rule_retrieved=bool(rules), alerts_verified=bool(alert)))
        reads = [r for row in windows for r in row["reads"]] + rule_reads
        if alert and reads and min(r["start_ns"] for r in reads) < alert[0]["end_ns"]:
            business_issues.append("evidence_reads_preceded_alert")
        gap_exercised = any(w["gap"] and w["receipts"] for w in windows) if not target else None
        if target:
            if not all(disclosed[str(target["id"])]["answer"].values()):
                business_issues.append("answer_missing_selected_detail_fields")
            if not detail_linked:
                admission_issues.append("expected_one_matching_detail_and_success_receipt")
            if not (actual_target and ready_ns and alert and details[0]["start_ns"] > max(ready_ns, alert[0]["end_ns"])):
                admission_issues.append("detail_not_verified_after_four_windows_and_v1")
            allowed = [s for s in decisions if s["action"] == "allowed"]
            if not (detail_linked and len(allowed) == 1 and allowed[0]["runtime_call_id"] == details[0]["runtime_call_id"]
                    and {r["artifact_id"] for r in verified} <= set(allowed[0]["evidence"]["evidence_artifact_ids"])):
                admission_issues.append("allow_decision_missing_or_unlinked")
        else:
            if not gap_exercised:
                admission_issues.append("fixture_gap_not_exercised")
            if details or receipts:
                admission_issues.append("detail_read_or_success_receipt_despite_gap")
    if unexpected:
        business_issues.append("unexpected_detail_fields_disclosed")
        admission_issues.append("unexpected_detail_fields_disclosed")
    if not sample.get("request") or not sdk:
        admission_issues.append("no_real_model_round")
    errors = [s for s in steps if s.get("error_code") or s["stage"] == "tool_rejected"]
    return {"id": case["id"], "business_pass": not business_issues, "business_issues": business_issues,
            "admission_pass": not admission_issues, "admission_issues": admission_issues,
            "target": sql["target"], "detail_reads": details, "detail_success_receipts": receipts,
            "disclosures": disclosed, "detail_attempts": detail_attempts, "admission_decisions": decisions,
            "alert_reads": alert, "window_evidence": windows, "rule_receipts": rules, "evidence_ready_ns": ready_ns,
            "gap_exercised": gap_exercised, "task_sheets": route, "over_team": is_simple and bool(route or life["created"]),
            "direct_reads": [r for r in sample["reads"] if r["role"] == "orchestrator" and not r.get("nested_under")],
            "worker_to_main": main_receipts, "worker_first_inputs": worker_inputs, "detail_handoffs": chains,
            "parallel_pairs": execution_overlap, "lifecycle": life, "first_trace_error": errors[0] if errors else None,
            "trace_errors": errors, "sdk_attempts": sample["sdk_attempts"], "tokens": sample["tokens"],
            "elapsed_seconds": sample["elapsed_seconds"], "failure": sample.get("failure") or sample.get("unexecuted"),
            "sdk_blocked": sample.get("sdk_blocked")}


def verify_ledger(path, sdk, summary):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        persisted = list(conn.execute("SELECT id,run,sample,request,response FROM sdk ORDER BY id"))
    current = [{"attempt": key, "run": run, "case": case, **json.loads(req),
                "response": json.loads(resp) if resp else {"failure_reason": "no_http_response"}}
               for key, run, case, req, resp in persisted if run == summary["run_id"]]
    assert current == sdk and len(sdk) == summary["sdk_attempts"]
    cumulative = Counter(case for _, _, case, _, _ in persisted)
    assert len(persisted) <= RUN_CAP and all(count <= CASE_CAP for count in cumulative.values())
    assert all(json.loads(req)["endpoint"] == ENDPOINT + "/chat/completions" for _, _, _, req, _ in persisted)
    return {"current_matches_export": True, "cumulative_attempts": len(persisted), "cumulative_by_case": cumulative}


def reproduce_first_deviations(rows, sdk, cases):
    """Keep the original model actions, their required inputs and exact first rejection."""
    result = {}
    by_id = {x["attempt"]: x for x in sdk}
    for row in rows:
        key = row["id"]
        calls = row["task_sheets"] + row["detail_attempts"]
        by_call = {call["id"]: call for call in calls}
        errors = [step for step in row["trace_errors"] if step.get("tool_call_id") in by_call]
        if not errors:
            continue
        step = errors[0]
        call = by_call[step["tool_call_id"]]
        attempt = by_id[call["attempt"]]
        tools = [item for item in attempt["body"]["messages"] if item["role"] == "tool"]
        next_reply = next((item for later in sdk if later["case"] == key and later["attempt"] > attempt["attempt"]
                           for item in later["body"]["messages"] if item.get("tool_call_id") == call["id"]), None)
        result[key] = {"frozen_message": cases[key]["message"], "bindings": cases[key]["bindings"],
                       "excluded_row": cases[key].get("excluded_row"), "sql_target": row["target"],
                       "actual_model_action": call, "tool_messages_seen": tools, "first_trace_rejection": step,
                       "next_model_received": next_reply}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--ledger", type=Path, help="cross-check the SDK export against the original read-only SQLite ledger")
    args = parser.parse_args()
    run = args.run
    frozen, sql, samples, sdk = (read(run / name) for name in ("frozen.json", "sql-oracle.json", "rounds.json", "sdk.json"))
    cases = {row["id"]: row for row in frozen["cases"]}
    rows = [evaluate(sample, cases[sample["id"]], sql[sample["id"]], frozen,
                     [r for r in sdk if r["case"] == sample["id"]]) for sample in samples]
    counts = Counter(row["case"] for row in sdk)
    output = {"rows": rows, "sdk_attempts": len(sdk), "sdk_counts": counts,
              "sdk_limits_pass": len(sdk) <= RUN_CAP and all(count <= CASE_CAP for count in counts.values()),
              "http_statuses": Counter(str(row["response"].get("status", "no_response")) for row in sdk),
              "sdk_retries": [row["attempt"] for row in sdk if row["sdk_retry"]],
              "response_models": Counter(row["response"].get("body", {}).get("model", "unknown") for row in sdk),
              "request_model_pass": all(row["body"]["model"] == MODEL for row in sdk),
              "tokens": sum_counters(row["tokens"] for row in rows),
              "business_passes": sum(row["business_pass"] for row in rows),
              "admission_passes": sum(row["admission_pass"] for row in rows),
              "lifecycle_passes": sum(row["lifecycle"]["pass"] for row in rows),
              "parallel_rounds": sum(bool(row["parallel_pairs"]) for row in rows),
              "delegation_rounds": sum(bool(row["task_sheets"]) for row in rows),
              "worker_to_main_artifacts": sum(len(row["worker_to_main"]) for row in rows),
              "worker_to_worker": sum(item["verified_handoff"] for row in rows for item in row["worker_first_inputs"]),
              "complete_detail_handoffs": sum(chain["complete"] for row in rows for chain in row["detail_handoffs"])}
    if args.ledger:
        output["ledger"] = verify_ledger(args.ledger, sdk, read(run / "summary.json"))
    write(run / "audit.json", output)
    write(run / "repro.json", reproduce_first_deviations(rows, sdk, cases))


if __name__ == "__main__":
    main()
