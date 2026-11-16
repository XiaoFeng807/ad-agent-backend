"""Run the T5A Chromium matrix and conditionally give R3's last request to GLM."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from scripts import run_task27_t5 as t5, verify_task27_r3 as r3
from scripts.run_real_diagnosis import CheckFailed, Redactor, require
from scripts.task27_t3_fixture import ARTIFACT, ROOT, digest, write

FULL = ("branch-1", "swap-1", "gap-1")
REPLAYS = {f"replay-{name}": name for name in FULL[:2]}
CAPS = dict.fromkeys(FULL, 12) | dict.fromkeys(REPLAYS, 1)
RUN_CAP = sum(CAPS.values())


class T5AAudit(t5.GLMAudit):
    run_cap = RUN_CAP

    def case_cap(self, case):
        return CAPS[case]


def model_steps(sample):
    return [row for row in (sample.get("trace") or {}).get("steps", []) if row["stage"] == "model_call"]


class LastRequestReplay:
    def __init__(self, live, archived_reply=None):
        self.live = live
        self.archived_reply = archived_reply
        self.fixed = r3.FixedModel()
        self.final_link = None

    def install(self, stack, provider, audit):
        self.audit = audit
        completions_type = type(provider._client.chat.completions)
        create = completions_type.create

        def route(completions, **payload):
            if audit.case not in REPLAYS:
                return create(completions, **payload)
            link = audit.model_link.get()
            if link.get("step") != r3.FINAL:
                require(len(self.fixed.calls) < 11, "unexpected_r3_replay_prefix_length")
                return self.fixed.create(**payload)
            require(self.final_link is None and len(self.fixed.calls) == 11 and not self.fixed.errors,
                    "invalid_r3_replay_prefix")
            require(dict(self.fixed.indices) == {"main": 4, "metrics-101": 2, "metrics-102": 2, "rule-check": 3},
                    "r3_replay_prefix_drift")
            require([tool["function"]["name"] for tool in payload["tools"]] == [t5.DETAIL]
                    and payload.get("tool_choice") == "auto", "unexpected_r3_final_tools")
            self.final_link = link
            if self.archived_reply is not None:
                from openai.types.chat import ChatCompletion

                return ChatCompletion.model_validate(self.archived_reply)
            if not self.live:
                return self.fixed.create(**payload)
            return create(completions, **payload)

        stack.enter_context(patch.object(completions_type, "create", route))

    def skip(self, case, samples):
        original = next((s for s in samples if s["id"] == REPLAYS.get(case["id"])), None)
        steps = model_steps(original) if original else []
        return len(steps) == 12 and steps[-1]["action"] == r3.FINAL

    def prepare(self, case, expected):
        if name := REPLAYS.get(case["id"]):
            self.final_link = None
            self.fixed.prepare_round(case, name, self.audit, expected)

    def capture(self, sample):
        if sample["id"] in REPLAYS and "request" in sample:
            sample["replay"] = {
                "source": "TASK-027-R3 FixedModel / T5 branch-1 archived responses",
                "fixed_model_calls": self.fixed.calls[:11],
                "fixed_model_errors": self.fixed.errors,
                "final_model_link": self.final_link,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--fixed-check", action="store_true")
    modes.add_argument("--offline-reply", type=Path, help="Use the recorded GLM final response from this SDK export.")
    parser.add_argument("--case", choices=FULL[:2], help="Select the positive fixture for --offline-reply.")
    args = parser.parse_args()
    require(__debug__, "replay_requires_assertions")
    if bool(args.offline_reply) != bool(args.case):
        parser.error("--offline-reply and --case must be provided together")
    args.output = args.output.resolve()
    require(not args.output.is_relative_to(t5.LEDGER.parent), "output_must_be_outside_t5_archive")
    args.output.mkdir(parents=True)
    args.ledger = args.output / "glm-sdk-ledger.sqlite3"
    frozen = t5.read(t5.FROZEN)
    require({**frozen, "frozen_utc": None} == t5.frozen_fixture(), "t5_frozen_fixture_mismatch")
    cases = {case["id"]: case for case in frozen["cases"]}
    archived_reply = None
    if args.offline_reply:
        replay = f"replay-{args.case}"
        row, = [row for row in t5.read(args.offline_reply) if row["case"] == replay and row["step"] == r3.FINAL]
        archived_reply = row["response"]["body"]
        require(archived_reply["model"] == t5.MODEL, "recorded_final_response_model_mismatch")
        selected = [{**cases[args.case], "id": replay}]
        mode = "offline-reply"
    elif args.fixed_check:
        selected, mode = [], "fixed-check"
    else:
        selected = [cases[name] for name in FULL]
        mode = "preflight" if args.preflight else "live"
    if mode in {"live", "fixed-check"}:
        selected.extend({**cases[name], "id": replay} for replay, name in REPLAYS.items())
    protected = [ROOT / "ad_agent.db", ARTIFACT, t5.FROZEN, t5.BASE, t5.MANIFEST, t5.DEEPSEEK_LEDGER, t5.LEDGER,
                 *sorted(p for p in r3.ARCHIVE.rglob("*") if p.is_file()),
                 *sorted(p for p in (ROOT / "backend").rglob("*") if p.suffix in {".py", ".html"}),
                 ROOT / "fast_server.py"]
    before = {str(path.relative_to(ROOT)): digest(path) for path in protected}
    summary = {"task": "TASK-027-T5A", "run_id": uuid4().hex, "mode": mode,
        "sdk_limit": RUN_CAP, "sample_limits": CAPS, "selected": [case["id"] for case in selected],
        "storage_before": before}
    redactor = Redactor()
    write(args.output / "frozen.json", frozen)
    try:
        with (TemporaryDirectory(dir=args.output, prefix="isolated-") as scratch,
              open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink)):
            t5.run(args, frozen, selected, Path(scratch), redactor, summary, audit_class=T5AAudit,
                   replay=LastRequestReplay(live=mode == "live", archived_reply=archived_reply))
        return 0
    except Exception as error:
        summary["failure"] = str(error) if isinstance(error, CheckFailed) else type(error).__name__
        print(summary["failure"], file=sys.stderr)
        return 1
    finally:
        summary["storage_after"] = {str(path.relative_to(ROOT)): digest(path) for path in protected}
        write(args.output / "summary.json", redactor.clean(summary))
        require(summary["storage_after"] == before, "protected_storage_changed")


if __name__ == "__main__":
    raise SystemExit(main())
