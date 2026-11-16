"""Run the TASK-045-T2 paired Listing acceptance through Edge and real SSE."""

import argparse
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from functools import wraps
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from statistics import median
from threading import Event, Thread
from time import perf_counter_ns, sleep
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from scripts import accept_task45_t1 as r1
from scripts import task45_t1_fixture as fixture
from scripts import task45_t2_faults as faults
from scripts.accept_task42_t1 import configure, read, serve, write
from scripts.run_real_diagnosis import Redactor
from scripts.run_task27_t5 import ENDPOINT, GLMAudit, MODEL, browser_env
from scripts.task45_t2_observe import FixedModel, Observation, calls, phase, verify_handoffs


CASES = [case for case in fixture.cases()
         if case["id"] in {"simple-tote", "simple-riser", "base", "swap", "missing-fact"}]
READERS = {r1.SALES: "read_sales", r1.FACTS: "read_facts", r1.CURRENT: "read_listing"}
RULES = {
    "business": r1.RULES["last_week"] + " " + r1.RULES["comparison"] + " " + r1.RULES["draft"],
    "simple": "The selected SKU yields 67 or 74 units. Exactly one actual sales read, zero Workers and proposals.",
    "missing_fact": "Rank all three by SQL, retain the winner and its sales, name main compartment capacity, "
                    "and require zero approval, draft invocation, proposal, execution receipt and product change.",
    "write": "Observe pending proposals. Never confirm. All Listing rows, versions and receipt tables remain unchanged.",
    "fixed_handoff": "B uses a dependent facts-to-Listing task pair. Pause the winner's first actual fact read. "
                     "Before release, require zero downstream creation, SDK input, business read and drafting. "
                     "Match the accepted task reference, board receipt, reader response, first SDK input, subsequent "
                     "read target and draft fact payload after release.",
    "live_path": "B decides with its exposed tools. Actual Worker launch and receipt chain determine coverage. "
                 "No Worker launch means live handoff uncovered.",
    "timing": "Browser fetch dispatch to SSE terminal. Evidence from frozen scope to last guarded read result. "
              "Dependency wait sums per-task queued-to-start or queued-to-batch-end intervals. "
              "Draft wall time spans its first model call to the last model result or proposal decision.",
    "counts": "Provider tokens sum only returned usage. Physical duplicates count repeat SKU/tool/effective arguments, "
              "including evidence revalidation. Guarded reuse and guarded duplicates are reported separately.",
}


def freeze(output, database, password, listing, prior):
    from backend.agent.fault_tolerance import TIMEOUT_CONFIG
    from backend.agent.request_budget import RequestBudget
    from backend.agent.team_runtime import DelegationRun
    from backend.agent.team_tools import TeamBudget

    def defaults(constructor, keys):
        parameters = inspect.signature(constructor).parameters
        return {key: parameters[key].default for key in keys}

    write_path = output / "frozen"
    write_path.mkdir()
    scorer_files = (Path(__file__), Path(__file__).with_name("task45_t2_observe.py"),
                    Path(faults.__file__), Path(r1.__file__), Path("tests/task45_t1_browser.cjs"))
    manifest = {
        "as_of": fixture.AS_OF, "timezone": "Asia/Shanghai",
        "comparison_windows": fixture.WINDOWS, "last_week_windows": fixture.LAST_WEEK,
        "rules": RULES, "fixture_sha256": r1.digest(Path(fixture.__file__)),
        "sql_sha256": r1.digest(r1.SQL), "last_week_sql": r1.LAST_WEEK_SQL,
        "scorer_sha256": {path.name: r1.digest(path) for path in scorer_files},
        "policies": {"A": {"allow_delegation": False}, "B": {"allow_delegation": True}},
        "tool_definitions": {"business": listing.READ_TOOLS, "B_extra": listing.DELEGATE},
        "provider": {"model": MODEL, "endpoint": ENDPOINT, "http_attempts_per_round": 12, "sdk_retries": 0},
        "budgets": {
            "execution": defaults(TeamBudget, ("tool_limit", "model_limit", "seconds")),
            "context": defaults(RequestBudget, ("limit", "output_limit", "safety_margin")),
            "team": defaults(DelegationRun, ("task_limit", "concurrency")),
            "maximum_output_tokens": {"main": 1000, "draft": 900, "worker_context_reserve": 1200},
            "call_timeout_seconds": {"main_and_draft": TIMEOUT_CONFIG["llm_call"], "worker": 30},
            "browser_process_timeout_seconds": 150,
        },
        "schedule": {"fixed_pairs_per_case": 1, "live_pairs_per_case": 2, "order": ["AB", "BA"]},
        "scenarios": {},
    }
    for case in CASES:
        name = case["id"]
        folder = write_path / name
        folder.mkdir()
        db = folder / "seed.sqlite3"
        sources = fixture.source(case.get("variant", "base"))
        r1.seed(db, database, password, sources)
        write(folder / "source.json", sources)
        write(folder / "sql-expected.json", {
            "comparison": r1.oracle(db, fixture.WINDOWS),
            "last_week": r1.oracle(db, fixture.LAST_WEEK, single=True),
        })
        write(folder / "sql-before.json", r1.snapshot(db))
        manifest["scenarios"][name] = {
            "definition": case, "message_sha256": sha256(case["message"].encode()).hexdigest(),
            "source_sha256": r1.digest(folder / "source.json"),
            "sql_expectation_sha256": r1.digest(folder / "sql-expected.json"),
            "sql_before_sha256": r1.digest(folder / "sql-before.json"),
        }
    write(output / "frozen.json", manifest)
    if prior and read(output / "frozen.json") != read(prior / "frozen.json"):
        raise RuntimeError("Frozen conditions differ from the passing fixed run")
    return r1.digest(output / "frozen.json")


class FactBarrier:
    def __init__(self, observation, enabled):
        self.obs = observation
        self.armed = enabled
        self.record = {}

    def wrap(self, reader):
        @wraps(reader)
        def read_facts(*args, **kwargs):
            if self.armed and self.obs.link.get():
                self.armed = False
                released = Event()
                self.record["blocked_ns"] = perf_counter_ns()

                def sample():
                    sleep(0.45)
                    self.record.update(blocked_steps=list(self.obs.trace),
                                       blocked_requests=list(self.obs.requests),
                                       blocked_reads=[dict(row) for row in self.obs.reads],
                                       released_ns=perf_counter_ns())
                    released.set()

                Thread(target=sample, daemon=True).start()
                if not released.wait(5):
                    raise TimeoutError("Fact reader barrier did not release")
            return reader(*args, **kwargs)
        return read_facts


def timings(obs, trace_id, browser_round):
    steps = [step for step in obs.trace if step["trace_id"] == trace_id]

    def selected(stage, action=None):
        return [step for step in steps if step["stage"] == stage and (action is None or step["action"] == action)]

    def span(start, end):
        return round((end - start) / 1e6, 3)

    scope = selected("listing_scope", "fixed")
    results = [step for step in selected("tool_result") if step["action"] in READERS]
    draft_start = selected("model_call", "listing_draft")
    draft_end = selected("model_result", "listing_draft") + selected("listing_proposal")
    waits = []
    for plan, batch in zip(obs.plans, selected("batch", "started")):
        dispatch = batch.get("parent_call_id")
        downstream = {step["detail"]: step for step in selected("worker", "created")
                      if step.get("parent_call_id") == dispatch}
        end = next((step for step in selected("batch") if step.get("parent_call_id") == dispatch
                    and step["action"] != "started"), None)
        for task in plan["tasks"]:
            if task["dependencies"] and (finished := downstream.get(task["task_id"], end)):
                waits.append({"task_id": task["task_id"], "ms": span(batch["ns"], finished["ns"]),
                              "started": task["task_id"] in downstream})
    return {
        "sse_ms": browser_round.get("sse", {}).get("terminalMs"),
        "evidence_ms": span(scope[0]["ns"], results[-1]["ns"]) if scope and results else None,
        "dependency_wait_ms": round(sum(row["ms"] for row in waits), 3),
        "dependency_waits": waits,
        "draft_ms": span(draft_start[0]["ns"], max(row["ns"] for row in draft_end))
        if draft_start and draft_end else None,
    }


def metrics(obs, trace_id, rows, fixed):
    phases = {}
    attempted, tool_requests = 0, []
    for kind in ("main", "worker", "draft"):
        requests = [row for row in rows if phase(row["body"]) == kind]
        tokens = []
        formats = 0
        for row in requests:
            response = row.get("response", {})
            body = response.get("body", {})
            if body.get("usage"):
                tokens.append(body["usage"])
            if body.get("choices"):
                response = body["choices"][0].get("message", {})
            for call_id, name, arguments in calls(response):
                if name == "delegate_tasks":
                    attempted += 1
                elif name in READERS:
                    tool_requests.append({"phase": kind, "model_call_id": row.get("model_call_id"),
                                          "tool_call_id": call_id, "name": name, "arguments": arguments})
            formats += int("previous response had the wrong JSON shape" in row["body"]["messages"][0]["content"])
        phases[kind] = {
            "requests": len(requests),
            "http_attempts": 0 if fixed else len(requests),
            "sdk_retries": sum(row.get("sdk_retry", 0) > 0 for row in requests),
            "format_retries": formats,
            "responses_with_usage": len(tokens),
            "input_tokens": sum(row.get("prompt_tokens", 0) for row in tokens) if tokens else None,
            "output_tokens": sum(row.get("completion_tokens", 0) for row in tokens) if tokens else None,
        }
    steps = [step for step in obs.trace if step["trace_id"] == trace_id]
    stages = Counter((step["stage"], step["action"]) for step in steps)
    physical_keys = [(row["source"], json.dumps(row["arguments"], sort_keys=True)) for row in obs.reads]
    return {
        "phases": phases,
        "attempted_dispatches": attempted,
        "approved_dispatches": stages["batch", "started"],
        "workers_started": stages["worker", "created"],
        "workers_reclaimed": stages["worker", "reclaimed"],
        "tool_requests": len(tool_requests),
        "tool_request_details": tool_requests,
        "guarded_reads": sum(row["stage"] == "business_read" for row in steps),
        "physical_reads": len(obs.reads),
        "reads_by_checkpoint": dict(Counter(row["checkpoint"] for row in obs.reads)),
        "checkpoint_reuses": stages["listing_evidence", "reused"],
        "revalidations": sum("runtime_call_id" not in row for row in obs.reads),
        "read_retries": sum(bool(row.get("retry_of")) and row["stage"] == "business_read" for row in steps),
        "receipt_reuses": sum(row["stage"] == "tool_reuse" for row in steps),
        "blocked_duplicates": sum(row["stage"] == "duplicate_read" for row in steps),
        "physical_repeat_reads": len(physical_keys) - len(set(physical_keys)),
        "model_errors": [step for step in steps if step["stage"] == "model_result" and step.get("error_code")],
    }


def score(case, group, fixed, browser, trace, before, after, rows, obs, barrier, sources, sql):
    business = r1.score({**case, "id": "pending" if case["id"] == "base" else case["id"]}, browser, trace,
                        before, after, rows, {}, sources, sql, fixed=False)
    # R1 stores full model inputs in its report. T2 persists first inputs once in the raw SDK/replay file.
    business.pop("worker_first_inputs")
    round_data = (browser.get("rounds") or [{}])[-1]
    evidence = (round_data.get("evidence") or {}).get("body", {}).get("data") or {}
    trace_id = trace.get("trace_id")
    counts = metrics(obs, trace_id, rows, fixed)
    handoffs = verify_handoffs(obs, rows, trace_id, evidence)
    goods = [item for item in handoffs if item["passed"]]
    business_checks = {}
    complex_case = not case.get("context")
    missing = case["id"] == "missing-fact"
    proposal = evidence.get("proposal") or {}
    draft_inputs = [json.loads(row["body"]["messages"][1]["content"])
                    for row in rows if phase(row["body"]) == "draft"]
    business_checks["终态和页面无错误"] = bool(round_data.get("sse", {}).get("terminalMs") is not None
        and not round_data.get("sse", {}).get("error") and not any(event["type"] == "error"
                                                                 for event in round_data.get("events", [])))
    business_checks["三款商品 SQL 快照、版本及执行回执未变"] = (
        all(before[key] == after[key] for key in ("products", "facts", "sales", "required_facts", "receipts"))
        and not after["receipts"])
    if complex_case:
        chosen = next(row for row in sql["comparison"] if row["selected"])
        wanted = next(item for item in sources if item["target"]["sku"] == chosen["sku"])
        business_checks["答案给出 SQL 入选商品和两窗件数"] = all(token in business["response"] for token in (
            wanted["title"], chosen["sku"], f"{chosen['baseline_units']} → {chosen['current_units']} 件",
            f"入选 {wanted['title']}", *fixture.WINDOWS))
        business_checks["商品资料底层读取均限定入选 SKU"] = all(
            row["arguments"]["sku"] == chosen["sku"] for row in obs.reads if row["source"] != r1.SALES)
        facts = evidence.get("facts", {})
        business_checks["事实原文与独立夹具一致"] = (
            facts.get("version") == wanted["facts_version"] and facts.get("facts") == wanted["facts"]
            and facts.get("target") == wanted["target"])
        if not missing:
            board_facts = next((row for row in obs.board if row["source"] == r1.FACTS
                                and row["artifact_id"] == proposal.get("artifact_refs", {}).get("facts")), None)
            business_checks["草案输入及提案引用同一份事实产物"] = bool(
                board_facts and board_facts["result"] == facts and draft_inputs
                and all(row["facts"] == facts and row["target"] == wanted["target"] for row in draft_inputs))
    else:
        business_checks["底层销量仅一次且无 SDK、派单、Worker"] = (
            len(obs.reads) == 1 and obs.reads[0]["source"] == r1.SALES and not rows
            and not counts["attempted_dispatches"] and not counts["workers_started"])
    controls = {}
    if group == "A":
        controls["直查路径关闭委派"] = (not counts["approved_dispatches"] and not counts["workers_started"]
            and all(tool["function"]["name"] != "delegate_tasks" for row in rows for tool in row["body"].get("tools") or []))
    elif complex_case:
        controls["模型实际获得自主委派工具"] = any(
            tool["function"]["name"] == "delegate_tasks" for row in rows if phase(row["body"]) == "main"
            for tool in row["body"].get("tools") or [])
    if barrier is not None:
        downstream = next((item for item in handoffs if item["task"]["task_id"] == "listing"), {})
        first = next((row for row in rows if row.get("model_call_id") == downstream.get("model_call_id")), {})
        controls["屏障内下游创建、模型请求、业务读取、草案全部为零"] = bool(
            barrier.get("released_ns") and any(row["source"] == r1.FACTS and "end_ns" not in row
              for row in barrier["blocked_reads"]) and not any(
                row["stage"] == "worker" and row["action"] == "created" and row["detail"] == "listing"
                for row in barrier["blocked_steps"])
            and not any(phase(row["body"]) == "draft" or (
                phase(row["body"]) == "worker" and json.loads(row["body"]["messages"][1]["content"])["task"]["task_id"] == "listing")
                for row in barrier["blocked_requests"])
            and not any(row["source"] == r1.CURRENT for row in barrier["blocked_reads"])
            and first.get("start_ns", 0) > barrier["released_ns"])
        controls["下游首份输入、看板产物、任务引用、实际查询和草案一致"] = bool(
            downstream.get("passed") and downstream["scope"]["target"] == evidence["target"]
            and downstream["scope"]["windows"] == list(fixture.WINDOWS)
            and len(downstream["receipts"]) == 4
            and {item["first_input_receipt"]["artifact_id"] for item in downstream["receipts"]} ==
            {row["artifact_id"] for row in evidence.get("comparison_receipts", [])} |
            {evidence.get("artifacts", {}).get(r1.FACTS, {}).get("artifact_id")}
            and all(read["source"] == r1.CURRENT and read["arguments"]["sku"] == evidence["target"]["sku"]
                    for read in downstream["downstream_reads"])
            and (not draft_inputs if missing else business_checks["草案输入及提案引用同一份事实产物"]))
    first = business["first_deviation"] or next((name for name, value in (business_checks | controls).items()
                                                  if not value), None)
    return {
        **business, "passed": first is None, "business_passed": business["passed"] and all(business_checks.values()),
        "first_deviation": first, "business_checks": business_checks, "control_checks": controls,
        "path": "delegated" if counts["workers_started"] else "direct" if counts["guarded_reads"] else "no_read",
        "verified_worker_handoffs": len(goods), "handoffs": handoffs, "metrics": counts,
        "timing": timings(obs, trace_id, round_data), "terminal": round_data.get("terminal"),
        "browser_failure": browser.get("failure"),
    }


def summarize(output, completed, *, fixed, manifest_sha256):
    def counts_for(rounds):
        return {
            "business_passed": sum(row["business_passed"] for row in rounds), "total": len(rounds),
            "gate_passed": sum(row["passed"] for row in rounds),
            "attempted_dispatch_rounds": sum(row["metrics"]["attempted_dispatches"] > 0 for row in rounds),
            "approved_dispatch_rounds": sum(row["metrics"]["approved_dispatches"] > 0 for row in rounds),
            "worker_started_rounds": sum(row["metrics"]["workers_started"] > 0 for row in rounds),
            "verified_handoff_rounds": sum(row["verified_worker_handoffs"] > 0 for row in rounds),
            "worker_reclaimed_rounds": sum(row["metrics"]["workers_reclaimed"] > 0 and
                row["metrics"]["workers_reclaimed"] == row["metrics"]["workers_started"] for row in rounds),
            "simple_over_delegation_rounds": sum(row["case"].startswith("simple-") and
                row["metrics"]["attempted_dispatches"] > 0 for row in rounds),
        }

    groups = {}
    for group in ("A", "B"):
        rounds = [row for row in completed if row["group"] == group]
        groups[group] = {**counts_for(rounds), "scenarios": {
            case["id"]: counts_for([row for row in rounds if row["case"] == case["id"]]) for case in CASES}}
    pairs = []
    timing_keys = ("sse_ms", "evidence_ms", "dependency_wait_ms", "draft_ms")
    for name, pair in dict.fromkeys((row["case"], row["pair"]) for row in completed):
        rounds = [row for row in completed if (row["case"], row["pair"]) == (name, pair)]
        if len(rounds) != 2:
            continue
        sides = {row["group"]: row for row in rounds}
        left, right = (sides[group] for group in ("A", "B"))
        pairs.append({"case": name, "pair": pair, "order": "".join(row["group"] for row in rounds),
                      "A": left["folder"], "B": right["folder"],
                      "policy": left.get("policy") if left.get("policy") == right.get("policy") else None,
                      "same_path": left["path"] == right["path"],
                      "paths": {side: row["path"] for side, row in sides.items()},
                      "reads": {side: row["metrics"]["reads_by_checkpoint"] for side, row in sides.items()},
                      "both_passed": left["business_passed"] and right["business_passed"],
                      "B_minus_A_ms": {key: round(right["timing"][key] - left["timing"][key], 3)
                                       if left["timing"][key] is not None and right["timing"][key] is not None
                                       else None for key in timing_keys}})

    def timing_median(pairs):
        result = {}
        for key in timing_keys:
            values = [pair["B_minus_A_ms"][key] for pair in pairs if pair["B_minus_A_ms"][key] is not None]
            result[key] = {"median": round(median(values), 3) if values else None, "pairs": len(values)}
        return result

    total = len(CASES) * (2 if fixed else 4)
    report = {"fixed": fixed, "frozen_sha256": manifest_sha256, "expected_rounds": total,
              "completed_rounds": len(completed), "passed": len(completed) == total
              and all(row["passed"] for row in completed), "groups": groups, "pairs": pairs,
              "B_minus_A_ms": timing_median(pairs),
              "scenario_medians_ms": {case["id"]: timing_median(
                  [pair for pair in pairs if pair["case"] == case["id"]]) for case in CASES},
              "worker_handoff_coverage": ("pending" if len(completed) < total else
                  "verified" if groups["B"]["verified_handoff_rounds"] else
                  "launched_unverified" if groups["B"]["worker_started_rounds"] else "uncovered"),
              "rounds": completed}
    write(output / "report.json", report)
    return report


def run(output, prior, *, baseline_source=None, fault_suite=False):
    fixed = prior is None
    bootstrap = output / "startup.sqlite3"
    configure(bootstrap, "mock" if fixed else "glm")
    if not fixed:
        credentials = dotenv_values(".env")
        os.environ.setdefault("GLM_API_KEY", credentials.get("GLM_API_KEY") or "")
        os.environ.setdefault("GLM_MODEL", credentials.get("GLM_MODEL") or MODEL)
        if not os.environ["GLM_API_KEY"]:
            raise RuntimeError("GLM_API_KEY is required for live mode")
    from backend import observability
    from backend.agent import listing_run, llm_provider, request_budget, team_contracts, team_runtime, team_tools
    from backend.captcha import captcha
    from backend.database import chat_groups, database, listing_operations
    from tests.home_diagnosis_server import demo_captcha
    baseline = None
    if baseline_source:
        spec = importlib.util.spec_from_file_location("task45_t2_baseline", baseline_source)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        write(output / "baseline.json", {"listing_run_sha256": r1.digest(baseline_source)})

    password = token_urlsafe(24)
    redactor = Redactor([password, os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY")])
    manifest_sha256 = freeze(output, database, password, listing_run, prior)
    database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(bootstrap)
    database.init_db()
    obs = Observation()
    barrier = FactBarrier(obs, False)
    fault = faults.Fault()
    active = {}
    requested_at = datetime.fromisoformat(fixture.AS_OF + "T12:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    accept, run_listing = chat_groups.accept, listing_run.run_listing

    def accept_at_frozen_date(*args, **kwargs):
        group_run, context = accept(*args, **kwargs)
        return replace(group_run, requested_at=requested_at), context

    def policy(message, user_id, group_run, model_provider, model_name, budget, trace_id, evidence_sink):
        allowed = active["policy"] == "B"
        obs.budgets.append({"run_id": group_run.run_id, "trace_id": trace_id,
                            "delegation": allowed, "execution_remaining": budget.execution.remaining(),
                            "execution_seconds_left": budget.execution.seconds_left(),
                            "context_limit": budget.limit, "output_limit": budget.output_limit,
                            "safety_margin": budget.safety_margin})
        implementation = baseline.run_listing if baseline and (fixed or active["group"] == "A") else run_listing
        return implementation(message, user_id, group_run, model_provider, model_name, budget, trace_id,
                              evidence_sink, allow_delegation=allowed)

    class LiveAudit(GLMAudit):
        run_cap = len(CASES) * 4 * 12

        def request(self, request):
            path = active.get("db")
            if (not self.enabled or not path or self.model_link.get().get("step") not in
                    {"listing_operations", "team_worker", "listing_draft"}
                    or database.sandbox_db_path() != path or not active["case"]["id"] in self.case):
                raise RuntimeError("Outbound request is outside the active synthetic Listing case")
            if r1.snapshot(path) != active["before"]:
                raise RuntimeError("Outbound request requires the frozen SQL snapshot")
            return super().request(request)

        def response(self, response):
            super().response(response)
            # Each scheduled pair retains its own failures and still gets its allotted attempts.
            self.stop = None

    model = FixedModel(obs) if fixed else llm_provider.ProviderFactory.create()
    audit = None
    completed = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=model))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        stack.enter_context(patch.object(chat_groups, "accept", accept_at_frozen_date))
        stack.enter_context(patch.object(listing_run, "run_listing", policy))
        stack.enter_context(patch("backend.memory.conversation_memory.store_conversation"))
        for attr, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                               ("FUNC_LOG_FILE", "functions.jsonl")):
            stack.enter_context(patch.object(observability, attr, output / filename))
        for module in filter(None, (observability, listing_run, llm_provider, request_budget, team_tools, team_runtime,
                                    listing_operations, chat_groups, baseline)):
            stack.enter_context(patch.object(module, "add_trace_step", obs.trace_step(module.add_trace_step)))
        stack.enter_context(patch.object(team_tools, "trace_tool_call", obs.trace_tool(team_tools.trace_tool_call)))
        board = team_contracts.ReceiptBoard
        stack.enter_context(patch.object(board, "append", obs.board_append(board.append)))
        stack.enter_context(patch.object(board, "accept", obs.board_accept(board.accept)))
        stack.enter_context(patch.object(board, "accept_direct", obs.board_accept(board.accept_direct, direct=True)))
        for source, name in READERS.items():
            original = getattr(listing_operations, name)
            if source == r1.FACTS:
                original = barrier.wrap(original)
            stack.enter_context(patch.object(listing_operations, name, obs.reader(source, original)))
        if baseline:
            require_snapshot = listing_operations._require_snapshot

            class LegacyReadConnection:
                """Turn off same-transaction reuse at the legacy storage checkpoints."""
                in_transaction = False

                def __init__(self, conn):
                    self.execute = conn.execute

            def paired_snapshot(conn, *args, **kwargs):
                if fixed or active["group"] == "A":
                    conn = LegacyReadConnection(conn)
                    kwargs.pop("reviewed_listing", None)
                return require_snapshot(conn, *args, **kwargs)

            stack.enter_context(patch.object(listing_operations, "_require_snapshot", paired_snapshot))
        if not fixed:
            stack.callback(model._client.close)
            if model._model != MODEL or str(model._client.base_url).rstrip("/") != ENDPOINT:
                raise RuntimeError("Expected the official GLM endpoint and " + MODEL)
            audit = LiveAudit(output / "sdk.sqlite3", redactor, uuid4().hex, LiveAudit.run_cap)
            hooks = model._client._client.event_hooks
            hooks["request"].append(audit.request)
            hooks["response"].append(audit.response)
            for module in (llm_provider, team_runtime):
                stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        import fast_server
        if fault_suite:
            stack.enter_context(patch.object(fast_server, "get_db", fault.approval(fast_server.get_db)))
            stack.enter_context(patch.object(chat_groups.GroupRun, "save_reply", fault.proposal(chat_groups.GroupRun.save_reply)))
            runtime = team_runtime.DelegationRun
            stack.enter_context(patch.object(runtime, "_block_unavailable_inputs",
                                             fault.handoff(runtime._block_unavailable_inputs)))

        schedule = [(pair, order, case, None) for pair, order in enumerate(("AB",) if fixed else ("AB", "BA"), 1)
                    for case in CASES] if not fault_suite else [
            (1, group, next(case for case in CASES if case["id"] == "base"), name) for name, group in faults.CASES]
        for pair, order, case, fault_name in schedule:
            seed = output / "frozen" / case["id"]
            for group in order:
                policy_group = ("A" if pair == 1 else "B") if baseline and not fixed else group
                identifier = fault_name or f"{pair:02}-{case['id']}-{group}"
                folder = output / identifier
                folder.mkdir()
                db_path = folder / "sandbox.sqlite3"
                shutil.copyfile(seed / "seed.sqlite3", db_path)
                for filename in ("source.json", "sql-expected.json", "sql-before.json"):
                    shutil.copyfile(seed / filename, folder / filename)
                before = read(folder / "sql-before.json")
                if r1.snapshot(db_path) != before:
                    raise RuntimeError("Database clone differs from the frozen fixture")
                write(folder / "scenario.json", {**case, "observe_pending": True,
                      "force_confirm": bool(fault_name and fault_name.startswith("approval-"))})
                active.update(group=group, policy=policy_group, case=case, db=db_path, before=before)
                database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(db_path)
                obs.reset()
                barrier.armed = fixed and policy_group == "B" and not case.get("context") and not fault_suite
                barrier_expected = barrier.armed
                barrier.record = {}
                if fixed:
                    model.case = "control-missing-fact" if case["id"] == "missing-fact" else case["id"]
                    model.requests.clear()
                else:
                    audit.case, audit.enabled = identifier, True
                fault.arm(fault_name, db_path)
                with serve(fast_server.app) as origin:
                    try:
                        process = subprocess.run(["node", "tests/task45_t1_browser.cjs", origin, str(folder),
                                                  str(db_path)], env={**browser_env(),
                                                  "TASK45_BROWSER_PASSWORD": password}, capture_output=True,
                                                 timeout=150, check=False)
                        write(folder / "browser-process.json", redactor.clean({"exit_code": process.returncode,
                              "stderr": process.stderr.decode(errors="replace")}))
                    except subprocess.TimeoutExpired:
                        write(folder / "browser-timeout.json", {"timeout_seconds": 150})
                    finally:
                        if audit:
                            audit.enabled = False
                browser_path = folder / "browser.json"
                browser = read(browser_path) if browser_path.exists() else {"failure": "browser_timeout"}
                write(browser_path, redactor.clean(browser))
                terminal = (browser.get("rounds") or [{}])[-1].get("terminal") or {}
                trace_id = terminal.get("trace_id") or (obs.budgets[-1]["trace_id"] if obs.budgets else None)
                trace = observability.get_trace(trace_id) or {}
                model_rows = list(obs.requests) if fixed else [row for row in audit.rows() if row["case"] == identifier]
                after = r1.snapshot(db_path)
                for filename, value in {
                    "model-replay.json" if fixed else "sdk.json": model_rows, "trace.json": trace,
                    "trace-timestamps.json": obs.trace, "bottom-reads.json": obs.reads,
                    "reading-inventory.json": obs.reading_inventory(),
                    "board.json": obs.board, "task-sheets.json": obs.plans, "budgets.json": obs.budgets,
                    "tool-definitions.json": [{"role": phase(row["body"]), "model_call_id": row.get("model_call_id"),
                                               "tools": row["body"].get("tools", [])} for row in model_rows],
                    "sql-after.json": after,
                }.items():
                    write(folder / filename, redactor.clean(value))
                if barrier_expected:
                    write(folder / "barrier.json", redactor.clean(barrier.record))
                if fault_name:
                    checks = fault.score(fault_name, obs, browser, after)
                    write(folder / "fault.json", redactor.clean(fault.record))
                    result = {"case": fault_name, "passed": all(checks.values()), "checks": checks}
                    write(folder / "result.json", result)
                    completed.append(result)
                    print(identifier, result["passed"], [name for name, passed in checks.items() if not passed], flush=True)
                    continue
                result = score(case, policy_group, fixed, browser, trace, before, after, model_rows, obs,
                               barrier.record if barrier_expected else None, read(folder / "source.json"),
                               read(folder / "sql-expected.json"))
                write(folder / "worker-handoff.json", redactor.clean(result.pop("handoffs")))
                write(folder / "result.json", redactor.clean(result))
                row = {key: result[key] for key in ("passed", "business_passed", "first_deviation", "trace_id",
                    "path", "metrics", "timing", "terminal", "verified_worker_handoffs", "browser_failure")}
                row.update(case=case["id"], pair=pair, group=group, policy=policy_group, folder=identifier)
                completed.append(redactor.clean(row))
                summarize(output, completed, fixed=fixed, manifest_sha256=manifest_sha256)
                print(identifier, row["passed"], row["first_deviation"] or row["path"], flush=True)
    if fault_suite:
        passed = len(completed) == len(faults.CASES) and all(row["passed"] for row in completed)
        write(output / "report.json", {"passed": passed, "rounds": completed})
        return passed
    return summarize(output, completed, fixed=fixed, manifest_sha256=manifest_sha256)["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixed-results", type=Path, help="Only run live GLM after a passing frozen replay.")
    parser.add_argument("--baseline-source", type=Path,
                        help="Replay this pre-change Listing module in fixed mode or pair it with current code in live mode.")
    parser.add_argument("--faults", action="store_true", help="Run the fixed committed-change boundary cases.")
    parser.add_argument("--fault-results", type=Path, help="Passing fixed boundary cases for the paired GLM run.")
    args = parser.parse_args()
    prior = args.fixed_results.resolve() if args.fixed_results else None
    if prior:
        report = read(prior / "report.json")
        if (not report["fixed"] or not report["passed"] or report["completed_rounds"] != len(CASES) * 2
                or report["frozen_sha256"] != r1.digest(prior / "frozen.json")
                or report["groups"]["B"]["verified_handoff_rounds"] != 3):
            parser.error("The fixed replay and three dependent handoff controls must pass first")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if args.faults and (prior or args.baseline_source):
        parser.error("Faults run in fixed mode against the current implementation.")
    if args.baseline_source and prior:
        fault_report = read(args.fault_results / "report.json") if args.fault_results else {}
        if not fault_report.get("passed") or {row["case"] for row in fault_report["rounds"]} != {
                name for name, _ in faults.CASES} or read(args.fault_results / "frozen.json") != read(prior / "frozen.json"):
            parser.error("The paired GLM run requires the passing, matching fixed fault suite")
    return run(output, prior, baseline_source=args.baseline_source, fault_suite=args.faults)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
