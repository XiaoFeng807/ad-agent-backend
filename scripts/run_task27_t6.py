"""Run the frozen T6 matrix from the Chromium work group with real GLM responses."""

import argparse
from contextlib import closing, redirect_stderr, redirect_stdout
from hashlib import sha256
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
from scripts.run_real_diagnosis import CheckFailed, Redactor, require
from scripts.task27_t3_fixture import ARTIFACT, ROOT, digest, write

ARCHIVE = ROOT / "logs/task27-t6"
LEDGER = ARCHIVE / "glm-sdk-ledger.sqlite3"
TARGETS = {"swap-1": 102, "review-branch-1": 101, "review-swap-1": 102}
SUFFIX = "  请安排不同的 Worker 分别完成取证和独立复核。复核 Worker 应依据收到的取证产物判断，并在清单确定后读取入选计划的详情。"


class T6Audit(t5.GLMAudit):
    run_cap = 36


def frozen_fixture():
    original = t5.read(t5.FROZEN)
    require({**original, "frozen_utc": None} == t5.frozen_fixture(), "t5_frozen_fixture_mismatch")
    cases = {case["id"]: case for case in original["cases"]}
    selected = [cases["swap-1"]]
    for name in ("branch-1", "swap-1"):
        case = cases[name]
        message = case["message"] + SUFFIX
        selected.append({**case, "id": f"review-{name}", "message": message,
                         "message_sha256": sha256(message.encode()).hexdigest()})
    branch, swap = selected[1:]
    require(branch["message"] == swap["message"], "review_prompts_differ")
    require(branch["bindings"] == {plan: swap["bindings"][other] for plan, other in (("101", "102"), ("102", "101"))},
            "review_bindings_not_swapped")
    require(branch["expected"]["rule"] == swap["expected"]["rule"], "review_rules_differ")
    require(all(
        [(w["start_date"], w["end_date"]) for w in branch["expected"]["metrics"][plan]["windows"]]
        == [(w["start_date"], w["end_date"]) for w in swap["expected"]["metrics"][plan]["windows"]]
        for plan in branch["bindings"]), "review_windows_differ")
    return {**original, "task": "TASK-027-T6", "t5_sha256": digest(t5.FROZEN), "sdk_limit": T6Audit.run_cap,
            "cases": selected, "sql_targets": TARGETS, "review_suffix": SUFFIX}


def recoverable_cases(previous, frozen):
    summary, samples = (t5.read(previous / name) for name in ("summary.json", "rounds.json"))
    require(summary["task"] == frozen["task"] and summary["preflight_sdk_attempts"] == 0,
            "recovery_requires_completed_preflight")
    cases = {case["id"]: case for case in frozen["cases"]}
    require([sample["id"] for sample in samples] == list(TARGETS), "recovery_requires_initial_matrix")
    require(samples[0].get("dom_replay_pass"), "initial_swap_did_not_complete")
    selected = []
    for sample in samples[1:]:
        case = cases[sample["id"]]
        require(sample.get("browser_failed") and sample.get("failure") == "chromium_did_not_verify_refresh"
                and not sample["sse"] and sample.get("http_status") is None
                and sample.get("request", {}).get("message") == case["message"], "unexpected_recovery_source")
        selected.append(case)
    return selected


def install_transport_observer(stack, provider, audit):
    # The client selects the transport, including any proxy mount, for this request.
    client = provider._client._client
    send = client._send_single_request

    def observe(request):
        events = []
        request.extensions["trace"] = lambda name, _: events.append(name)
        try:
            return send(request)
        except httpx.TransportError as error:
            attempt = request.extensions.get("task27_t3_attempt")
            if attempt:
                chain = []
                current = error
                while current is not None:
                    chain.append({"type": type(current).__name__, "errno": getattr(current, "errno", None),
                                  "winerror": getattr(current, "winerror", None)})
                    current = current.__cause__
                result = {"failure_reason": "no_http_response", "end_ns": perf_counter_ns(),
                          "request_stage": events[-1] if events else "http_transport",
                          "transport_events": events, "exception_chain": chain,
                          "timeout": request.extensions.get("timeout")}
                with closing(sqlite3.connect(audit.ledger)) as conn, conn:
                    conn.execute("UPDATE sdk SET response=? WHERE id=? AND response IS NULL",
                                 (json.dumps(result), attempt))
            raise

    stack.enter_context(patch.object(client, "_send_single_request", observe))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--recover-browser", type=Path, help="Continue the two review cases after a browser capture failure.")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.ledger = LEDGER
    require(args.output.is_relative_to(ARCHIVE), "output_must_be_inside_t6_archive")
    frozen = frozen_fixture()
    if args.recover_browser:
        previous = args.recover_browser.resolve()
        require(previous.is_relative_to(ARCHIVE), "recovery_source_must_be_inside_t6_archive")
        selected = recoverable_cases(previous, frozen)
    else:
        selected = frozen["cases"]
    targets = {case["id"]: TARGETS[case["id"]] for case in selected}
    args.output.mkdir(parents=True, exist_ok=False)
    protected = [ROOT / "ad_agent.db", ARTIFACT, t5.FROZEN, t5.BASE, t5.MANIFEST, t5.LEDGER, t5.DEEPSEEK_LEDGER,
                 ROOT / "logs/task27-t5a/live/glm-sdk-ledger.sqlite3", ROOT / "logs/task27-r4/glm-sdk-ledger.sqlite3",
                 ROOT / "fast_server.py", *sorted(p for p in (ROOT / "backend").rglob("*")
                                                  if p.suffix in {".py", ".html"})]
    before = {str(path.relative_to(ROOT)): digest(path) for path in protected}
    summary = {"task": frozen["task"], "mode": "preflight" if args.preflight else "live", "run_id": uuid4().hex,
               "sdk_limit": T6Audit.run_cap, "round_sdk_limit": t5.CASE_CAP, "selected": list(targets),
               "recovery_source": str(previous) if args.recover_browser else None,
               "storage_before": before}
    redactor = Redactor()
    write(args.output / "frozen.json", frozen)
    try:
        with (TemporaryDirectory(dir=args.output, prefix="isolated-") as scratch,
              open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink)):
            t5.run(args, frozen, selected, Path(scratch), redactor, summary, audit_class=T6Audit,
                   expected_targets=targets, install_observers=install_transport_observer,
                   resume_cases=bool(args.recover_browser))
        return 0
    except Exception as error:
        summary["failure"] = str(error) if isinstance(error, CheckFailed) else type(error).__name__
        print(summary["failure"])
        return 1
    finally:
        summary["storage_after"] = {str(path.relative_to(ROOT)): digest(path) for path in protected}
        write(args.output / "summary.json", redactor.clean(summary))
        require(summary["storage_after"] == before, "protected_storage_changed")


if __name__ == "__main__":
    raise SystemExit(main())
