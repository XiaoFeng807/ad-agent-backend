"""Run the frozen TASK-027-T3 matrix against the normal work-group SSE route."""

import argparse
from collections import Counter
from contextlib import ExitStack, closing, contextmanager, redirect_stderr, redirect_stdout
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter, perf_counter_ns
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from scripts.run_real_diagnosis import CheckFailed, Redactor, require
from scripts.task27_t3_fixture import (
    ALERT, ALERT_SQL, ARTIFACT, DOCS, OWNER, PLANS, PLAN_SQL, ROOT, digest, freeze, write,
)
from scripts.task27_t3a_fixture import CASE_CAP, RUN_CAP, VARIANT, apply_variant

OUTPUT = ROOT / "logs/task27-t3"
GROUPS = "/api/chat/conversations"
USERNAME = "task27-t3-owner"
SDK_CAP = 160


class Audit:
    def __init__(self, ledger, redactor, run, ceiling, *, variant=False):
        self.ledger, self.redactor, self.run, self.ceiling = ledger, redactor, run, ceiling
        self.variant = variant
        self.case = self.stop = None
        self.model_link = ContextVar("model_link", default={})
        self.tool_link = ContextVar("tool_link", default={})
        self.reads, self.work, self.receipts = [], [], []
        with closing(sqlite3.connect(ledger)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sdk (id INTEGER PRIMARY KEY, run TEXT, sample TEXT, "
                         "request TEXT NOT NULL, response TEXT)")

    def request(self, request):
        require(self.case and request.url.scheme == "https" and request.url.host == "api.deepseek.com"
                and request.method == "POST" and request.url.path.endswith("/chat/completions"),
                "unexpected_sdk_destination")
        payload = self.redactor.clean(json.loads(request.content))
        row = {**self.model_link.get(), "start_ns": perf_counter_ns(), "body": payload,
               "sdk_retry": int(request.headers.get("x-stainless-retry-count", "0"))}
        with closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if self.stop or conn.execute("SELECT COUNT(*) FROM sdk").fetchone()[0] >= self.ceiling:
                self.stop = self.stop or "sdk_attempt_limit"
                raise CheckFailed(self.stop)
            if self.variant:
                used = conn.execute("SELECT COUNT(*), COUNT(CASE WHEN sample=? THEN 1 END) FROM sdk WHERE run=?",
                                    (self.case, self.run)).fetchone()
                if used[0] >= RUN_CAP:
                    self.stop = "variant_sdk_attempt_limit"
                    raise CheckFailed(self.stop)
                require(used[1] < CASE_CAP, "round_sdk_attempt_limit")
            attempt = conn.execute("INSERT INTO sdk(run,sample,request) VALUES (?,?,?)",
                                   (self.run, self.case, json.dumps(row, ensure_ascii=False))).lastrowid
        request.extensions["task27_t3_attempt"] = attempt

    def response(self, response):
        row = {"status": response.status_code, "end_ns": perf_counter_ns()}
        if response.status_code == 200 and "text/event-stream" not in response.headers.get("content-type", ""):
            response.read()
            try:
                row["body"] = self.redactor.clean(response.json())
            except ValueError:
                row["failure_reason"] = "response_not_json"
        elif response.status_code != 200:
            row["failure_reason"] = f"provider_http_{response.status_code}"
            if response.status_code in (401, 402, 403, 429):
                self.stop = row["failure_reason"]
        with closing(sqlite3.connect(self.ledger)) as conn, conn:
            conn.execute("UPDATE sdk SET response=? WHERE id=?",
                         (json.dumps(row, ensure_ascii=False), response.request.extensions["task27_t3_attempt"]))

    def rows(self, *, all_runs=False):
        sql = "SELECT id,run,sample,request,response FROM sdk"
        params = () if all_runs else (self.run,)
        with closing(sqlite3.connect(self.ledger)) as conn:
            rows = conn.execute(sql + ("" if all_runs else " WHERE run=?") + " ORDER BY id", params)
            return [{"attempt": key, "run": run, "case": case, **json.loads(req),
                     "response": json.loads(resp) if resp else {"failure_reason": "no_http_response"}}
                    for key, run, case, req, resp in rows]

    def trace_model(self, trace_step):
        @wraps(trace_step)
        def observe(trace_id, stage, action, *args, **kwargs):
            if stage == "model_call":
                self.model_link.set({"trace_id": trace_id, "step": action,
                                     "model_call_id": kwargs.get("runtime_call_id"),
                                     "role": kwargs.get("role", "team_worker"),
                                     "worker_id": kwargs.get("parent_call_id"), "task_id": kwargs.get("detail")})
            return trace_step(trace_id, stage, action, *args, **kwargs)
        return observe

    def trace_tool(self, trace_tool):
        @contextmanager
        def observe(trace_id, role, name, call_id=None, **kwargs):
            link = self.tool_link.set({"trace_id": trace_id, "role": role, "tool_call_id": call_id, **kwargs})
            try:
                with trace_tool(trace_id, role, name, call_id, **kwargs) as step:
                    yield step
            finally:
                self.tool_link.reset(link)
        return observe

    def wrap_read(self, name, original):
        parameters = signature(original)

        @wraps(original)
        def read(*args, **kwargs):
            link = self.tool_link.get()
            if not link:
                return original(*args, **kwargs)  # Exclude dashboard refreshes and fixture preflight.
            bound = parameters.bind(*args, **kwargs)
            bound.apply_defaults()
            row = {**link, "case": self.case, "tool": name, "arguments": dict(bound.arguments),
                   "start_ns": perf_counter_ns(), "nested_under": link.get("active_read")}
            self.reads.append(row)
            token = self.tool_link.set({**link, "active_read": name})
            try:
                result = original(*args, **kwargs)
                if name == "search_knowledge" and isinstance(result, str):
                    try:
                        row["result"] = json.loads(result)
                    except ValueError:
                        row["result"] = result
                else:
                    row["result"] = result
                return result
            except Exception as error:
                row["exception"] = type(error).__name__
                raise
            finally:
                row["end_ns"] = perf_counter_ns()
                self.tool_link.reset(token)
        return read

    def wrap_work(self, original):
        @wraps(original)
        def work(runtime, task, inputs, direct_inputs, attempt, trace_id, worker_id):
            row = {"case": self.case, "trace_id": trace_id, "worker_id": worker_id, "attempt": attempt,
                   "task": task.model_dump(mode="json"), "start_ns": perf_counter_ns()}
            self.work.append(row)
            try:
                output, error = original(runtime, task, inputs, direct_inputs, attempt, trace_id, worker_id)
                row.update(output=output, error_code=error)
                return output, error
            finally:
                row["end_ns"] = perf_counter_ns()
        return work

    def wrap_receipt(self, original):
        @wraps(original)
        def record(runtime, task_id, attempt, trace_id, worker_id, **receipt):
            accepted = original(runtime, task_id, attempt, trace_id, worker_id, **receipt)
            self.receipts.append({"case": self.case, "trace_id": trace_id, "task_id": task_id, "attempt": attempt,
                                  "worker_id": worker_id, "accepted": accepted, "ns": perf_counter_ns(), **receipt})
            return accepted
        return record


def configure(scratch, redactor):
    values = dotenv_values(ROOT / ".env")
    for key in ("LLM_PROVIDER", "API_KEY", "DEEPSEEK_API_KEY", "MODEL", "BASE_URL"):
        if values.get(key):
            os.environ.setdefault(key, values[key])
    secret = token_urlsafe(48)
    redactor.secrets.extend([secret, *(os.environ.get(key) for key in
                                      ("API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"))])
    os.environ.update(AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", ANONYMIZED_TELEMETRY="False",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH=str(scratch / "sandbox.sqlite3"),
                      PLAN_METRICS_DB_PATH=str(ARTIFACT), RAG_DB_PATH=str(scratch / "knowledge"), JWT_SECRET=secret)
    from backend.config.config import settings

    config = settings.llm_config()
    endpoint = urlsplit(config.base_url or "")
    require(config.provider == "deepseek" and config.model and endpoint.scheme == "https"
            and endpoint.netloc == "api.deepseek.com" and config.api_key, "configured_deepseek_required")
    return settings, config


def seed(database, password):
    database.init_db()
    with closing(database.get_db()) as conn, conn:
        conn.execute("INSERT INTO users (id,username,password,role) VALUES (?,?,?,'user')",
                     (OWNER, USERNAME, database.hash_password(password)))
        conn.executemany("INSERT INTO ad_plans(id,plan_name,status,daily_budget,user_id) "
                         "VALUES(:id,:plan_name,:status,:daily_budget,:user_id)", PLANS)
        conn.execute("INSERT INTO alerts(type,message,level,is_read,user_id,created_at) "
                     "VALUES(:type,:message,:level,:is_read,:user_id,'2026-10-31 09:00:00')", ALERT)
        require([dict(row) for row in conn.execute(PLAN_SQL, (OWNER,))] == PLANS, "unexpected_plan_fixture")
        require([dict(row) for row in conn.execute(ALERT_SQL, (OWNER,))] == [ALERT], "unexpected_alert_fixture")


def bind(case, source, database, settings, scratch):
    metrics = ARTIFACT
    if omitted := case.get("excluded_row"):
        metrics = scratch / (case["id"] + ".sqlite3")
        shutil.copyfile(ARTIFACT, metrics)
        with closing(sqlite3.connect(metrics)) as conn, conn:
            deleted = conn.execute("DELETE FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date=?",
                                   (omitted["source"], omitted["date"])).rowcount
            require(deleted == 1, "expected_one_history_row_to_remove")
    settings.PLAN_METRICS_DB_PATH = str(metrics)
    with closing(database.get_db()) as conn, conn:
        conn.execute("DELETE FROM plan_metric_bindings")
        conn.executemany("INSERT INTO plan_metric_bindings VALUES (?,?,?,?)",
                         [(int(plan), source["source_id"], source["data_version"], campaign)
                          for plan, campaign in case["bindings"].items() if campaign])
        return {"bindings": [dict(row) for row in conn.execute("SELECT * FROM plan_metric_bindings ORDER BY plan_id")],
                "history_sha256": digest(metrics), "excluded_row": omitted}


def login(client, password, redactor):
    captcha = client.get("/api/captcha").json()
    response = client.post("/api/login", json={"username": USERNAME, "password": password,
                                               "captcha_id": captcha["captcha_id"], "captcha_text": "0726"})
    require(response.status_code == 200 and response.json()["code"] == 200, "fixture_login_failed")
    token = response.json()["data"]["token"]
    redactor.secrets.append(token)
    client.headers["Authorization"] = f"Bearer {token}"


def http_round(client, case, sample):
    group = client.post(GROUPS, json={"title": f"27-T3 {case['id']}"})
    require(group.status_code == 201, "group_creation_failed")
    group_id = group.json()["data"]["conversation_id"]
    sample["request"] = {"conversation_id": group_id, "message": case["message"]}
    with client.stream("POST", "/api/chat/stream", json=sample["request"]) as response:
        sample["http_status"] = response.status_code
        for line in response.iter_lines():
            if line.startswith("data: "):
                sample["sse"].append(json.loads(line[6:]))
    history = client.get(f"{GROUPS}/{group_id}/history")
    history.raise_for_status()
    sample["history"] = history.json()["data"]


def browser_round(url, folder, case, sample, password, redactor):
    write(folder / "browser-input.json", {"title": f"27-T3 {case['id']}", "message": case["message"],
                                        "username": USERNAME})
    target = folder / "browser.json"
    env = {key: value for key, value in os.environ.items() if key.upper() not in
           {"API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "JWT_SECRET"}}
    env["AD_TASK27_T3_PASSWORD"] = password
    try:
        process = subprocess.run(["node", str(ROOT / "tests/task27_t3_browser.cjs"), url,
                                  str(folder / "browser-input.json"), str(target)], cwd=ROOT,
                                 env=env, capture_output=True, timeout=135)
        if process.returncode:
            (folder / "browser-error.txt").write_text(redactor.clean(process.stderr.decode(errors="replace")),
                                                       encoding="utf-8")
            sample["browser_failed"] = True
    finally:
        if target.exists():
            sample.update(json.loads(target.read_text(encoding="utf-8")))


def summarize(sample, audit, observability):
    case = sample["id"]
    group_id = sample.get("request", {}).get("conversation_id")
    traces = [t for t in observability.list_recent_traces() if any(s["stage"] == "chat_group"
              and s["detail"] == group_id for s in t["steps"])]
    sample["trace"] = traces[0] if len(traces) == 1 else None
    sdk = [row for row in audit.rows() if row["case"] == case]
    sample["reads"] = [row for row in audit.reads if row["case"] == case]
    sample["workers"] = [row for row in audit.work if row["case"] == case]
    sample["admission"] = [row for row in audit.receipts if row["case"] == case]
    counts = Counter((r["tool"], json.dumps(r["arguments"], sort_keys=True)) for r in sample["reads"]
                     if r["tool"] not in {x["nested_under"] for x in sample["reads"]})
    sample["sdk_attempts"] = [row["attempt"] for row in sdk]
    sample["model_calls"] = len({row["model_call_id"] for row in sdk if row.get("model_call_id")})
    sample["tokens"] = dict(sum_counters(row["response"].get("body", {}).get("usage", {}) for row in sdk))
    sample["read_count"] = sum(counts.values())
    sample["duplicate_reads"] = sum(count - 1 for count in counts.values())
    sample["answer"] = "".join(event.get("content", "") for event in sample["sse"] if event["type"] == "text")


def sum_counters(values):
    totals = Counter()
    for value in values:
        totals.update({key: count for key, count in value.items() if type(count) is int})
    return totals


def run(args, output, frozen, selected, scratch, redactor, summary):
    settings, config = configure(scratch, redactor)
    summary.update(provider=config.provider, model=config.model, endpoint=config.base_url, embedding="FixedReviewEmbedding")
    from chromadb.api.shared_system_client import SharedSystemClient
    import uvicorn
    from backend import observability
    from backend.agent import home_react, llm_provider, team_runtime, team_tools
    from backend.agent.conditional_detail import parse_detail_condition
    from backend.captcha import captcha
    from backend.database import database
    from backend.memory import conversation_memory
    from backend.rag import rag_knowledge
    from backend.rag.review_rule_demo import seed_review_rules
    from backend.tools import tools
    from scripts.eval_review_retrieval import FixedReviewEmbedding
    from tests.home_diagnosis_server import demo_captcha

    audit = Audit(args.ledger, redactor, summary["run_id"], 0 if args.preflight else args.sdk_ceiling,
                  variant=args.variant == VARIANT)
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
        for name in ("TRACE_LOG_FILE", "FUNC_LOG_FILE", "ERROR_LOG_FILE"):
            stack.enter_context(patch.object(observability, name, Path(os.devnull)))
        for module in (conversation_memory, rag_knowledge):
            stack.enter_context(patch.object(module, "BGEEmbedding", FixedReviewEmbedding))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        for module in (llm_provider, team_runtime):
            stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        for module in (home_react, team_tools):
            stack.enter_context(patch.object(module, "trace_tool_call", audit.trace_tool(module.trace_tool_call)))
        stack.enter_context(patch.object(team_runtime.DelegationRun, "_work", audit.wrap_work(team_runtime.DelegationRun._work)))
        stack.enter_context(patch.object(team_runtime.DelegationRun, "_record",
                                         audit.wrap_receipt(team_runtime.DelegationRun._record)))
        for name in home_react.READ_SCHEMAS:
            wrapped = audit.wrap_read(name, getattr(tools, name))
            stack.enter_context(patch.object(tools, name, wrapped))
            stack.enter_context(patch.dict(tools.function_map, {name: wrapped}))

        from fast_server import app
        rag = rag_knowledge.get_rag_knowledge()
        seed_review_rules(rag)
        if args.variant:
            formal = json.loads((OUTPUT / "formal/summary.json").read_text(encoding="utf-8"))
            require(all(summary[key] == formal[key] for key in
                        ("provider", "model", "endpoint", "embedding", "source_hashes")),
                    "variant_configuration_differs_from_original")
            classifications = {case["id"]: parse_detail_condition(case["message"]) for case in selected}
            summary["entry_preflight"] = {key: {"endpoint": "/api/chat/stream", "payload": ["message", "conversation_id"],
                                               "parse_detail_condition": result}
                                          for key, result in classifications.items()}
            require(all(result == (False, None) for result in classifications.values()), "variant_route_changed")
            summary["sdk_before"] = len(audit.rows(all_runs=True))
        for case in selected:
            bind(case, frozen["source"], database, settings, scratch)
            if case["kind"] == "simple":
                continue
            expected = case["expected"]
            rule = expected["rule"]
            reply = tools.search_knowledge(rule["title"] + " " + rule["version"])
            if isinstance(reply, str):
                reply = json.loads(reply)
            require(any(hit["chunk_id"] == rule["chunk_id"] and hit["content"] == rule["content"]
                        for hit in reply["evidence"]), "rule_preflight_failed")
            for plan_id, plan in expected["metrics"].items():
                for row in plan["windows"]:
                    result = tools.get_plan_metrics_by_date(int(plan_id), row["start_date"], row["end_date"], OWNER)
                    if plan["unbound"]:
                        require(result.get("error_code") == "plan_history_not_mapped", "unbound_preflight_failed")
                        break
                    coverage = result["coverage"]
                    require(all(coverage[key] == row[key] for key in ("expected_days", "observed_days", "missing_dates"))
                            and (row["missing_dates"] or all(result[key] == row[key] for key in ("impressions", "clicks"))),
                            "fixture_does_not_match_sql")
        summary["local_preflight"] = True
        if args.preflight:
            summary.update(sdk_attempts=len(audit.rows()), task_sdk_attempts=len(audit.rows(all_runs=True)))
            require(summary["sdk_attempts"] == 0 and not audit.stop, "preflight_sdk_attempt")
            return

        listener = stack.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
        thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        samples = []
        try:
            client = stack.enter_context(httpx.Client(base_url=url, timeout=110, trust_env=False))
            for _ in range(100):
                if server.started or not thread.is_alive():
                    break
                thread.join(0.05)
            require(server.started, "loopback_server_not_ready")
            for case in selected:
                folder = output / case["id"]
                folder.mkdir()
                sample = {"id": case["id"], "sse": [], "entry": "http" if args.http else "chromium"}
                if audit.stop:
                    sample["unexecuted"] = audit.stop
                    samples.append(sample)
                    continue
                sample["fixture"] = bind(case, frozen["source"], database, settings, scratch)
                audit.case = case["id"]
                started = perf_counter()
                try:
                    if not args.http:
                        browser_round(url, folder, case, sample, password, redactor)
                    if args.variant and not sample.get("request"):
                        raise CheckFailed("chromium_did_not_post_round")
                    if args.http or not sample.get("request"):
                        require(not any(row["case"] == case["id"] for row in audit.rows()), "will_not_repeat_sdk_round")
                        if not args.http:
                            sample["entry"] = "http_after_browser_setup_failure"
                        login(client, password, redactor)
                        http_round(client, case, sample)
                except Exception as error:
                    sample["failure"] = str(error) if isinstance(error, CheckFailed) else type(error).__name__
                    if isinstance(error, (httpx.TimeoutException, subprocess.TimeoutExpired)):
                        audit.stop = "evaluation_transport_timeout"
                finally:
                    sample["elapsed_seconds"] = sample.get("browser_round_seconds", round(perf_counter() - started, 3))
                    summarize(sample, audit, observability)
                    samples.append(sample)
                    write(folder / "round.json", redactor.clean(sample))
                    write(output / "rounds.json", redactor.clean(samples))
                    write(output / "sdk.json", audit.rows())
                    audit.case = None
                    print(json.dumps({"case": case["id"], "sdk": len(sample["sdk_attempts"]),
                                      "workers": len(sample["workers"]), "trace": (sample["trace"] or {}).get("trace_id"),
                                      "terminal": [row["type"] for row in sample["sse"] if row["type"] in {"done", "error"}],
                                      "failure": sample.get("failure")}), file=sys.__stdout__, flush=True)
                    rows = audit.rows()
                    if rows and all(row["response"].get("status") is None for row in rows):
                        audit.stop = audit.stop or "provider_no_http_response"
        finally:
            server.should_exit = True
            thread.join(35)
            summary.update(sdk_attempts=len(audit.rows()), task_sdk_attempts=len(audit.rows(all_runs=True)), stop=audit.stop)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=lambda path: Path(path).resolve())
    parser.add_argument("--frozen", type=Path, default=DOCS / "frozen.json")
    parser.add_argument("--ledger", type=lambda path: Path(path).resolve(), default=OUTPUT / "sdk-ledger.sqlite3")
    parser.add_argument("--sdk-ceiling", type=int, default=SDK_CAP, help="cumulative ceiling, including prior runs in this ledger")
    parser.add_argument("--case", action="append")
    parser.add_argument("--variant", choices=[VARIANT], help="run the separately frozen T3A Chromium comparison")
    parser.add_argument("--preflight", action="store_true", help="SQL and fixture checks, zero SDK attempts")
    parser.add_argument("--http", action="store_true")
    args = parser.parse_args()
    require(0 <= args.sdk_ceiling <= SDK_CAP, "invalid_sdk_ceiling")
    frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
    oracle = json.loads(json.dumps(freeze()))
    require({k: v for k, v in frozen.items() if k != "frozen_utc"} ==
            {k: v for k, v in oracle.items() if k != "frozen_utc"}, "frozen_fixture_mismatch")
    if args.variant:
        require(not args.http and args.ledger == (OUTPUT / "sdk-ledger.sqlite3").resolve()
                and args.ledger.is_file(), "variant_requires_chromium_and_shared_ledger")
        frozen = apply_variant(frozen, args.frozen)
    require(not args.case or set(args.case) <= {case["id"] for case in frozen["cases"]}, "unknown_case")
    selected = [case for case in frozen["cases"] if not args.case or case["id"] in args.case]
    output = args.output
    output.mkdir(parents=True)
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    if args.variant:
        write(output / "frozen.json", frozen)
    else:
        shutil.copyfile(args.frozen, output / "frozen.json")
    protected = [ROOT / "ad_agent.db", ARTIFACT]
    before = {str(path.relative_to(ROOT)): digest(path) for path in protected}
    summary = {"run_id": uuid4().hex, "frozen_sha256": digest(output / "frozen.json"), "sdk_limit": args.sdk_ceiling,
               "selected": [case["id"] for case in selected], "storage_before": before,
               "source_hashes": {name: digest(ROOT / name) for name in (
                   "backend/agent/home_react.py", "backend/agent/orchestrator.py", "backend/agent/team_runtime.py",
                   "backend/agent/team_tools.py", "backend/agent/llm_provider.py", "fast_server.py")}}
    if args.variant:
        summary.update(variant=args.variant, base_frozen_sha256=digest(args.frozen),
                       round_sdk_limit=CASE_CAP, run_sdk_limit=RUN_CAP)
    redactor = Redactor()
    try:
        with (TemporaryDirectory(dir=output, prefix="isolated-") as scratch,
              open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink)):
            run(args, output, frozen, selected, Path(scratch), redactor, summary)
        return 0
    except Exception as error:
        summary["failure"] = str(error) if isinstance(error, CheckFailed) else type(error).__name__
        print(summary["failure"], file=sys.stderr)
        return 1
    finally:
        after = {str(path.relative_to(ROOT)): digest(path) for path in protected}
        summary["storage_after"] = after
        write(output / "summary.json", redactor.clean(summary))
        require(before == after, "protected_storage_changed")


if __name__ == "__main__":
    raise SystemExit(main())
