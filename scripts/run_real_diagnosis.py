"""Two real-provider rounds through POST /api/chat/stream, with isolated demo data.

No model replacement is available here. --preflight never sends a model request.
Only allowlisted SDK body fields are archived; headers and login responses are excluded.
"""

import argparse
import asyncio
from collections import Counter
from contextlib import closing, redirect_stderr, redirect_stdout
import importlib
import json
import logging
import os
from pathlib import Path
import re
from secrets import token_urlsafe
from tempfile import mkdtemp
from threading import Lock
from time import perf_counter
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
RULE_ID = "ctr-review-v1#low-ctr"
PLAN_ID, USER_ID = 101, 17
WINDOWS = (
    {"baseline": {"start_date": "2026-10-19", "end_date": "2026-10-25"},
     "current": {"start_date": "2026-10-26", "end_date": "2026-11-01"}},
    {"baseline": {"start_date": "2026-10-23", "end_date": "2026-10-25"},
     "current": {"start_date": "2026-10-30", "end_date": "2026-11-01"}},
)
PLAN_WINDOWS = (
    {"baseline": {"start_date": "2026-10-15", "end_date": "2026-10-21"},
     "current": {"start_date": "2026-10-22", "end_date": "2026-10-28"}},
    {"baseline": {"start_date": "2026-10-19", "end_date": "2026-10-21"},
     "current": {"start_date": "2026-10-26", "end_date": "2026-10-28"}},
)
ROUND_TIMEOUT = 300  # Same outer boundary as the existing acceptance runner.


class CheckFailed(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = [value for value in secrets if value]

    def clean(self, value):
        # Never serialize request headers, arbitrary settings, or exception messages.
        if isinstance(value, dict):
            return {self.clean(key): self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if not isinstance(value, str):
            return value
        for secret in sorted(filter(None, self.secrets), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"]+", "[REDACTED]", value)
        return re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", value)


class SDKWireAudit:
    """Observe the real SDK transport without replacing its requests or responses."""

    def __init__(self, redactor, *, capture_reply=False, capture_fixture_inputs=False):
        self.redactor = redactor
        self.capture_reply = capture_reply
        self.capture_fixture_inputs = capture_fixture_inputs
        self.rows = []
        self.trace_id = None
        self._lock = Lock()

    def request(self, request):
        body = json.loads(request.content)
        row = {
            "trace_id": self.trace_id, "model": body.get("model"),
            "tools": body.get("tools"), "stream": body.get("stream", False),
            "timeout": request.extensions.get("timeout"),
            "assistant_calls": [call for msg in body.get("messages", [])
                                for call in msg.get("tool_calls") or []],
            "tool_receipts": [{"tool_call_id": msg["tool_call_id"], "content": msg.get("content")}
                              for msg in body.get("messages", []) if msg.get("role") == "tool"],
        }
        if self.capture_fixture_inputs:
            row["input_messages"] = [{key: msg[key] for key in ("role", "content") if key in msg}
                                     for msg in body.get("messages", []) if msg["role"] in ("system", "user")]
            row["tool_choice"] = body.get("tool_choice")
        with self._lock:
            self.rows.append(self.redactor.clean(row))
            request.extensions["diagnosis_audit"] = (len(self.rows) - 1, perf_counter())

    def response(self, response):
        index, start = response.request.extensions["diagnosis_audit"]
        row = self.rows[index]
        row["status"] = response.status_code
        if response.status_code != 200:
            row["elapsed_seconds"] = round(perf_counter() - start, 3)
            row["failure_reason"] = f"provider_http_{response.status_code}"
            return
        response.read()  # Diagnosis currently uses non-streaming SDK completions behind SSE.
        row["elapsed_seconds"] = round(perf_counter() - start, 3)
        payload = response.json()
        row["response_model"] = self.redactor.clean(payload.get("model"))
        if isinstance(payload.get("usage"), dict):
            row["usage"] = {key: count for key, count in payload["usage"].items() if type(count) is int}
        row["response_calls"] = self.redactor.clean([
            call for choice in payload.get("choices", [])
            for call in choice.get("message", {}).get("tool_calls") or []
        ])
        if self.capture_reply:
            row["response_text"] = self.redactor.clean([
                choice.get("message", {}).get("content") for choice in payload.get("choices", [])
            ])

    def attach(self, provider):
        provider._client._client.event_hooks["request"].append(self.request)
        provider._client._client.event_hooks["response"].append(self.response)


def prepare_isolation(directory, embedding, report, metrics_db=None, source_plan_id="13289562"):
    from backend import observability
    from backend.config.config import settings
    from backend.database import database
    from backend.memory import memory_manager

    database.DB_PATH = directory / "demo.sqlite3"
    memory_manager.MEMORY_DIR = directory / "memory"
    settings.RAG_DB_PATH = str(directory / "chroma")
    # Legacy logs may contain provider exception bodies. Only sanitized traces are archived below.
    observability.TRACE_LOG_FILE = Path(os.devnull)
    observability.FUNC_LOG_FILE = Path(os.devnull)
    observability.ERROR_LOG_FILE = Path(os.devnull)
    database.init_db()
    password = token_urlsafe(24)
    with closing(database.get_db()) as conn, conn:
        conn.execute("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                     (USER_ID, "real-provider-demo", database.hash_password(password)))
        conn.execute("INSERT INTO ad_plans (id, plan_name, user_id) VALUES (?, '隔离演示计划', ?)",
                     (PLAN_ID, USER_ID))
        # These remain USER-level reports. No plan history is fabricated or inferred.
        conn.executemany(
            "INSERT INTO daily_reports (report_date, user_id, impressions, clicks) VALUES (?, ?, ?, ?)",
            [(day, USER_ID, impressions, clicks) for day, impressions, clicks in (
                ("2026-10-19", 1500, 120), ("2026-10-23", 1200, 90),
                ("2026-10-26", 1400, 70), ("2026-10-30", 1100, 50))],
        )
    report["preflight"]["sqlite"] = {"ready": True, "isolated": True}
    if metrics_db is not None:
        import sqlite3
        settings.PLAN_METRICS_DB_PATH = str(metrics_db.resolve())
        with closing(sqlite3.connect(metrics_db.resolve().as_uri() + "?mode=ro", uri=True)) as artifact:
            manifest = json.loads(artifact.execute("SELECT manifest FROM metadata").fetchone()[0])
            require(manifest["kind"] == "public", "real_run_requires_public_metrics")
            require(artifact.execute("SELECT 1 FROM plan_daily_metrics WHERE source_plan_id=?", (source_plan_id,)).fetchone(),
                    "source_plan_not_found")
        with closing(database.get_db()) as conn, conn:
            conn.execute("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)",
                         (PLAN_ID, manifest["source_id"], manifest["data_version"], source_plan_id))
        report["preflight"]["plan_metrics"] = {"ready": True, "manifest": manifest, "source_plan_id": source_plan_id}
    rag = None
    try:
        from backend.rag import rag_knowledge
        from backend.rag.review_rule_demo import DEMO_RULES, seed_review_rules

        if embedding == "fixed":
            from scripts.eval_review_retrieval import FixedReviewEmbedding
            rag_knowledge.BGEEmbedding = FixedReviewEmbedding
        rag = rag_knowledge.get_rag_knowledge()
        seed_review_rules(rag)
        expected = DEMO_RULES[0]
        chunk = rag.get_chunk(expected[0])
        evidence = rag.generate(RULE_ID).get("evidence", [])
        require(chunk and chunk["content"] == expected[4], "preflight_rule_original_mismatch")
        require(any(hit["chunk_id"] == expected[0] for hit in evidence), "preflight_rule_retrieval_missing")
        report["preflight"]["knowledge"] = {"ready": True, "embedding": embedding,
                                               "chunks": rag.collection.count(), "chunk_id": expected[0]}
    except Exception as error:
        report["preflight"]["knowledge"] = {"ready": False, "embedding": embedding,
                                               "failure_type": type(error).__name__,
                                               "reason": "demo_rules_or_embedding_not_ready"}
        if isinstance(error, ModuleNotFoundError):
            report["preflight"]["knowledge"]["missing_dependency"] = error.name
    return password, rag


async def stream_round(app, body, token, audit, report):
    """Inspect the DB at ASGI's terminal send, before the route returns to its caller."""
    from backend import observability
    from backend.database import database

    events, observations = [], []
    incoming = [{"type": "http.request", "body": json.dumps(body).encode()}]
    status = None

    async def receive():
        if incoming:
            return incoming.pop()
        await asyncio.Event().wait()

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        for block in message.get("body", b"").decode().split("\n\n"):
            if not block.startswith("data: "):
                continue
            event = json.loads(block[6:])
            events.append(event)
            if event.get("trace_id"):
                audit.trace_id = event["trace_id"]
            if event["type"] in {"done", "error"}:
                trace = observability.get_trace(event["trace_id"])
                with closing(database.get_db()) as conn:
                    rows = conn.execute("SELECT reply FROM diagnosis_turns ORDER BY id").fetchall()
                    proposals = conn.execute("SELECT action_id FROM diagnosis_review_proposals").fetchall()
                observations.append({"trace_status": trace["status"],
                                     "reply_committed": bool(rows and rows[-1]["reply"] is not None),
                                     "reply_matches_text": bool(rows and rows[-1]["reply"] == "".join(
                                         e.get("content", "") for e in events if e["type"] == "text")),
                                     "proposal_committed": event.get("review_proposal", {}).get("action_id")
                                     in {row["action_id"] for row in proposals}})

    started = perf_counter()
    round_result = {"request": body, "events": events, "terminal_observations": observations}
    report["rounds"].append(round_result)
    try:
        await asyncio.wait_for(app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/api/chat/stream", "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode()),
                        (b"content-type", b"application/json")],
        }, receive, send), timeout=ROUND_TIMEOUT)
    finally:
        round_result["elapsed_seconds"] = round(perf_counter() - started, 3)
        round_result["http_status"] = status
        trace_ids = {e["trace_id"] for e in events if e.get("trace_id")}
        round_result["traces"] = [observability.get_trace(trace_id) for trace_id in sorted(trace_ids)]
    require(status == 200, "sse_http_failure")
    require([e["type"] for e in events if e["type"] in {"done", "error"}] == ["done"], "sse_terminal_failure")
    require(events[-1]["type"] == "done", "sse_done_not_last")
    require(len(trace_ids) == 1, "sse_trace_mismatch")
    require(observations == [{"trace_status": "completed", "reply_committed": True,
                             "reply_matches_text": True, "proposal_committed": True}], "commit_order_failure")
    return round_result


async def verify_round(client, result, windows, audit, model, plan_metrics=False):
    from backend.agent.diagnosis import DiagnosisSnapshot
    from backend.rag.review_rule_demo import DEMO_RULES

    events = result["events"]
    proposal = events[-1]["review_proposal"]
    trace = result["traces"][0]
    result["trace_id"] = trace["trace_id"]
    snapshot = DiagnosisSnapshot(task_id=proposal["task_id"], plan_id=PLAN_ID, rule_id=RULE_ID, **windows)
    require(all(proposal[key] == value for key, value in snapshot.model_dump().items()), "task_snapshot_mismatch")
    require(proposal["source_trace_id"] == trace["trace_id"], "proposal_trace_mismatch")
    text_events = [event for event in events if event["type"] == "text"]
    require(text_events, "reply_text_missing")
    citations = [citation for event in text_events for citation in event.get("citations", [])]
    require(citations and citations == proposal["citations"], "current_round_citations_missing")
    if plan_metrics:
        from fractions import Fraction
        import math
        require(not proposal["evidence_gaps"], "plan_metrics_evidence_incomplete")
        require(set(proposal["metrics"]) == {"baseline", "current"}, "plan_metrics_snapshot_missing")
        base, current = (proposal["metrics"][key] for key in ("baseline", "current"))
        b, c = Fraction(base["clicks"], base["impressions"]), Fraction(current["clicks"], current["impressions"])
        expected = {"baseline_ctr_pct": b*100, "current_ctr_pct": c*100,
                    "decline_pp": (b-c)*100, "relative_decline_pct": (b-c)/b*100}
        require(all(math.isclose(proposal["calculation"][k], float(v), abs_tol=1e-10) for k,v in expected.items()),
                "numeric_recalculation_mismatch")
        require(proposal["rule_evaluation"]["rule_id"] == RULE_ID, "exact_rule_evaluation_missing")
    else:
        require(any("历史 CTR" in gap for gap in proposal["evidence_gaps"]), "plan_history_gap_missing")
    originals = []
    for citation in citations:
        response = await client.get(citation["url"])
        require(response.status_code == 200, "rule_original_http_failure")
        chunk = response.json()
        expected = next(rule for rule in DEMO_RULES if rule[0] == citation["chunk_id"])
        require(expected[2] == "v1" and chunk["content"] == expected[4], "rule_original_mismatch")
        originals.append(chunk)
    result["rule_originals"] = originals
    rows = [row for row in audit.rows if row["trace_id"] == trace["trace_id"]]
    require(rows and all(row["model"] == model for row in rows), "sdk_model_mismatch")
    require(all(row.get("status") == 200 for row in rows), "sdk_request_failure")
    calls = [call for row in rows for call in row.get("response_calls", [])]
    leaves = [call for call in calls if call["function"]["name"] in
              {"get_plan_detail", "get_daily_report_by_date", "get_plan_metrics_by_date", "search_knowledge"}]
    actual = []
    for call in leaves:
        name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"])
        require(snapshot.permits_tool(name, args), "actual_tool_arguments_mismatch")
        # The existing tool schema declares plan_id as a string; the guard accepts either form.
        comparable = {**args, "plan_id": str(args["plan_id"])} if "plan_id" in args else args
        actual.append((name, comparable))
    metric_tool = "get_plan_metrics_by_date" if plan_metrics else "get_daily_report_by_date"
    metric_scope = {"plan_id": str(PLAN_ID)} if plan_metrics else {}
    expected_calls = [("get_plan_detail", {"plan_id": str(PLAN_ID)}),
                      (metric_tool, {**metric_scope, **windows["baseline"]}),
                      (metric_tool, {**metric_scope, **windows["current"]}),
                      ("search_knowledge", {"query": RULE_ID})]
    require(all(call in actual for call in expected_calls), "required_model_tool_call_missing")
    receipts = {msg["tool_call_id"] for row in rows for msg in row["tool_receipts"]}
    require(all(call["id"] in receipts for call in calls), "sdk_tool_receipt_missing")
    for row in rows:
        ids = {call["id"] for call in row["assistant_calls"]}
        require(all(msg["tool_call_id"] in ids for msg in row["tool_receipts"]), "sdk_receipt_id_mismatch")
    trace_calls = [step for step in trace["steps"] if step["stage"] == "tool_call"]
    trace_results = [step for step in trace["steps"] if step["stage"] == "tool_result"]
    require(Counter(step["runtime_call_id"] for step in trace_calls) ==
            Counter(step["runtime_call_id"] for step in trace_results), "trace_tool_pair_mismatch")
    require(all(any(step.get("tool_call_id") == call["id"] and step["action"] == call["function"]["name"]
                    for step in trace_calls) for call in leaves), "sdk_trace_tool_mismatch")
    require(not any(step.get("error_code") for step in trace_results), "trace_tool_failure")
    require(len(trace_calls) <= 12, "execution_budget_exceeded")
    result["checks"] = {key: True for key in ("actual_model", "actual_tool_arguments", "task_snapshot",
                       "current_citations_and_original", "tool_receipts", "sse_and_trace", "commit_before_done")}
    return proposal


async def execute(directory, password, report, redactor, windows_list=WINDOWS, plan_metrics=False):
    import httpx
    from backend.agent.llm_provider import ProviderFactory
    from backend.database import database

    provider = ProviderFactory.create()
    audit = SDKWireAudit(redactor)
    audit.attach(provider)
    report["sdk_requests"] = audit.rows
    try:
        server = importlib.import_module("fast_server")
        from backend.agent.agent_pool import AgentPool
        server.agent_pool = AgentPool(min_size=1, max_size=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://demo.local") as client:
            login = await client.post("/api/login", json={"username": "real-provider-demo", "password": password})
            require(login.status_code == 200 and login.json().get("code") == 200, "demo_login_failure")
            token = login.json()["data"]["token"]
            redactor.secrets.append(token)
            client.headers["authorization"] = f"Bearer {token}"
            report["preflight"]["jwt"]["session_verified"] = bool(server.auth_instance.validate_token(token))
            require(report["preflight"]["jwt"]["session_verified"], "demo_jwt_session_failure")
            first = None
            for index, windows in enumerate(windows_list):
                diagnosis = {"plan_id": PLAN_ID, "rule_id": RULE_ID, **windows} if first is None else {
                    "task_id": first["task_id"], **windows}
                body = {"message": "按本轮参数查询计划历史曝光与点击，计算两窗 CTR、百分点差和相对降幅，检索精确版本规则后诊断并引用；请明确证据缺项。",
                        "diagnosis": diagnosis}
                result = await stream_round(server.app, body, token, audit, report)
                proposal = await verify_round(client, result, windows, audit, report["model"], plan_metrics)
                if index == 0:
                    first = proposal
                    # Force restoration from persistent task state using a fresh Agent pool.
                    server.agent_pool = AgentPool(min_size=1, max_size=1)
                else:
                    require(first["task_id"] == proposal["task_id"], "task_restore_failure")
                    require(first["source_trace_id"] != proposal["source_trace_id"], "round_trace_reused")
            response = await client.post(f"/api/diagnosis/review-proposals/{first['action_id']}/confirm")
            require(response.status_code == 200, "confirm_first_proposal_failure")
            receipt = response.json()["data"]
            response = await client.get(f"/api/diagnosis/reviews/{receipt['review_id']}")
            require(response.status_code == 200, "review_readback_failure")
            readback = response.json()["data"]
            require(readback == receipt and all(readback[key] == value for key, value in first.items()),
                    "first_round_snapshot_changed")
            report["review"] = {"confirmed_after_round": 2, "receipt": receipt, "get_readback": readback,
                                "first_round_snapshot_preserved": True}
    finally:
        for row in audit.rows:
            if "status" not in row:
                row["failure_reason"] = "provider_transport_failure_or_timeout"
        # No login token is left in the reusable evidence database.
        with closing(database.get_db()) as conn, conn:
            conn.execute("DELETE FROM sessions")
        provider._client.close()


def main(argv=None):
    import sys

    argv = sys.argv[1:] if argv is None else argv
    if "--natural" in argv:
        from scripts.run_natural_diagnosis import main as natural_main

        return natural_main([arg for arg in argv if arg != "--natural"])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="check local readiness without model calls")
    parser.add_argument("--embedding", choices=("bge", "fixed"), default="bge",
                        help="fixed isolates provider testing from BGE; never replaces the LLM")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "logs" / "real-diagnosis")
    parser.add_argument("--plan-metrics-db", type=Path, help="use the public read-only plan artifact and mapped demo windows")
    parser.add_argument("--source-plan-id", default="13289562", help="server-side campaign mapping for isolated plan 101")
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    directory = Path(mkdtemp(prefix="run-", dir=args.output_dir.resolve()))
    report = {"status": "待运行", "real_model_executed": False, "preflight": {}, "rounds": [],
              "sdk_requests": [], "failures": [], "limits": {"tools_per_round": 12,
              "react_rounds": 5, "sdk_timeout_seconds": 30, "sdk_default_retries": 2,
              "outer_round_timeout_seconds": ROUND_TIMEOUT},
              "scope": {"embedding": args.embedding, "model": "real ProviderFactory only",
                        "historical_plan_ctr": "未覆盖；用户级日报不能代替计划历史指标",
                        "generation_quality": "未验收开放生成质量；最终缺项回复由服务端证据守卫生成",
                        "platform_execution": "未覆盖；只创建本地复核单"}}
    redactor = Redactor()
    rag = None
    started = perf_counter()
    disabled = logging.root.manager.disable
    try:
        # Suppress legacy free-form output. Evidence is exported exclusively through the redactor.
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            logging.disable(logging.CRITICAL)
            os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False")
            from backend.config.config import settings
            from backend.auth.auth import _signing_key

            config = settings.llm_config()
            redactor.secrets.extend(getattr(settings, key) for key in
                                   ("API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GLM_API_KEY", "JWT_SECRET"))
            redactor.secrets.append(config.api_key)
            report.update(provider=config.provider, model=config.model)
            key_ready = bool(config.api_key.strip()) and not config.api_key.lower().startswith(("your_", "replace"))
            endpoint = urlsplit(config.base_url or "https://api.openai.com/v1")
            endpoint_ready = bool(endpoint.hostname) and endpoint.scheme in {"http", "https"}
            report["preflight"]["model"] = {
                "ready": config.provider != "mock" and key_ready and bool(config.model.strip()) and endpoint_ready,
                "credential_ready": key_ready, "endpoint_ready": endpoint_ready,
                "required": {"openai": ["OPENAI_API_KEY", "OPENAI_MODEL"],
                             "glm": ["GLM_API_KEY"],
                             "deepseek": ["API_KEY or DEEPSEEK_API_KEY", "MODEL", "BASE_URL"]}.get(config.provider, [])}
            try:
                _signing_key()
                jwt_ready = True
            except Exception:
                jwt_ready = False
            report["preflight"]["jwt"] = {"ready": jwt_ready, "required": ["JWT_SECRET"]}
            password, rag = prepare_isolation(directory, args.embedding, report, args.plan_metrics_db, args.source_plan_id)
            if args.plan_metrics_db:
                report["scope"]["historical_plan_ctr"] = "公开 Criteo 同计划两窗，数字与来源版本快照及服务端独立复算"
                report["scope"]["generation_quality"] = "最终数值与规则判定由服务端已验证证据生成；开放生成质量单独验收"
            redactor.secrets.append(password)
            ready = all(check["ready"] for check in report["preflight"].values())
            report["preflight_ready"] = ready
            if not ready:
                report["failures"] = [f"preflight_{name}_not_ready" for name, check in report["preflight"].items()
                                      if not check["ready"]]
            elif args.preflight:
                report["status"] = "预检通过；真实模型待运行"
            else:
                report["status"] = "运行中"
                asyncio.run(execute(directory, password, report, redactor,
                                    PLAN_WINDOWS if args.plan_metrics_db else WINDOWS, bool(args.plan_metrics_db)))
                report["status"] = "通过"
    except Exception as error:
        code = str(error) if isinstance(error, CheckFailed) else type(error).__name__
        report["failures"].append(code)
        report["status"] = "失败"
    finally:
        logging.disable(disabled)
        if rag is not None:
            rag.close()
            from backend.rag.rag_knowledge import _open_knowledge
            _open_knowledge.cache_clear()
        report["real_model_executed"] = bool(report["sdk_requests"])
        report["elapsed_seconds"] = round(perf_counter() - started, 3)
        safe = redactor.clean(report)
        (directory / "report.json").write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        traces = [trace for result in safe["rounds"] for trace in result.get("traces", [])]
        (directory / "traces.jsonl").write_text("".join(json.dumps(trace, ensure_ascii=False) + "\n" for trace in traces), encoding="utf-8")
        print(f"{safe['status']} · Provider={safe.get('provider', 'unknown')} · model={safe.get('model', 'unknown')}")
        print(f"真实 SDK 请求 {len(safe['sdk_requests'])} · 完成轮次 {sum('checks' in r for r in safe['rounds'])}/2")
        print(f"脱敏报告 {directory / 'report.json'}")
    return 0 if report["status"] in {"通过", "预检通过；真实模型待运行"} else (2 if report["status"] == "待运行" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
