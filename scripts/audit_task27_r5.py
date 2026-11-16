"""Score R5 against the frozen browser, SQL and independent-worker evidence chain."""

import argparse
from collections import Counter
from contextlib import closing
from functools import partial
import json
from pathlib import Path
import sqlite3

from scripts import audit_task27_t6 as t6
from scripts.audit_task27_t5 import object_or_empty
from scripts.audit_task27_t5a import trace_steps
from scripts.run_task27_r5 import CHECKS, LEDGER, TARGETS, frozen_fixture
from scripts.run_task27_t5 import CASE_CAP, DETAIL, ENDPOINT, MODEL, read
from scripts.task27_t3_fixture import write


def prove_worker(sample, score, sdk):
    if not score["worker_handoff_verified"]:
        return None
    chain = score["detail_chain"]
    worker_id = score["actual_detail_worker"]
    transfer = next(row for row in score["worker_transfers"] if row["worker_id"] == worker_id)
    first = next(row for row in sdk if row["worker_id"] == worker_id)
    action = next(row for row in sdk if row["attempt"] == chain["sdk_attempt"])
    context = transfer["first_input"]
    inputs = [item for rows in context["inputs"].values() for item in rows]
    adopted = transfer["matching_artifacts"]
    proof = chain["admission"]["evidence"].get("review_input", {})
    scope = {"worker_id": worker_id, "run_id": score["lifecycle"]["run_id"],
             "conversation_id": score["lifecycle"]["group_id"]}
    ids = [item["artifact_id"] for item in inputs]
    model_inputs = [step for step in trace_steps(sample, "model_call", "team_worker")
                    if step.get("runtime_call_id") == first["model_call_id"]
                    and step.get("parent_call_id") == worker_id]
    if not (len(model_inputs) == 1 and len(adopted) == len(inputs) == 5
            and {item["artifact_id"] for item in adopted} == set(ids)
            and transfer["verified_windows"] == 4 and len(transfer["verified_rule_artifact_ids"]) == 1
            and not context.get("direct_inputs")
            and proof == {**scope, "model_call_id": first["model_call_id"],
                          "action_model_call_id": action["model_call_id"], "input_artifact_ids": ids}
            and chain["admission"]["parent_call_id"] == worker_id
            and chain["arguments"] == {"plan_id": score["target"], "user_id": 17}
            and action["worker_id"] == worker_id):
        return None
    recorded = model_inputs[0]["evidence"]
    if recorded != {**scope, "input_artifact_ids": ids,
                    "producers": [{key: item[key] for key in ("artifact_id", "task_id", "worker_id")}
                                  for item in inputs]}:
        return None
    for item in inputs:
        source = next(row for row in adopted if row["artifact_id"] == item["artifact_id"])
        if item["worker_id"] != source["producer_worker_id"] or item["task_id"] != source["producer_task_id"]:
            return None
    return {**scope, "first_sdk_attempt": first["attempt"], "model_call_id": first["model_call_id"],
            "evidence": adopted, **chain}


def normalize_r6_task(item, call, *, sdk):
    """Reconstruct the internal task from the stage advertised on the wire."""
    request = next(row for row in sdk if row["attempt"] == call["attempt"])
    tools = [tool["function"] for tool in request["body"].get("tools", [])
             if tool["function"]["name"] == "delegate_tasks"]
    if len(tools) != 1 or not isinstance(item, dict):
        return None
    definitions = tools[0]["parameters"].get("$defs", {})
    properties = definitions.get("TeamTask", {}).get("properties", {})
    legacy = {"input_refs", "direct_refs"}
    if properties.keys() & legacy or any(item.get(field, []) != [] for field in legacy):
        return None
    item = {key: value for key, value in item.items() if key not in legacy}
    if "dependencies" in properties and "artifact_refs" not in properties:
        if item.pop("artifact_refs", []) != []:
            return None
        if item.get("capabilities") == [DETAIL]:
            deps = item.get("dependencies", [])
            batch_ids = {task["task_id"] for task in call["task_sheet"]["tasks"]}
            if not deps or not set(deps) <= batch_ids - {item["task_id"]}:
                return None
            item["input_refs"] = deps
    elif "artifact_refs" in properties and "dependencies" not in properties:
        choices = definitions["ArtifactRef"]["properties"]
        options = next((state["artifact_refs"] for message in reversed(request["body"]["messages"])
                        if message["role"] == "tool"
                        and (state := object_or_empty(message["content"]).get("independent_review", {}))
                        and state["status"] == "review_required"), [])
        if ("dependencies" in item or any(
            set(choices[key]["enum"]) != {row[key] for row in options} for key in ("task_id", "artifact_id")
        )):
            return None
        refs = item.get("artifact_refs", [])
        if any(ref not in options for ref in refs) or (item.get("capabilities") == [DETAIL]
                                                       and any(ref not in refs for ref in options)):
            return None
        item["dependencies"] = list(dict.fromkeys(ref["task_id"] for ref in refs))
    else:
        return None
    return item


def score_case(sample, case, sql, frozen, sdk, *, r6=False):
    normalize_task = partial(normalize_r6_task, sdk=sdk) if r6 else None
    score = t6.score_round(sample, case, sql, frozen, sdk, normalize_task=normalize_task)
    proof = prove_worker(sample, score, sdk)
    detail_reply = trace_steps(sample, "conditional_detail", "reply")
    final_status = detail_reply[0]["evidence"].get("independent_review_status") if len(detail_reply) == 1 else None
    gap_case = sql["target"] is None
    if gap_case:
        handoff = bool(score["gap_exercised"] and not score["detail_reads"] and not score["detail_success_receipts"]
                       and not any(s["action"] == "allowed" for s in score["admission_decisions"]))
        complete = final_status == "evidence_pending" and "独立复核未完成" in sample["answer"]
    else:
        handoff = bool(proof)
        complete = final_status == "completed" and "独立复核已完成" in sample["answer"]
    checks = dict(zip(CHECKS, (
        sample.get("request", {}).get("message") == case["message"],
        bool(sdk) and len(sdk) <= CASE_CAP and all(
            r["endpoint"] == ENDPOINT + "/chat/completions" and r["body"]["model"] == MODEL
            and r["trace_id"] == score["lifecycle"]["trace_id"] for r in sdk),
        score["lifecycle_verified"] and score["history_matches_terminal"],
        score["business_pass"], score["admission_pass"], handoff, complete,
    )))
    deviation = None
    if not all(checks.values()):
        deviation = t6.first_deviation(score, sample, case, sdk) or {
            "reason": next(key for key, passed in checks.items() if not passed),
            "action": score["detail_attempts"] or score["task_sheets"],
        }
    return {**score, "checks": checks, "passed": all(checks.values()), "independent_worker_chain": proof,
            "service_review_status": final_status, "first_deviation": deviation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args()
    summary, frozen, oracles, samples, sdk = (
        read(args.run / f"{name}.json") for name in ("summary", "frozen", "sql-oracle", "rounds", "sdk"))
    assert frozen == frozen_fixture() and [s["id"] for s in samples] == list(TARGETS)
    with closing(sqlite3.connect(args.ledger.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        persisted = [{"attempt": attempt, "run": run, "case": case, **json.loads(request),
                      "response": json.loads(response) if response else {"failure_reason": "no_http_response"}}
                     for attempt, run, case, request, response in conn.execute(
                         "SELECT id,run,sample,request,response FROM sdk ORDER BY id")]
    counts = Counter(row["case"] for row in persisted)
    assert summary["sdk_attempts"] == len(sdk) and [row for row in persisted if row["run"] == summary["run_id"]] == sdk
    assert len(persisted) <= 36 and set(counts) <= TARGETS.keys() and all(n <= CASE_CAP for n in counts.values())
    scores = [score_case(sample, case, oracles[case["id"]], frozen, [r for r in sdk if r["case"] == case["id"]],
                         r6=summary["task"] == "TASK-027-R6")
              for sample, case in zip(samples, frozen["cases"])]
    report = {"task": summary["task"], "passed": all(row["passed"] for row in scores), "rounds": scores,
              "sdk_attempts": len(persisted), "attempts_by_case": dict(counts),
              "http_statuses": dict(Counter(str(r["response"].get("status")) for r in persisted)),
              "sdk_retries": sum(r["sdk_retry"] > 0 for r in persisted)}
    write(args.run / "audit.json", report)
    write(args.run / "repro.json", {row["id"]: {"failed_checks": [key for key, ok in row["checks"].items() if not ok],
                                               "first_deviation": row["first_deviation"]}
                                   for row in scores if not row["passed"]})
    for row in scores:
        print(row["id"], "PASS" if row["passed"] else "FAIL", row["sdk_attempts"],
              [key for key, ok in row["checks"].items() if not ok])
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
