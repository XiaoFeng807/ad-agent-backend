"""Freeze, preflight and run the three real Chromium/GLM independent review cases."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from scripts import run_task27_t5 as t5, run_task27_t6 as t6
from scripts.run_real_diagnosis import CheckFailed, Redactor, require
from scripts.task27_t3_fixture import ARTIFACT, ROOT, digest, write


ARCHIVE = ROOT / "logs/task27-r5"
LEDGER = ARCHIVE / "glm-sdk-ledger.sqlite3"
TARGETS = {"review-branch-1": 101, "review-swap-1": 102, "review-gap-1": None}
CHECKS = (
    "frozen_browser_text", "sdk_ledger", "lifecycle", "review_business_evidence", "review_admission",
    "review_worker_chain_or_concrete_gap", "service_completion_status",
)


def frozen_fixture():
    original = t6.frozen_fixture()
    base = t5.read(t5.FROZEN)
    missing = next(case for case in base["cases"] if case["id"] == "gap-1")
    message = missing["message"] + t6.SUFFIX
    cases = [*original["cases"][1:], {**missing, "id": "review-gap-1", "message": message,
                                    "message_sha256": sha256(message.encode()).hexdigest()}]
    return {**original, "task": "TASK-027-R5", "cases": cases, "sql_targets": TARGETS,
            "checks": list(CHECKS), "rule": "Two positive worker chains and one concrete gap must all pass."}


def confirm_preflight(path, frozen):
    summary = t5.read(path / "summary.json")
    require(summary.get("browser_preflight") and summary["sdk_attempts"] == 0
            and t5.read(path / "frozen.json") == frozen, "r5_zero_request_preflight_required")
    samples = t5.read(path / "rounds.json")
    require([r["id"] for r in samples] == list(TARGETS), "preflight_cases_differ")
    require(all(sample["captured_without_stream"] and sample["request"]["message"] == case["message"]
                for sample, case in zip(samples, frozen["cases"])), "preflight_browser_text_drift")


def main(*, archive=ARCHIVE, task="TASK-027-R5"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--verified-preflight", type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.ledger = archive / LEDGER.name
    require(args.output.is_relative_to(archive), f"output_must_be_inside_{archive.name}_archive")
    frozen = frozen_fixture()
    if not args.preflight:
        confirm_preflight(args.verified_preflight, frozen)
    args.output.mkdir(parents=True, exist_ok=False)
    protected = [ROOT / "ad_agent.db", ARTIFACT, t5.FROZEN, t5.LEDGER, t5.DEEPSEEK_LEDGER, t6.LEDGER,
                 ROOT / "logs/task27-r4/glm-sdk-ledger.sqlite3", ROOT / "fast_server.py",
                 *sorted(path for path in t6.ARCHIVE.rglob("*") if path.is_file()),
                 *sorted(path for path in (ROOT / "backend").rglob("*") if path.suffix in {".py", ".html"})]
    if archive != ARCHIVE:
        protected += [LEDGER, *sorted(path for folder in ("preflight-final", "live")
                                      for path in (ARCHIVE / folder).rglob("*.json"))]
    before = {str(path.relative_to(ROOT)): digest(path) for path in protected}
    summary = {"task": task, "mode": "preflight" if args.preflight else "live", "run_id": uuid4().hex,
               "sdk_limit": t6.T6Audit.run_cap, "round_sdk_limit": t5.CASE_CAP,
               "selected": list(TARGETS), "storage_before": before}
    if args.verified_preflight:
        summary["verified_preflight"] = str(args.verified_preflight.resolve())
    redactor = Redactor()
    write(args.output / "frozen.json", frozen)
    try:
        with (TemporaryDirectory(dir=args.output, prefix="isolated-") as scratch,
              open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink)):
            t5.run(args, frozen, frozen["cases"], Path(scratch), redactor, summary, audit_class=t6.T6Audit,
                   expected_targets=TARGETS, install_observers=t6.install_transport_observer, browser_preflight=True)
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
