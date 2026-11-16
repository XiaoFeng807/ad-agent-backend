"""Freeze and run the real GLM Chromium work-group evaluation with a dedicated SDK ledger."""

import argparse
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter, perf_counter_ns
from unittest.mock import patch
from uuid import uuid4

from dotenv import dotenv_values

from scripts.audit_task27_t3 import metric_matches, rule_matches
from scripts.run_real_diagnosis import CheckFailed, Redactor, require
from scripts.run_task27_t3 import Audit, USERNAME, bind, seed, summarize
from scripts.task27_t3_fixture import ARTIFACT, OWNER, ROOT, digest, freeze, write
from scripts.task27_t3a_fixture import BASE, MANIFEST, ORIGINAL_SUFFIX, VARIANT_SUFFIX, apply_variant

FROZEN = ROOT / "docs/task27-t5/frozen.json"
LEDGER = ROOT / "logs/task27-t5/glm-sdk-ledger.sqlite3"
DEEPSEEK_LEDGER = ROOT / "logs/task27-t3/sdk-ledger.sqlite3"
MODEL = "glm-4.5-air"
ENDPOINT = "https://open.bigmodel.cn/api/paas/v4"
CASE_CAP, RUN_CAP = 12, 72
DETAIL, METRICS, RULE = "get_plan_detail", "get_plan_metrics_by_date", "search_knowledge"
BROWSER = ROOT / "tests/task27_t3_browser.cjs"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def frozen_fixture():
    base = read(BASE)
    oracle = json.loads(json.dumps(freeze()))
    require({**base, "frozen_utc": None} == {**oracle, "frozen_utc": None}, "t3_oracle_drift")
    variant = apply_variant(base, BASE)
    by_id = {case["id"]: case for case in base["cases"]}
    missing = by_id["gap-2"]
    require(missing["message"].endswith(ORIGINAL_SUFFIX), "missing_date_message_drift")
    message = missing["message"].removesuffix(ORIGINAL_SUFFIX) + VARIANT_SUFFIX
    missing = {**missing, "message": message, "message_sha256": sha256(message.encode()).hexdigest()}
    cases = [by_id["simple-1"], by_id["simple-2"], *variant["cases"], missing]
    require(cases[2]["message"] == cases[3]["message"], "positive_messages_differ")
    return {**base, "task": "TASK-027-T5", "frozen_utc": None, "cases": cases,
            "t3_sha256": digest(BASE), "t3a_sha256": digest(MANIFEST),
            "model": MODEL, "sdk_limit": RUN_CAP, "round_sdk_limit": CASE_CAP}


class GLMAudit(Audit):
    """Reserve an attempt before each actual HTTP send, including SDK retries and failures."""

    enabled = False
    blocked = None
    run_cap = RUN_CAP

    def case_cap(self, case):
        return CASE_CAP

    def request(self, request):
        if not self.enabled:
            self.stop = "preflight_attempted_sdk_request"
            raise CheckFailed(self.stop)
        require(self.case and str(request.url) == ENDPOINT + "/chat/completions"
                and request.method == "POST", "unexpected_glm_sdk_destination")
        body = self.redactor.clean(json.loads(request.content))
        require(body.get("model") == MODEL and not body.get("stream"), "unexpected_glm_sdk_payload")
        row = {**self.model_link.get(), "start_ns": perf_counter_ns(), "body": body,
               "endpoint": str(request.url), "sdk_retry": int(request.headers.get("x-stainless-retry-count", "0"))}
        with closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            total, used = conn.execute("SELECT COUNT(*), COUNT(CASE WHEN sample=? THEN 1 END) FROM sdk",
                                       (self.case,)).fetchone()
            if self.stop or total >= self.run_cap or used >= self.case_cap(self.case):
                self.blocked = self.stop or ("total_sdk_attempt_limit" if total >= self.run_cap else "round_sdk_attempt_limit")
                raise CheckFailed(self.blocked)
            attempt = conn.execute("INSERT INTO sdk(run,sample,request) VALUES (?,?,?)",
                                   (self.run, self.case, json.dumps(row, ensure_ascii=False))).lastrowid
        request.extensions["task27_t3_attempt"] = attempt

    def observe_direct(self, original):
        def accept(runtime, source, result):
            artifact = original(runtime, source, result)
            if artifact:
                self.receipts.append({**self.tool_link.get(), "case": self.case, "source": source,
                                      "artifact_id": artifact, "accepted": True, "ns": perf_counter_ns(),
                                      "tool_call_id": result["runtime_call_id"], "arguments": result["arguments"],
                                      "status": "succeeded" if result["success"] else "failed",
                                      "result": result.get("result"), "error_code": result.get("error_code")})
            return artifact
        return accept


def configure(scratch, redactor):
    values = dotenv_values(ROOT / ".env")
    for key in ("LLM_PROVIDER", "GLM_API_KEY", "GLM_MODEL"):
        if values.get(key):
            os.environ.setdefault(key, values[key])
    secret = token_urlsafe(48)
    redactor.secrets.extend([secret, *(values.get(key) for key in ("API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY", "JWT_SECRET")),
                             *(os.environ.get(key) for key in ("API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY", "OPENAI_API_KEY"))])
    os.environ.update(AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", ANONYMIZED_TELEMETRY="False",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH=str(scratch / "sandbox.sqlite3"),
                      PLAN_METRICS_DB_PATH=str(ARTIFACT), RAG_DB_PATH=str(scratch / "knowledge"), JWT_SECRET=secret)
    from backend.config.config import settings

    config = settings.llm_config()
    require((config.provider, config.model, config.base_url) == ("glm", MODEL, ENDPOINT) and config.api_key,
            "configured_glm_4_5_air_required")
    return settings


def sql_oracle(case, frozen, database, metrics):
    """Read the actual SQLite binding and independently aggregate the read-only history file."""
    with closing(database.get_db(read_only=True)) as conn:
        plans = [dict(row) for row in conn.execute(frozen["plan_sql"], (OWNER,))]
        bindings = [dict(row) for row in conn.execute("SELECT * FROM plan_metric_bindings ORDER BY plan_id")]
    require(plans == frozen["plans"], "isolated_plans_drifted")
    bound = {str(row["plan_id"]): row["source_plan_id"] for row in bindings}
    require(bound == {p: v for p, v in case["bindings"].items() if v}, "isolated_bindings_drifted")
    expected = case["expected"]
    if case["kind"] == "simple":
        return {"plans": plans, "bindings": bindings, "target": expected["detail"]["id"]}
    rows = {}
    rule = expected["rule"]
    with closing(sqlite3.connect(metrics.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        for plan, facts in expected["metrics"].items():
            windows = []
            for expected_window in facts["windows"]:
                start, end = (expected_window[k] for k in ("start_date", "end_date"))
                # The isolated copy already omits the row. The frozen oracle omitted it in SQL.
                params = [bound.get(plan), start, end, None]
                row = dict(conn.execute(frozen["sql"]["window"], params).fetchone())
                row["missing_dates"] = row["missing_dates"].split(",") if row["missing_dates"] else []
                require(all(expected_window[key] == val for key, val in row.items()), "window_sql_drift")
                windows.append({**row, "start_date": start, "end_date": end, "sql_params": params})
            base, current = windows
            params = [base["impressions"], current["impressions"], base["ctr_pct"], current["ctr_pct"],
                      rule["min_impressions"], rule["min_relative_decline_pct"]]
            comparison = dict(conn.execute(frozen["sql"]["review"], params).fetchone())
            require(all(facts[key] == val for key, val in comparison.items()), "review_sql_drift")
            rows[plan] = {**comparison, "unbound": plan not in bound, "windows": windows, "review_sql_params": params}
    targets = [int(plan) for plan, row in rows.items() if row["qualifies"]]
    target = targets[0] if len(targets) == 1 and all(row["qualifies"] is not None for row in rows.values()) else None
    require(target == (expected["detail"] or {}).get("id"), "detail_target_sql_drift")
    return {"plans": plans, "bindings": bindings, "metrics": rows, "rule": rule, "target": target}


def browser_env():
    keys = {"api_key", "glm_api_key", "deepseek_api_key", "openai_api_key", "jwt_secret"}
    return {key: value for key, value in os.environ.items() if key.lower() not in keys}


def browser_round(url, folder, case, sample, password, redactor, *, capture_only=False):
    inputs, target = folder / "browser-input.json", folder / "browser.json"
    write(inputs, {"title": f"27-T5 {case['id']}", "message": case["message"], "username": USERNAME})
    try:
        command = ["node", str(BROWSER), url, str(inputs), str(target)]
        if capture_only:
            command.append("--capture-only")
        process = subprocess.run(command, cwd=ROOT,
                                 env={**browser_env(), "AD_TASK27_T3_PASSWORD": password}, capture_output=True, timeout=135)
        if process.returncode:
            (folder / "browser-error.txt").write_text(redactor.clean(process.stderr.decode(errors="replace")),
                                                       encoding="utf-8")
            sample["browser_failed"] = True
    finally:
        if target.exists():
            sample.update(redactor.clean(read(target)))
            write(target, redactor.clean(read(target)))


def committed_group(sample, database):
    if not (group_id := sample.get("request", {}).get("conversation_id")):
        return
    with closing(database.get_db(read_only=True)) as conn:
        sample["committed_runs"] = [dict(row) for row in conn.execute(
            "SELECT run_id, trace_id, status FROM chat_group_runs WHERE conversation_id=?", (group_id,))]
        sample["committed_entries"] = [dict(row) for row in conn.execute(
            "SELECT kind, content, details FROM chat_group_entries WHERE run_id IN "
            "(SELECT run_id FROM chat_group_runs WHERE conversation_id=?) ORDER BY id", (group_id,))]


def run(args, frozen, selected, scratch, redactor, summary, *, audit_class=GLMAudit, replay=None,
        expected_targets=None, install_observers=None, resume_cases=False, browser_preflight=False):
    settings = configure(scratch, redactor)
    from chromadb.api.shared_system_client import SharedSystemClient
    import uvicorn
    from backend import observability
    from backend.agent import home_react, llm_provider, team_runtime, team_tools
    from backend.agent.conditional_detail import parse_detail_condition
    from backend.agent.multi_window_review import parse_review_goal
    from backend.captcha import captcha
    from backend.database import database
    from backend.memory import conversation_memory
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import seed_review_rules
    from backend.tools import tools
    from scripts.eval_review_retrieval import FixedReviewEmbedding
    from tests.home_diagnosis_server import demo_captcha

    ledger = scratch / "preflight-ledger.sqlite3" if args.preflight else args.ledger
    ledger.parent.mkdir(parents=True, exist_ok=True)
    audit = audit_class(ledger, redactor, summary["run_id"], audit_class.run_cap)
    prior = audit.rows(all_runs=True)
    require(all(row.get("endpoint") == ENDPOINT + "/chat/completions" for row in prior), "ledger_is_not_t5_glm")
    used = {case["id"]: sum(row["case"] == case["id"] for row in prior) for case in selected}
    require(resume_cases or not any(used.values()), "selected_round_already_attempted")
    require(all(count < audit.case_cap(name) for name, count in used.items()), "selected_round_at_sdk_limit")
    require(len(prior) + sum(audit.case_cap(name) - count for name, count in used.items()) <= audit.run_cap,
            "insufficient_sdk_attempt_capacity")
    summary.update(sdk_before=len(prior), provider="glm", model=MODEL, endpoint=ENDPOINT,
                   embedding="FixedReviewEmbedding", sqlite=sqlite3.sqlite_version)
    password = token_urlsafe(24)
    redactor.secrets.append(password)
    seed(database, password)
    provider = llm_provider.ProviderFactory.create()
    hooks = provider._client._client.event_hooks
    hooks["request"].append(audit.request)
    hooks["response"].append(audit.response)
    with ExitStack() as stack:
        stack.callback(provider._client.close)
        stack.callback(SharedSystemClient.clear_system_cache)
        stack.callback(lambda: [system.stop() for system in SharedSystemClient._identifier_to_system.values()])
        if replay:
            replay.install(stack, provider, audit)
        if install_observers:
            install_observers(stack, provider, audit)
        for name in ("TRACE_LOG_FILE", "FUNC_LOG_FILE", "ERROR_LOG_FILE"):
            stack.enter_context(patch.object(observability, name, Path(os.devnull)))
        for module in (conversation_memory, rag_knowledge):
            stack.enter_context(patch.object(module, "BGEEmbedding", FixedReviewEmbedding))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        for module in (llm_provider, team_runtime):
            stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        for module in (home_react, team_tools):
            stack.enter_context(patch.object(module, "trace_tool_call", audit.trace_tool(module.trace_tool_call)))
        runtime = team_runtime.DelegationRun
        stack.enter_context(patch.object(runtime, "_work", audit.wrap_work(runtime._work)))
        stack.enter_context(patch.object(runtime, "_record", audit.wrap_receipt(runtime._record)))
        stack.enter_context(patch.object(runtime, "accept_direct", audit.observe_direct(runtime.accept_direct)))
        for name in home_react.READ_SCHEMAS:
            wrapped = audit.wrap_read(name, getattr(tools, name))
            stack.enter_context(patch.object(tools, name, wrapped))
            stack.enter_context(patch.dict(tools.function_map, {name: wrapped}))
        for name in ("query_plan_detail", "query_plan_metrics"):
            wrapped = audit.wrap_read(name, getattr(tools, name))

            def observe(*args, observed=wrapped, **kwargs):
                before = len(audit.reads)
                try:
                    return observed(*args, **kwargs)
                finally:
                    # The actual connector is a callable, not part of the SQL parameters.
                    for row in audit.reads[before:]:
                        row["arguments"].pop("connect", None)

            stack.enter_context(patch.object(tools, name, observe))

        from fast_server import app

        seed_review_rules(rag_knowledge.get_rag_knowledge())
        require("post" in app.openapi()["paths"]["/api/chat/stream"], "stream_route_missing")
        oracles = {}
        for case in selected:
            require(parse_detail_condition(case["message"]) == (False, None), "unexpected_single_window_route")
            review, goal = parse_review_goal(case["message"])
            require(review == (case["kind"] != "simple") and review == bool(goal), "unexpected_multi_window_route")
            fixture = bind(case, frozen["source"], database, settings, scratch)
            sql = sql_oracle(case, frozen, database, Path(settings.PLAN_METRICS_DB_PATH))
            oracles[case["id"]] = {**sql, "fixture": fixture, "review_goal": goal.trace_fields() if goal else None}
            if not goal:
                continue
            require(rule_matches(json.loads(tools.search_knowledge(goal.rule)), sql["rule"]), "exact_rule_preflight_failed")
            for plan, facts in sql["metrics"].items():
                for window in facts["windows"]:
                    result = tools.get_plan_metrics_by_date(int(plan), window["start_date"], window["end_date"], OWNER)
                    require(metric_matches(result, window, {**facts, "id": int(plan)}, OWNER, frozen["source"]),
                            "fixture_tool_differs_from_sql")
        chrome = subprocess.run(["node", "-e", "const {chromium}=require('playwright'); "
                                 "chromium.launch(require('./tests/browser_options.cjs').browserOptions()).then(async b => "
                                 "{console.log(b.version()); await b.close();}).catch(() => process.exit(1));"],
                                cwd=ROOT, env=browser_env(), capture_output=True, timeout=30, text=True)
        require(chrome.returncode == 0 and chrome.stdout.strip(), "chromium_launch_failed")
        if expected_targets is not None:
            require({name: sql["target"] for name, sql in oracles.items()} == expected_targets,
                    "preflight_sql_targets_differ")
        require(not audit.rows() and not audit.stop, "preflight_used_sdk")
        summary.update(local_preflight=True, preflight_sdk_attempts=0, chromium=chrome.stdout.strip())
        write(args.output / "sql-oracle.json", oracles)
        if args.preflight and not browser_preflight:
            summary.update(sdk_attempts=0, task_sdk_attempts=0)
            return

        listener = stack.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
        thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        samples = []
        audit.enabled = not args.preflight and (replay is None or replay.live)
        try:
            for _ in range(100):
                if server.started or not thread.is_alive():
                    break
                thread.join(0.05)
            require(server.started, "loopback_server_not_ready")
            for case in selected:
                folder = args.output / case["id"]
                folder.mkdir()
                sample = {"id": case["id"], "entry": "chromium", "sse": []}
                started = perf_counter()
                try:
                    if replay and replay.skip(case, samples):
                        sample["unexecuted"] = "natural_boundary_already_exercised"
                        continue
                    if audit.stop:
                        sample["unexecuted"] = audit.stop
                        continue
                    sample["fixture"] = bind(case, frozen["source"], database, settings, scratch)
                    require(sample["fixture"] == oracles[case["id"]]["fixture"], "formal_fixture_differs_from_preflight")
                    audit.case, audit.blocked = case["id"], None
                    if replay:
                        replay.prepare(case, oracles[case["id"]])
                    browser_round(url, folder, case, sample, password, redactor, capture_only=args.preflight)
                    require(sample.get("request"), "chromium_did_not_post_round")
                    if args.preflight:
                        require(sample.get("captured_without_stream"), "chromium_did_not_capture_preflight")
                    else:
                        require(sample.get("dom_replay_pass"), "chromium_did_not_verify_refresh")
                except Exception as error:
                    sample["failure"] = str(error) if isinstance(error, CheckFailed) else type(error).__name__
                    if isinstance(error, subprocess.TimeoutExpired):
                        audit.stop = "evaluation_transport_timeout"
                finally:
                    summarize(sample, audit, observability)
                    if replay:
                        replay.capture(sample)
                    committed_group(sample, database)
                    sample.update(elapsed_seconds=sample.get("browser_round_seconds", round(perf_counter() - started, 3)),
                                  sdk_blocked=audit.blocked)
                    samples.append(sample)
                    write(folder / "round.json", redactor.clean(sample))
                    write(args.output / "rounds.json", redactor.clean(samples))
                    write(args.output / "sdk.json", audit.rows())
                    print(json.dumps({"case": case["id"], "sdk": len(sample["sdk_attempts"]),
                                      "trace": (sample["trace"] or {}).get("trace_id"), "workers": len(sample["workers"]),
                                      "failure": sample.get("failure"), "unexecuted": sample.get("unexecuted")}),
                          file=sys.__stdout__, flush=True)
                    audit.case = None
        finally:
            server.should_exit = True
            thread.join(35)
            summary.update(sdk_attempts=len(audit.rows()), task_sdk_attempts=len(audit.rows(all_runs=True)), stop=audit.stop)
        if args.preflight:
            require(len(samples) == len(selected) and not audit.rows() and not audit.stop and all(
                sample.get("captured_without_stream") and not sample.get("failure") and not sample["committed_runs"]
                for sample in samples), "browser_preflight_failed")
            summary["browser_preflight"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--case", action="append")
    parser.add_argument("--ledger", type=lambda value: Path(value).resolve(), default=LEDGER)
    args = parser.parse_args()
    oracle = frozen_fixture()
    if args.freeze:
        require(not FROZEN.exists(), "t5_already_frozen")
        FROZEN.parent.mkdir(parents=True, exist_ok=True)
        write(FROZEN, {**oracle, "frozen_utc": datetime.now(timezone.utc).isoformat()})
        return 0
    frozen = read(FROZEN)
    require({**frozen, "frozen_utc": None} == oracle, "t5_frozen_fixture_mismatch")
    require(args.output is not None, "output_required")
    require(args.ledger != DEEPSEEK_LEDGER.resolve() and
            (not args.ledger.exists() or not args.ledger.samefile(DEEPSEEK_LEDGER)), "must_use_separate_glm_ledger")
    selected = [case for case in frozen["cases"] if not args.case or case["id"] in args.case]
    require(not args.case or set(args.case) == {case["id"] for case in selected}, "unknown_case")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    protected = [ROOT / "ad_agent.db", ARTIFACT, BASE, MANIFEST, DEEPSEEK_LEDGER,
                 ROOT / "scripts/run_task27_t3.py", BROWSER]
    before = {str(path.relative_to(ROOT)): digest(path) for path in protected}
    summary = {"run_id": uuid4().hex, "frozen_sha256": digest(FROZEN), "storage_before": before,
               "sdk_limit": RUN_CAP, "round_sdk_limit": CASE_CAP, "selected": [case["id"] for case in selected],
               "source_hashes": {name: digest(ROOT / name) for name in (
                   "backend/agent/home_react.py", "backend/agent/orchestrator.py", "backend/agent/multi_window_review.py",
                   "backend/agent/team_runtime.py", "backend/agent/team_tools.py", "backend/agent/llm_provider.py",
                   "backend/routes/chat_routes.py", "backend/static/index.html", "fast_server.py")}}
    redactor = Redactor()
    write(args.output / "frozen.json", frozen)
    try:
        with (TemporaryDirectory(dir=args.output, prefix="isolated-") as scratch,
              open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink)):
            run(args, frozen, selected, Path(scratch), redactor, summary)
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
