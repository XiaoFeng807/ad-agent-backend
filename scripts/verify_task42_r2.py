"""Replay the frozen T3 conditional questions and audit the Listing read chain."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from scripts import verify_task42_t3 as t3
from scripts.accept_task42_t1 import configure, read, write
from scripts.verify_task42_r1 import FROZEN, measure_reads


COMPLETE, GAP = "conditional-complete", "conditional-gap"
ARCHIVE = FROZEN.parent / "live/paired-dynamic"
READS = (t3.t2.SALES, t3.t2.LISTING, t3.t2.FACTS)
FROZEN_HASH = "2e057142130840cd8ac4c885ecac4626ab1966b3ef3d3deae3b42d3f1c4aa35f"


class ArchivedActions:
    name = "Mock"

    def __init__(self, *, paired):
        self.paired = paired
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def start_case(self, case):
        replies = [deepcopy(row["response"]["body"]["choices"][0]["message"])
                   for row in read(ARCHIVE / case["id"] / "audit.json")["sdk"]]
        if self.paired and case["id"] == COMPLETE:
            replies[1]["tool_calls"].extend(replies.pop(2)["tool_calls"])
        self.replies = iter(replies)

    def create(self, **_payload):
        reply = next(self.replies, {"content": "", "tool_calls": []})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=reply.get("content"), tool_calls=reply.get("tool_calls", [])))])


def boundary_case(client, model, case, folder, password, injection):
    from backend import observability
    from backend.database import database, listing_operations as listings

    path = folder / "sandbox.sqlite3"
    captures = {key: [] for key in ("reads", "receipts", "batches", "sdk")}
    reached, released = Barrier(2, timeout=20), Barrier(2, timeout=20)
    original = listings.read_facts
    facts_calls = []

    def read_facts(*args):
        facts_calls.append(list(args[1:]))
        if len(facts_calls) == 1:
            if injection == "facts-read-failure":
                raise sqlite3.DatabaseError("task42_r2_injected_facts_read_failure")
            value = original(*args)
            reached.wait()
            released.wait()
            return value
        return original(*args)

    with ExitStack() as stack:
        stack.enter_context(patch.object(database.settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(path)))
        for attr, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                               ("FUNC_LOG_FILE", "functions.jsonl")):
            stack.enter_context(patch.object(observability, attr, folder / filename))
        t3.seeded(database, listings, password, case, None)
        before = t3.t1.snapshot(path)
        headers = t3.t1.login(client, t3.t2.OWNER, password)
        model.start_case(case)
        stack.enter_context(patch.object(listings, "read_facts", read_facts))
        t3.t1.capture_runtime(stack, captures, listings, model, None)
        mutation = None
        if injection == "facts-read-failure":
            response = t3.t1.http_round(client, headers, injection, case["message"])
            after_injection = before
        else:
            table = {"listing-version": "listing_test_products", "facts-version": "listing_test_facts"}[injection]
            mutation = f"UPDATE {table} SET version=version+1 WHERE shop_id=? AND marketplace_id=? AND sku=?"
            with ThreadPoolExecutor(max_workers=1) as executor:
                stream = executor.submit(t3.t1.http_round, client, headers, injection, case["message"])
                reached.wait()
                try:
                    with closing(sqlite3.connect(path)) as conn, conn:
                        if conn.execute(mutation, tuple(t3.TARGET.values())).rowcount != 1:
                            raise AssertionError("Mutation did not affect exactly one row")
                    after_injection = t3.t1.snapshot(path)
                finally:
                    released.wait()
                response = stream.result(timeout=20)
        terminal = [event for event in response["events"] if event["type"] in ("done", "error", "stopped")]
        evidence_response = client.get(f"{response['base']}/runs/{terminal[-1]['run_id']}/evidence", headers=headers)
        evidence = t3.t1.body(evidence_response)
        history = t3.t1.edge_history(client, headers, response["base"])
        after = t3.t1.snapshot(path)

    trace_id = terminal[-1].get("trace_id")
    traces = t3.t1.read_traces(folder)
    steps = next((trace["steps"] for trace in traces if trace["trace_id"] == trace_id), [])
    blocked = READS[1] if injection == "listing-version" else READS[2]
    checks = {
        "fault_injected": before != after_injection if mutation else any(
            step["stage"] == "tool_result" and step["action"] == blocked and step["status"] == "error" for step in steps),
        "both_dependencies_attempted_together": {call["function"]["name"]
                                                 for call in t3.sdk_calls(captures["sdk"][1:2])} == set(READS[1:]),
        "gap_returned": [event["type"] for event in terminal] == ["done"] and evidence_response.status_code == 200
        and blocked in evidence["gaps"] and blocked not in evidence["artifacts"],
        "draft_never_called": not any(row["step"] == "listing_draft" for row in captures["sdk"]),
        "zero_history_actions": history["history_status"] == 200 and all(
            not row.get("listing_actions") for row in history["history"]["entries"]),
        "zero_proposals_and_receipts": not after["listing_change_proposals"] and not after["listing_change_receipts"],
        "no_additional_listing_writes": after_injection == after,
    }
    result = {"passed": all(checks.values()), "trace_id": trace_id, "checks": checks}
    write(folder / "audit.json", {"summary": result, "case": case, "injection": injection, "sql_mutation": mutation,
                                 "response": response, "evidence": evidence, "history": history, "runtime": captures,
                                 "facts_reader_calls": facts_calls, "sql_before": before,
                                 "sql_after_injection": after_injection, "sql_after": after})
    return result


def boundaries(output, case):
    from fastapi.testclient import TestClient

    configure(output / "bootstrap.sqlite3")
    from backend.agent.llm_provider import ProviderFactory
    from backend.memory import conversation_memory

    model = ArchivedActions(paired=True)
    password = token_urlsafe(24)
    cases = {}
    with (patch.object(ProviderFactory, "create", return_value=model),
          patch.object(conversation_memory, "store_conversation")):
        from fast_server import app

        with TestClient(app) as client:
            for injection in ("listing-version", "facts-version", "facts-read-failure"):
                folder = output / injection
                folder.mkdir()
                cases[injection] = boundary_case(client, model, case, folder, password, injection)
    passed = all(result["passed"] for result in cases.values())
    write(output / "report.json", {"passed": passed, "cases": cases})
    return int(not passed)


def audit_case(audit, expected_requests):
    case, sdk = audit["case"], audit["sdk"]
    complete = case["id"] == COMPLETE
    evidence = audit["response"].get("evidence") or {}
    reads = audit["reader_invocations"]
    business = Counter(row["read"] for row in reads if row["phase"] == "business_read")
    steps = audit["trace"].get("steps", [])
    calls = {call["id"]: call["function"] for call in t3.sdk_calls(sdk)}
    draft = next((json.loads(row["body"]["messages"][-1]["content"]) for row in sdk
                  if row["step"] == "listing_draft"), {})
    chains = {}
    for name, ref in evidence.get("artifacts", {}).items():
        result = next((row for row in steps if row["stage"] == "tool_result" and row["action"] == name
                       and row.get("runtime_call_id") == ref["runtime_call_id"]), {})
        key = t3.t1.READ_KEYS[name]
        value = evidence[key]
        provider_id = result.get("tool_call_id")
        function = calls.get(provider_id, {})
        chains[name] = {**ref, "model_tool_call_id": provider_id, "function": function,
                        "source": value["source"], "version": value["version"],
                        "in_draft_input": draft.get(key) == value and draft.get("artifacts", {}).get(name) == ref}
    expected_tools = set(READS if complete else READS[:1])
    metrics = evidence.get("metrics") or {}
    checks = {
        "frozen_business_rubric": audit["summary"]["passed"],
        "requests": expected_requests is None or len(sdk) == expected_requests,
        "initial_decision_offers_only_sales": bool(sdk) and [tool["function"]["name"]
                                                               for tool in sdk[0]["body"].get("tools", [])] == [READS[0]],
        "exact_business_reads": business == Counter(("read_sales", "read_listing", "read_facts") if complete
                                                     else ("read_sales",)),
        "distinct_artifact_and_call_ids": set(chains) == expected_tools and all(
            len({row[key] for row in chains.values()}) == len(expected_tools) and
            all(row[key] for row in chains.values()) for key in ("artifact_id", "runtime_call_id", "model_tool_call_id")),
        "actual_model_tool_arguments": len(calls) == len(expected_tools) and all(
            row["function"].get("name") == name and json.loads(row["function"]["arguments"]) == {"sku": t3.t2.SKU}
            for name, row in chains.items()),
    }
    if complete:
        checks["draft_used_all_three_artifacts"] = all(row["in_draft_input"] for row in chains.values())
        if expected_requests == 3:
            checks["dependency_calls_in_one_decision"] = len(sdk) == 3 and [row["step"] for row in sdk] == [
                "listing_operations", "listing_operations", "listing_draft"] and {
                call["function"]["name"] for call in t3.sdk_calls(sdk[1:2])} == set(READS[1:])
            checks["dependency_decision_preserves_delegation"] = len(sdk) >= 2 and {
                tool["function"]["name"] for tool in sdk[1]["body"].get("tools", [])} == {*READS[1:], "delegate_tasks"}
            checks["dependency_decision_instructs_same_response"] = len(sdk) >= 2 and all(
                fragment in sdk[1]["body"]["messages"][0]["content"] for fragment in (
                    "complete sales in both windows", "in this single response", *READS[1:]))
    else:
        checks.update(unknown_total=(metrics.get("current") or {}).get("units", -1) is None,
                      no_dependent_queries=all(row["read"] == "read_sales" for row in reads),
                      no_draft=not draft and evidence.get("proposal") is None,
                      unchanged_listing_sql=audit["sql_before"] == audit["sql_after"])
    return {"passed": all(checks.values()), "checks": checks, "requests": len(sdk),
            "business_reads": business, "reads_by_phase": Counter(row["phase"] for row in reads),
            "reads_by_function": Counter(row["read"] for row in reads), "chains": chains,
            "totals": {key: metrics.get(key, {}).get("units") for key in ("baseline", "current")},
            **{key: audit["summary"].get(key) for key in ("trace_id", "tokens", "sdk_retries", "seconds",
                                                        "trace_seconds", "sse_seconds", "delegation_calls")}}


def run(stage, root, name, repeats):
    if sha256(FROZEN.read_bytes()).hexdigest() != FROZEN_HASH:
        raise RuntimeError("T3 frozen input hash changed")
    frozen = read(FROZEN)
    selected = [case for case in frozen["paired"] if case["id"] in (COMPLETE, GAP)]
    live = stage.endswith("live")
    baseline = stage.startswith("baseline")
    if stage == "live" and not all(read(root / path)["passed"] for path in (
            "fixed/conditional.json", "boundaries-loopback/report.json", "approval/fixed/summary.json")):
        raise RuntimeError("Fixed replay, boundary and approval regressions must pass before live verification")
    output = root / name
    output.mkdir(parents=True, exist_ok=False)
    (output / "frozen.json").write_bytes(FROZEN.read_bytes())
    if stage == "boundaries":
        return boundaries(output, next(case for case in selected if case["id"] == COMPLETE))
    schedule = [(f"round-{index}", {**case, **({"browser_approval": True}
                 if stage == "live" and index == 1 and case["id"] == COMPLETE else {})}, "dynamic")
                for index in range(1, repeats + 1) for case in selected]
    prior = [path for path in root.rglob("glm-sdk-ledger.sqlite3")] if live else []
    result = t3.execute("live" if live else "fixed", output, frozen, output, prior, schedule=schedule,
                        reader_probe=measure_reads, fixed_provider=None if live else ArchivedActions(paired=not baseline))
    cases = {}
    for group, case, _ in schedule:
        if (path := output / group / case["id"] / "audit.json").exists():
            expected = 1 if case["id"] == GAP else (None if stage == "baseline-live" else 4 if baseline else 3)
            cases[f"{group}/{case['id']}"] = audit_case(read(path), expected)
    passed = result == 0 and len(cases) == len(schedule) and all(case["passed"] for case in cases.values())
    write(output / "conditional.json", {"passed": passed, "cases": cases})
    return int(not passed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("baseline-fixed", "baseline-live", "fixed", "boundaries", "live"))
    parser.add_argument("--output-dir", type=Path, default=Path("logs/task42-r2"))
    parser.add_argument("--run-name")
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=1)
    args = parser.parse_args()
    return run(args.stage, args.output_dir.resolve(), args.run_name or args.stage, args.repeats)


if __name__ == "__main__":
    raise SystemExit(main())
