"""Run only the twelfth request against GLM for each isolated Chromium replay."""

import argparse
from collections import Counter
from contextlib import closing, redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from unittest.mock import patch
from uuid import uuid4

import httpx

from scripts import run_task27_t5 as t5
from scripts.audit_task27_t5 import evaluate
from scripts.audit_task27_t5a import linked_detail, trace_steps
from scripts.run_real_diagnosis import CheckFailed, Redactor, require
from scripts.run_task27_t5a import LastRequestReplay, REPLAYS, model_steps
from scripts.task27_t3_fixture import ARTIFACT, ROOT, digest, write
from scripts.verify_task27_r3 import FINAL, T5A, verify_final_payload


LEDGER = ROOT / "logs/task27-r4/glm-sdk-ledger.sqlite3"
TARGETS = {"replay-branch-1": 101, "replay-swap-1": 102}


def accepted_evidence(rows, case):
    return [row for row in rows if row["case"] == case and row["accepted"] and row["status"] == "succeeded"
            and row["source"] in {"get_plan_metrics_by_date", "search_knowledge"}]


class FinalAudit(t5.GLMAudit):
    run_cap = 2

    def case_cap(self, case):
        return 1

    def request(self, request):
        require(self.model_link.get().get("step") == FINAL and self.case in TARGETS, "unexpected_live_step")
        body = json.loads(request.content)
        assert [tool["function"]["name"] for tool in body["tools"]] == [t5.DETAIL]
        assert body["tool_choice"] == "auto"
        verify_final_payload(body["messages"], TARGETS[self.case], accepted_evidence(self.receipts, self.case))
        super().request(request)

    def record_transport_error(self, request, error):
        if attempt := request.extensions.get("task27_t3_attempt"):
            chain = []
            while error:
                chain.append({"type": type(error).__name__, "errno": getattr(error, "errno", None),
                              "winerror": getattr(error, "winerror", None)})
                error = error.__cause__
            result = {"failure_reason": "no_http_response", "end_ns": perf_counter_ns(),
                      "exception_chain": chain, "timeout": request.extensions.get("timeout")}
            with closing(sqlite3.connect(self.ledger)) as conn, conn:
                conn.execute("UPDATE sdk SET response=? WHERE id=? AND response IS NULL",
                             (json.dumps(result), attempt))


class FinalReplay(LastRequestReplay):
    def install(self, stack, provider, audit):
        super().install(stack, provider, audit)
        client = provider._client._client
        send = client.send

        def observe(request, **kwargs):
            try:
                return send(request, **kwargs)
            except httpx.TransportError as error:
                audit.record_transport_error(request, error)
                raise

        stack.enter_context(patch.object(client, "send", observe))


def score_round(sample, case, oracle, frozen, sdk):
    score = evaluate(sample, case, oracle, frozen, sdk)
    name, target = sample["id"], oracle["target"]
    opened = trace_steps(sample, FINAL, "opened")
    outcomes = trace_steps(sample, FINAL, "result")
    steps = model_steps(sample)
    replay = sample.get("replay", {})
    prefix = replay.get("fixed_model_calls", [])
    wire = sdk[0] if len(sdk) == 1 else {}
    model_ids = [step["runtime_call_id"] for step in steps]
    chain = linked_detail(sample, score, sdk)
    request_evidence = None
    if wire:
        request_evidence = verify_final_payload(
            wire["body"]["messages"], target, accepted_evidence(sample["admission"], name))
    checks = {
        "twelfth_request": len(model_ids) == 12 and len(prefix) == 11 and not replay.get("fixed_model_errors")
        and steps[-1]["action"] == wire.get("step") == FINAL and wire.get("model_call_id") == model_ids[-1]
        and Counter(row["model_call_id"] for row in prefix) == Counter(model_ids[:-1]),
        "http_200": wire.get("response", {}).get("status") == 200 and wire.get("sdk_retry") == 0
        and wire.get("response", {}).get("body", {}).get("model") == t5.MODEL,
        "approved_evidence": bool(request_evidence and len(opened) == 1
                                  and opened[0]["budget"]["model"] == 1 and opened[0]["budget"]["tool"] > 0
                                  and opened[0]["evidence"]["request_evidence"] == request_evidence),
        "rule_summary_budget_trace": any(step.get("error_code") == "model_budget_exhausted"
                                         and step.get("detail") == "rule-check" for step in sample["trace"]["steps"])
        if sample.get("trace") else False,
        "structured_detail": bool(chain and len(outcomes) == 1 and outcomes[0]["evidence"] == {
            "actions": [{"tool_call_id": chain["call"]["id"], "tool": t5.DETAIL, "success": True,
                         "error_code": None, "runtime_call_id": chain["runtime_call_id"]}],
            "detail_artifact_ids": [chain["detail_artifact_id"]],
        }),
        "business": score["business_pass"] and score["admission_pass"],
        "history": score["lifecycle"]["pass"] and sample.get("stream_posts") == 1 and not score["failure"],
    }
    return {**score, "status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "detail_chain": chain, "request_evidence": request_evidence, "sdk_response": wire.get("response")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    require(__debug__, "replay_requires_assertions")
    args.output = args.output.resolve()
    protected_roots = (t5.LEDGER.parent, T5A.parent)
    require(not any(args.output.is_relative_to(folder) for folder in protected_roots), "output_inside_prior_archive")
    args.output.mkdir(parents=True, exist_ok=False)
    args.ledger, args.preflight = LEDGER, False
    frozen = t5.read(t5.FROZEN)
    require({**frozen, "frozen_utc": None} == t5.frozen_fixture(), "t5_frozen_fixture_mismatch")
    cases = {case["id"]: case for case in frozen["cases"]}
    selected = {name: {**cases[original], "id": name} for name, original in REPLAYS.items()}
    protected = (ROOT / "ad_agent.db", ARTIFACT, t5.LEDGER, T5A / "glm-sdk-ledger.sqlite3")
    before = {str(path): digest(path) for path in protected}
    redactor = Redactor()
    summary = {"task": "TASK-027-R4", "run_id": uuid4().hex, "sdk_limit": 2, "status": "failed",
               "storage_before": before}
    try:
        with (TemporaryDirectory(dir=args.output, prefix="isolated-") as scratch,
              open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink)):
            t5.run(args, frozen, list(selected.values()), Path(scratch), redactor, summary,
                   audit_class=FinalAudit, replay=FinalReplay(live=True))
        samples, sdk, oracles = (t5.read(args.output / file)
                                 for file in ("rounds.json", "sdk.json", "sql-oracle.json"))
        scores = [score_round(sample, selected[name], oracles[name], frozen, [row for row in sdk if row["case"] == name])
                  for sample in samples if (name := sample["id"]) in selected]
        write(args.output / "audit.json", redactor.clean(scores))
        summary["status"] = "passed" if len(scores) == 2 and all(row["status"] == "passed" for row in scores) else "failed"
    except Exception as error:
        summary["failure"] = str(error) if isinstance(error, CheckFailed) else type(error).__name__
    finally:
        summary["storage_after"] = {str(path): digest(path) for path in protected}
        write(args.output / "summary.json", redactor.clean(summary))
        require(summary["storage_after"] == before, "protected_storage_changed")
    return int(summary["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
