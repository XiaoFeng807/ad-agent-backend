"""Score T5A business results and actual GLM budget-boundary calls separately."""

import argparse
from collections import Counter
from pathlib import Path

from scripts.audit_task27_t3 import sdk_calls
from scripts.audit_task27_t5 import evaluate, object_or_empty, reproduce_first_deviations, verify_ledger
from scripts.run_task27_t5 import DETAIL, MODEL, read
from scripts.run_task27_t5a import CAPS, FULL, REPLAYS, RUN_CAP, model_steps
from scripts.task27_t3_fixture import ROOT, write
from scripts.verify_task27_r3 import FINAL


def trace_steps(sample, stage, action):
    return [s for s in (sample.get("trace") or {}).get("steps", [])
            if (s["stage"], s["action"]) == (stage, action)]


def linked_detail(sample, score, sdk):
    if len(score["detail_reads"]) != 1 or len(score["detail_success_receipts"]) != 1:
        return None
    leaf, = score["detail_reads"]
    receipt, = score["detail_success_receipts"]
    call_id, runtime_id = leaf["tool_call_id"], leaf["runtime_call_id"]
    artifact = receipt["artifact_id"]
    calls = [call for call in sdk_calls(sdk, DETAIL) if call["id"] == call_id
             and object_or_empty(call["function"]["arguments"]) == {"plan_id": score["target"]}]
    admitted = [s for s in trace_steps(sample, "conditional_detail", "allowed")
                if s.get("tool_call_id") == call_id and s.get("runtime_call_id") == runtime_id
                and s["evidence"]["requested_plan_id"] == score["target"] and s["evidence"]["executed"]]
    accepted = [s for s in trace_steps(sample, "receipt", "accepted")
                if s.get("runtime_call_id") == runtime_id
                and (s.get("tool_call_id") == call_id
                     or (receipt.get("worker_id") and s.get("parent_call_id") == receipt["worker_id"]))
                and s["evidence"]["artifact_id"] == artifact]
    published = trace_steps(sample, "conditional_detail", "reply")
    if not (len(calls) == len(admitted) == len(accepted) == len(published) == 1
            and published[0]["evidence"]["detail_artifact_ids"] == [artifact]
            and receipt["arguments"] == {"plan_id": score["target"]}):
        return None
    request = next(row for row in sdk if row["attempt"] == calls[0]["attempt"])
    schema = [tool for tool in request["body"].get("tools", []) if tool["function"]["name"] == DETAIL]
    if len(schema) != 1 or leaf["start_ns"] <= request["response"].get("end_ns", leaf["start_ns"]):
        return None
    return {"sdk_attempt": request["attempt"], "sdk_step": request["step"], "schema": schema[0],
            "call": calls[0], "runtime_call_id": runtime_id, "admission": admitted[0],
            "arguments": leaf["arguments"], "detail_artifact_id": artifact, "receipt": receipt,
            "after_evidence_ms": round((leaf["start_ns"] - score["evidence_ready_ns"]) / 1e6, 3)
            if score["evidence_ready_ns"] else None}


def boundary(sample, score, sdk, reference_tools):
    replay = sample.get("replay")
    steps = model_steps(sample)
    requests = [row for row in sdk if row.get("step") == FINAL]
    opened = trace_steps(sample, FINAL, "opened")
    result = trace_steps(sample, FINAL, "result")
    if not requests:
        return {"status": "opened_without_sdk" if opened else "not_reached",
                "model_calls": len(steps), "opened": opened}
    request = requests[-1]
    final = request["body"]
    call = score["detail_chain"]
    ids = set(opened[0]["evidence"]["evidence_artifact_ids"]) if len(opened) == 1 else set()
    tool_inputs = "\n".join(m.get("content") or "" for m in final["messages"] if m["role"] == "tool")
    verified = {w["receipts"][0]["artifact_id"] for w in score["window_evidence"] if w["receipts"]}
    rules = {row["artifact_id"] for row in score["rule_receipts"]}
    expected_step_ids = [row["runtime_call_id"] for row in steps]
    ordinal_ok = (len(steps) == 12 and steps[-1]["action"] == FINAL
                  and expected_step_ids[-1] == request["model_call_id"] and request is sdk[-1])
    if replay:
        prefix = replay["fixed_model_calls"]
        ordinal_ok = (ordinal_ok and len(sdk) == 1 and len(prefix) == 11 and not replay["fixed_model_errors"]
                      and Counter(row["model_call_id"] for row in prefix) == Counter(expected_step_ids[:-1])
                      and replay["final_model_link"]["model_call_id"] == request["model_call_id"])
    else:
        ordinal_ok = ordinal_ok and len(sdk) == 12
    checks = {
        "twelfth_model_request": len(requests) == 1 and ordinal_ok,
        "r3_tool_schema": final.get("tools") == reference_tools and final.get("tool_choice") == "auto",
        "verified_opening": bool(len(opened) == 1 and opened[0]["budget"]["model"] == 1
                                 and opened[0]["budget"]["tool"] > 0
                                 and opened[0]["evidence"]["allowed_plan_ids"] == [score["target"]]
                                 and len(verified) == 4 and verified <= ids and rules & ids
                                 and all(artifact in tool_inputs for artifact in ids)),
        "structured_call_and_receipt": bool(call and call["sdk_attempt"] == request["attempt"]
                                            and len(sdk_calls([request])) == 1 and len(result) == 1
                                            and result[0]["evidence"]["actions"] == [{
                                                "tool_call_id": call["call"]["id"], "tool": DETAIL,
                                                "success": True, "error_code": None,
                                                "runtime_call_id": call["runtime_call_id"]}]
                                            and result[0]["evidence"]["detail_artifact_ids"]
                                            == [call["detail_artifact_id"]]),
    }
    return {"status": "passed" if all(checks.values()) and score["round_pass"] else "failed",
            "sdk_attempt": request["attempt"], "model_calls": len(steps), "checks": checks,
            "opened": opened, "result": result, "model_response": request["response"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    folder = parser.parse_args().archive
    summary = read(folder / "summary.json")
    assert summary["mode"] == "live" and summary["preflight_sdk_attempts"] == 0
    assert summary["storage_before"] == summary["storage_after"]
    assert summary["sdk_limit"] == RUN_CAP and summary["sample_limits"] == CAPS
    frozen = read(folder / "frozen.json")
    cases = {case["id"]: case for case in frozen["cases"]}
    cases.update({name: {**cases[original], "id": name} for name, original in REPLAYS.items()})
    samples, sdk, sql = (read(folder / path) for path in ("rounds.json", "sdk.json", "sql-oracle.json"))
    ledger = verify_ledger(folder / "glm-sdk-ledger.sqlite3", sdk, summary)
    assert ledger["cumulative_attempts"] == len(sdk) <= RUN_CAP
    assert all(case in CAPS and n <= CAPS[case] for case, n in ledger["cumulative_by_case"].items())
    assert all(row["body"]["model"] == MODEL and row["sdk_retry"] == 0 for row in sdk)
    assert all(row["response"]["body"]["model"] == MODEL for row in sdk if "body" in row["response"])
    assert [sample["id"] for sample in samples] == [*FULL, *REPLAYS]
    group_ids = [sample["request"]["conversation_id"] for sample in samples if sample.get("request")]
    assert len(group_ids) == len(set(group_ids))
    reference = read(ROOT / "logs/task27-r3/verified-sqlite/branch-1.json")
    reference_tools, = [row["tools"] for row in reference["model_calls"] if row["step"] == FINAL]
    rows, skipped, repro = [], {}, {}
    for sample in samples:
        name = sample["id"]
        if reason := sample.get("unexecuted"):
            skipped[name] = reason
            continue
        requests = [row for row in sdk if row["case"] == name]
        score = evaluate(sample, cases[name], sql[name], frozen, requests)
        score["detail_chain"] = linked_detail(sample, score, requests)
        if score["target"] and score["detail_chain"] is None:
            score["admission_issues"].append("detail_call_to_published_receipt_chain_missing")
            score["admission_pass"] = False
        score["lifecycle"]["single_browser_post"] = sample.get("stream_posts") == 1
        score["lifecycle"]["pass"] &= score["lifecycle"]["single_browser_post"]
        score["round_pass"] = all(score[key] for key in ("business_pass", "admission_pass")) and (
            score["lifecycle"]["pass"] and not score["failure"])
        score["boundary"] = boundary(sample, score, requests, reference_tools)
        score["scope"] = "r3_fixed_prefix" if name in REPLAYS else "end_to_end"
        rows.append(score)
        deviation = reproduce_first_deviations([score], sdk, cases).get(name)
        if deviation:
            repro[name] = {"first_trace_rejection": deviation}
        failures = [row for row in requests if row["response"].get("failure_reason")]
        if failures:
            repro.setdefault(name, {})["first_sdk_failure"] = {
                "sdk": failures[0], "model_result_trace": [
                    s for s in (sample.get("trace") or {}).get("steps", []) if s["stage"] == "model_result"
                    and s.get("runtime_call_id") == failures[0]["model_call_id"]]}
        if not score["round_pass"] or score["boundary"]["status"] == "failed":
            attempt = score["boundary"].get("sdk_attempt")
            relevant = next((row for row in requests if row["attempt"] == attempt), requests[-1] if requests else None)
            repro.setdefault(name, {}).update({"final_relevant_sdk": relevant,
                                              "guard": score["admission_decisions"],
                                              "boundary_trace": trace_steps(sample, FINAL, "opened")
                                              + trace_steps(sample, FINAL, "result"),
                                              "business_issues": score["business_issues"],
                                              "admission_issues": score["admission_issues"]})
    full = [row for row in rows if row["scope"] == "end_to_end"]
    supplements = [row for row in rows if row["scope"] == "r3_fixed_prefix"]
    targets = {row["target"] for row in rows if row["boundary"]["status"] == "passed"}
    report = {"task": "TASK-027-T5A", "ledger": ledger, "sdk_http_statuses": Counter(
        str(row["response"].get("status", "no_http_response")) for row in sdk),
        "full_pass": len(full) == len(FULL) and all(row["round_pass"] for row in full),
        "boundary_pass": targets == {101, 102}, "boundary_verified_targets": sorted(targets),
        "full": full, "supplements": supplements, "unexecuted": skipped}
    write(folder / "audit.json", report)
    write(folder / "repro.json", repro)
    return 0 if report["full_pass"] and report["boundary_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
