"""Independently replay Listing and compare its two tool configurations."""

import argparse
from collections import Counter
from contextlib import ExitStack, closing
from contextvars import ContextVar
from datetime import datetime, timedelta
from functools import wraps
from hashlib import sha256
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import subprocess
from time import monotonic
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values

from scripts import task42_t1_workgroup as t1
from scripts import verify_task42_t2_live as t2
from scripts.accept_task42_t1 import configure, read, serve, write
from scripts.run_real_diagnosis import Redactor
from scripts.run_task27_t5 import ENDPOINT, MODEL, browser_env


TARGET = {"shop_id": "task42-test", "marketplace_id": "US", "sku": t2.SKU}
WINDOWS = (t2.FIRST, "2026-11-05", "2026-11-06", t2.LAST)
WINDOW_FIELDS = ("baseline_start", "baseline_end", "current_start", "current_end")
ARMS = ("direct", "dynamic")
REQUEST = ContextVar("task42_http", default=None)


def freeze():
    prior = {case["id"]: case for case in t2.freeze()}

    def case(name, message, variant, reads, expected, value=None):
        return {"id": name, "message": message, "message_sha256": sha256(message.encode()).hexdigest(),
                "variant": variant, "expected_reads": reads, "expected": expected, "expect_value": value}

    all_reads = [t2.SALES, t2.LISTING, t2.FACTS]
    natural = case("natural-sales", t1.ORIGINAL_SIMPLE, "original", [t2.SALES], "sales")
    conditional = (f"查 SKU {t2.SKU} 在 {t2.RANGE} 的两周销量。只有两窗销量数据完整才核对在售五点和商品资料，"
                   "重写英文五点，把经核验的容量放第一条，先给我审批。缺数据就告诉我缺什么。")
    material = (f"结合 SKU {t2.SKU} 在 {t2.RANGE} 的销量和当前商品资料重写英文五点。"
                "必须写不锈钢，商品资料无依据时说明缺什么。")
    fixed = [natural, *(prior[key] for key in ("read-original-bullets", "explicit-no-draft",
             "other-shop-same-sku", "relative-thirty-days", "sales-gap-conditional", "unsupported-waterproof"))]
    spot = [natural, {**prior["capacity-changed"], "variant": "capacity-only"},
            prior["insufficient-facts"], prior["browser-complete"]]
    paired = [
        natural, prior["read-original-bullets"],
        case("independent-evidence", f"我要更新 SKU {t2.SKU} 的英文五点。请核对 {t2.RANGE} 的两周销量、"
             "在售五点和已核验的商品资料，给我一份可审批的五点草案。",
             "original", all_reads, "draft"),
        case("conditional-complete", conditional, "original", all_reads, "capacity", "500 mL"),
        case("conditional-gap", conditional, "sales-gap", [t2.SALES], "gap", "metrics_incomplete"),
        case("material-supported", material, "original", all_reads, "required", "stainless steel"),
        case("material-changed", material, "changed", all_reads, "unsupported", "不锈钢"),
        prior["explicit-no-draft"],
    ]
    return {
        "model": MODEL, "arms": list(ARMS), "sdk_cap": 48, "sdk_cap_per_round": 8,
        "rules": {
            "both": "Exact natural request and one committed SSE done. Match SQL days, totals, target and evidence "
                    "payload. Match SDK calls, runtime IDs and persisted evidence. Use exact verified wording. "
                    "Missing prerequisites yield zero dependent business reads, drafts and writes. No writes before approval.",
            "arm": "Only task admission is disabled in direct arm. Prompts, data, model and request budgets are shared. "
                   "Business pass does not require delegation. Record attempted and admitted delegation separately.",
            "handoff": "Join task sheet, dependencies, first Worker input and board receipts by artifact ID. "
                       "Compare tool call ID, source, arguments and result. Match each created Worker to its reclamation.",
            "material": "The same request yields a stainless-steel draft with the original facts and a specific "
                        "evidence gap with aluminum facts. Neither preview changes product or receipt rows.",
            "metrics": "Count actual SDK sends including retries, provider usage, SSE wall time, distinct admitted reads, "
                       "duplicate tool attempts, exact reader invocations by phase and Worker reclamation.",
        },
        "fixed": fixed, "spot": spot, "paired": paired,
    }


def start_request_capture(app, calls):
    async def observed(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        method, path = scope["method"], scope["path"]
        if path == "/api/chat/stream":
            phase = "workgroup"
        elif path.endswith("/confirm"):
            phase = "approve"
        elif "/listing-actions/" in path:
            phase = "action_get"
        else:
            phase = "other"
        row = {"method": method, "path": path, "phase": phase}
        calls.append(row)
        token = REQUEST.set(row)
        started = monotonic()

        async def timed_send(message):
            if message["type"] == "http.response.body" and not message.get("more_body"):
                row["seconds"] = round(monotonic() - started, 4)
            await send(message)

        try:
            return await app(scope, receive, timed_send)
        finally:
            row.setdefault("seconds", round(monotonic() - started, 4))
            REQUEST.reset(token)
    return observed


def measure_reads(original, name, audit, observations):
    @wraps(original)
    def observed(*args, **kwargs):
        request = REQUEST.get() or {}
        # Listing's worker threads have their own context. Every concurrent worker
        # in this serial runner belongs to the active workgroup request.
        phase = request.get("phase", "workgroup")
        link = audit.tool_link.get()
        if phase == "workgroup":
            phase = "business_read" if link else "evidence_revalidation"
        observations.append({"read": name, "phase": phase, "path": request.get("path"),
                             "arguments": list(args[1:]), "tool": link})
        return original(*args, **kwargs)
    return observed


def sales_windows(case, today):
    if case["expect_value"] != "30":
        return WINDOWS
    return tuple((today - timedelta(days=offset)).isoformat() for offset in (30, 16, 15, 1))


def seeded(database, listings, password, case, today):
    variant = case["variant"]
    t2.seed(database, listings, password, variant, today)
    with closing(database.get_db()) as conn, conn:
        owner = conn.execute("SELECT id FROM users WHERE username=?", (t2.OWNER,)).fetchone()[0]
        listings.seed_test_product(conn, owner, sku="TASK42-CONTROL")
        if variant == "capacity-only":
            facts = conn.execute("SELECT facts FROM listing_test_facts WHERE shop_id=? AND sku=?",
                                 (TARGET["shop_id"], t2.SKU)).fetchone()[0]
            conn.execute("UPDATE listing_test_facts SET facts=?,version=version+1 WHERE shop_id=? AND sku=?",
                         (facts.replace("500 mL", "750 mL"), TARGET["shop_id"], t2.SKU))
    return owner


def sql_oracle(path, case, today):
    params = {**TARGET, **dict(zip(WINDOW_FIELDS, sales_windows(case, today), strict=True))}
    with t1.connection(path) as conn:
        return [dict(row) for row in conn.execute(t1.SQL, params)]


def round_response(client, folder, origin, case, password, path, browser):
    if browser:
        env = {**browser_env(), "AD_TASK42_USERNAME": t2.OWNER, "AD_TASK42_PASSWORD": password,
               "AD_TASK42_SQLITE": str(path), "AD_TASK42_MESSAGE": case["message"], "AD_TASK42_NATURAL": "1"}
        process = subprocess.run(["node", "tests/task42_listing_browser.cjs", origin, str(folder)],
                                 env=env, timeout=125, capture_output=True)
        report_path = folder / "listing-browser.json"
        result = read(report_path) if report_path.exists() else {}
        result["browser_exit"] = process.returncode
        result["browser_stderr"] = process.stderr.decode(errors="replace")
        headers = t1.login(client, t2.OWNER, password)
        if result.get("request"):
            result.update(t1.edge_history(client, headers, t1.GROUPS + "/" + result["request"]["conversation_id"]))
        return result
    headers = t1.login(client, t2.OWNER, password)
    result = t1.http_round(client, headers, case["id"], case["message"])
    terminal = next((event for event in result["events"] if event["type"] == "done"), {})
    if run := terminal.get("run_id"):
        evidence = client.get(f"{result['base']}/runs/{run}/evidence", headers=headers)
        result.update(evidence_status=evidence.status_code, evidence=t1.body(evidence))
    result.update(t1.edge_history(client, headers, result["base"]))
    return result


def sdk_calls(rows):
    calls = []
    for row in rows:
        response = row["response"]
        if "body" in response and response["body"].get("choices"):
            response = response["body"]["choices"][0]["message"]
        for call in response.get("tool_calls") or []:
            calls.append({"step": row.get("step"), "task_id": row.get("task_id"), **call})
    return calls


def handoff(runtime, rows):
    tasks = {task["task_id"]: task for batch in runtime["batches"] for task in batch["submitted"].get("tasks", [])}
    accepted = {row["artifact_id"]: row for row in runtime["receipts"]}
    verified = {key: ids for batch in runtime["batches"] for key, ids in
                batch.get("returned", {}).get("verified_inputs", {}).items()}
    first = {}
    for row in rows:
        if row.get("step") == "team_worker":
            value = json.loads(row["body"]["messages"][1]["content"])
            first.setdefault(value["task"]["task_id"], value)
    issues = []
    details = {}
    fields = ("source", "arguments", "result", "tool_call_id", "status")
    for key, context in first.items():
        actual = context["task"]
        task = tasks.get(key, {})
        inputs = [item for items in context["inputs"].values() for item in items] + context.get("direct_inputs", [])
        ids = [item["artifact_id"] for item in inputs]
        evidence_ids = [item["artifact_id"] for item in inputs if item["source"] != "worker_model"]
        details[key] = {"task": actual, "first_input": context, "input_ids": ids,
                        "evidence_ids": evidence_ids, "verified_ids": verified.get(key)}
        if not task or any(actual.get(field) != task.get(field, []) for field in ("dependencies", "input_refs")):
            issues.append(f"task_dependency_mismatch:{key}")
        if actual.get("objective") != task.get("objective") or any(
                actual.get(field) != task.get(field, []) for field in ("capabilities", "read_scope")):
            issues.append(f"task_scope_mismatch:{key}")
        if not all((receipt := accepted.get(item["artifact_id"])) and
                   all(receipt.get(field, "succeeded") == item[field] for field in fields if field in item)
                   for item in inputs):
            issues.append(f"worker_input_receipt_mismatch:{key}")
        if key in verified and sorted(evidence_ids) != verified[key]:
            issues.append(f"worker_verified_input_mismatch:{key}")
        for ref in actual.get("input_refs", []):
            if not context["inputs"].get(ref):
                issues.append(f"missing_upstream_input:{key}:{ref}")
    return details, issues


def product_write_matches(before, after, browser):
    products = "listing_test_products"
    receipts = "listing_change_receipts"
    if browser is None:
        return all(before[key] == after[key] for key in before if key != "listing_change_proposals")
    if any(before[key] != after[key] for key in before if key not in (products, receipts, "listing_change_proposals")):
        return False
    old, new = before[products], after[products]
    changed = [(a, b) for a, b in zip(old, new) if a != b]
    if len(old) != len(new) or len(changed) != 1 or len(before[receipts]) != 0 or len(after[receipts]) != 1:
        return False
    a, b = changed[0]
    receipt = browser.get("receipt") or {}
    return (all(a[key] == b[key] == value for key, value in TARGET.items())
            and {key for key in a if a[key] != b[key]} == {"bullets", "version"}
            and a["version"] + 1 == b["version"] == receipt.get("committed_version")
            and json.loads(b["bullets"]) == receipt.get("committed_bullets")
            and after[receipts][0]["receipt_id"] == receipt.get("receipt_id"))


def evaluate(case, response, before, after, oracle, trace, rows, runtime, observations, owner, today, live, arm):
    evidence = response.get("evidence") or {}
    calls = sdk_calls(rows)
    steps = trace.get("steps", [])
    workers = [step for step in steps if step["stage"] == "worker" and step["action"] == "created"]
    reclaimed = [step for step in steps if step["stage"] == "worker" and step["action"] == "reclaimed"]
    attempted = [call for call in calls if call["function"]["name"] == "delegate_tasks"]
    receipts = {row["artifact_id"]: row for row in runtime["receipts"]}
    reads = {step["runtime_call_id"]: step["action"] for step in steps if step["stage"] == "business_read"}
    reads.update({row["tool_call_id"]: row["source"] for row in receipts.values()
                  if row.get("task_id") and row["source"] in t1.READ_KEYS and not row.get("retry_of")})
    # Reuse the established T2 business rubric, including per-tool evidence
    # matching. Worker receipts also count as reads in the independent rubric.
    rubric_case = {**case, "expected": "draft" if case["expected"] == "required" else case["expected"]}
    if case.get("browser_approval"):
        rubric_case["id"] = "browser-complete"
    rubric_trace = {**trace, "steps": [step for step in steps if step["stage"] != "business_read"] +
                    [{"stage": "business_read", "action": name} for name in reads.values()]}
    rubric_rows = rows if live else [{**row, "response": {"status": 200}} for row in rows]
    score = t2.score(rubric_case, response, t2_snapshot(before), t2_snapshot(after), rubric_trace, rubric_rows, today)
    issues = score["issues"]
    first_stage = {}

    def check(stage, label, condition):
        if not condition:
            issues.append(label)
            first_stage[label] = stage

    check("routing", "natural_request_payload", set(response.get("request") or {}) == {"conversation_id", "message"})
    check("routing", "missing_trace", bool(trace))
    check("routing", "uncommitted_group_history", response.get("history_status") == 200 and bool(response.get("history")))
    history = (response.get("history") or {}).get("entries", [])
    assistant = next((entry for entry in history if entry["kind"] == "assistant"), {})
    action_count = len(assistant.get("listing_actions") or [])
    expected_proposal = case["expected"] in {"draft", "capacity", "required"}
    check("draft", "committed_action_count", action_count == int(expected_proposal))
    if case["expected"] != "rejected":
        check("routing", "missing_listing_evidence", evidence.get("kind") == "listing_operations_v1")
        check("read", "unexpected_target", evidence.get("target") == TARGET)
    if t2.SALES in case["expected_reads"]:
        check("read", "independent_sql_mismatch", evidence.get("metrics") == t1.expected_metrics(oracle, TARGET))
        dates = sales_windows(case, today)
        sales_args = [item["arguments"] for item in observations if item["read"] == "read_sales"]
        check("read", "actual_reader_dates_or_scope", bool(sales_args) and all(
            tuple(args) == (owner, *TARGET.values(), *dates) for args in sales_args))
    if case["expected"] == "sales" and case["expect_value"] is None:
        check("read", "user_reply_missing_totals", all(value in assistant.get("content", "") for value in ("70 件", "49 件")))
    if case["expected"] == "required":
        check("draft", "requested_material_missing", any(
            case["expect_value"] in item["text"].lower() for item in (evidence.get("proposal") or {}).get("bullets", [])))
    if case["expected"] == "unsupported":
        term = case["expect_value"] or "防水"
        if term != "防水":
            # T2's frozen unsupported case assumes waterproofing.
            issues[:] = [issue for issue in issues if issue != "unsupported_requested_fact_not_reported"]
        check("draft", "requested_claim_gap", evidence.get("unverified_requirements") == [term]
              and "unverified_listing_requirement" in evidence.get("gaps", [])
              and term in assistant.get("content", ""))
    if case["expected"] == "gap":
        check("draft", "gap_reached_draft_or_business_write",
              not any(row.get("step") == "listing_draft" for row in rows)
              and before["listing_change_proposals"] == after["listing_change_proposals"])
        if case["variant"] == "sales-gap":
            check("handoff", "missing_sales_reached_dependent_action",
                  not runtime["batches"] and not workers and set(reads.values()) <= {t2.SALES})

    for name, ref in evidence.get("artifacts", {}).items():
        receipt = receipts.get(ref["artifact_id"], {})
        check("handoff", f"selected_receipt_mismatch:{name}", receipt.get("tool_call_id") == ref["runtime_call_id"]
              and receipt.get("arguments") == {"sku": t2.SKU} and receipt.get("result") == evidence.get(t1.READ_KEYS[name]))
    details, handoff_issues = handoff(runtime, rows)
    for issue in handoff_issues:
        check("handoff", issue, False)
    check("handoff", "worker_identity_not_reclaimed", Counter(step["runtime_call_id"] for step in workers)
          == Counter(step["runtime_call_id"] for step in reclaimed))
    if arm == "direct":
        check("routing", "unexpected_direct_arm_delegation", not runtime["batches"] and not workers
              and all(tool["function"]["name"] != "delegate_tasks" for row in rows for tool in row["body"].get("tools", [])))
    drafts = [row for row in rows if row.get("step") == "listing_draft"]
    check("draft", "draft_sdk_count", len(drafts) == int(expected_proposal))
    for row in drafts:
        draft = json.loads(row["body"]["messages"][-1]["content"])
        check("handoff", "draft_evidence_join", all(draft.get(k) == evidence.get(k)
              for k in ("metrics", "listing", "facts", "artifacts")))
        reply = row["response"]
        if "body" in reply:
            reply = (reply["body"].get("choices") or [{}])[0].get("message", {})
        try:
            model_bullets = json.loads(reply.get("content") or "{}").get("bullets")
        except (ValueError, AttributeError):
            model_bullets = None
        selected = [{key: item[key] for key in ("fact_id", "text")}
                    for item in (evidence.get("proposal") or {}).get("bullets", [])]
        check("draft", "sdk_draft_output_mismatch", model_bullets == selected)
    approval = case["id"] == "browser-complete" or case.get("browser_approval", False)
    check("execution", "sql_product_or_receipt_delta", product_write_matches(
        before, after, (response.get("readback") or {}) if approval else None))
    if approval:
        check("execution", "edge_refresh_or_approval_readback", response.get("browser_exit") == 0
              and response.get("approval") == response.get("readback") == response.get("refresh")
              and response.get("sequence") == ["POST", "GET", "GET"])
    duplicate_signals = sum(step["stage"] in {"duplicate_read", "tool_reuse", "duplicate_dispatch"} for step in steps)
    tool_attempts = Counter((call["function"]["name"], call["function"]["arguments"])
                            for call in calls if call["function"]["name"] in t1.READ_KEYS)
    score.update(passed=not issues, issues=issues,
                 first_deviation=({"stage": first_stage.get(issues[0], "business_rubric"), "check": issues[0]}
                                  if issues else None),
                 path="worker" if workers else "direct" if reads else "rejected_or_no_read",
                 missing_evidence=sorted(set(case["expected_reads"]) - evidence.get("artifacts", {}).keys()),
                 actual_tool_calls=calls, read_counts=dict(Counter(reads.values())),
                 reader_invocations=len(observations), reader_phases=dict(Counter(row["phase"] for row in observations)),
                 duplicate_tool_attempts=sum(count - 1 for count in tool_attempts.values()),
                 duplicate_runtime_signals=duplicate_signals, delegation_calls=len(attempted),
                 delegation_batches=len(runtime["batches"]), handoff=details, committed_actions=action_count)
    return score


def t2_snapshot(snapshot):
    return {key: snapshot["listing_" + ("test_" if key == "products" else "change_") + key]
            for key in ("products", "proposals", "receipts")}


def preflight(client, provider, audit, output):
    import socket

    health = client.get("/api/health")
    health.raise_for_status()
    # DNS only. A request to the provider's endpoint would consume a live attempt.
    try:
        addresses = socket.getaddrinfo(httpx.URL(ENDPOINT).host, 443, type=socket.SOCK_STREAM)
    except OSError:
        addresses = []
    edge = subprocess.run(["node", "-e", "const { chromium } = require('playwright'); "
                           "chromium.launch({channel:'msedge'}).then(b => b.close()).catch(() => process.exit(1));"],
                          env=browser_env(), timeout=30, capture_output=True)
    result = {"model": provider._model, "endpoint": ENDPOINT, "loopback": health.status_code,
              "dns_resolved": bool(addresses), "edge_exit": edge.returncode, "sdk_attempts": len(audit.rows()),
              "max_sdk_attempts": audit.run_cap, "round_sdk_attempts": audit.case_cap("preflight")}
    result["passed"] = bool(addresses) and edge.returncode == 0 and not audit.rows() and not audit.stop
    write(output / "preflight.json", result)
    return result["passed"]


def execute(stage, output, frozen, root, prior_ledgers, *, schedule=None, reader_probe=measure_reads,
            fixed_provider=None):
    live = stage == "live"
    configure(output / "bootstrap.sqlite3", "glm" if live else "mock")
    if live:
        os.environ["GLM_MODEL"] = MODEL
        if not os.environ.get("GLM_API_KEY"):
            os.environ["GLM_API_KEY"] = dotenv_values(".env").get("GLM_API_KEY") or ""
    from backend import observability
    from backend.agent import listing_run, llm_provider, team_tools
    from backend.captcha import captcha
    from backend.database import database, listing_operations as listings
    from backend.memory import conversation_memory
    from tests.home_diagnosis_server import demo_captcha
    from tests.test_listing_workgroup import ListingModel

    password = token_urlsafe(24)
    redactor = Redactor([password, os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY")])
    audit = t2.LiveAudit(output / ("glm-sdk-ledger.sqlite3" if live else "fixed-empty-ledger.sqlite3"),
                         redactor, uuid4().hex, 48)
    prior_attempts = 0
    for path in {item.resolve(strict=True) for item in prior_ledgers}:
        with t1.connection(path) as conn:
            prior_attempts += conn.execute("SELECT count(*) FROM sdk").fetchone()[0]
    audit.run_cap = 48 - prior_attempts
    if audit.run_cap <= 0:
        raise RuntimeError("GLM SDK attempt limit already reached")
    provider = llm_provider.ProviderFactory.create() if live else (fixed_provider or ListingModel())
    if live:
        if provider._model != MODEL or str(provider._client.base_url).rstrip("/") != ENDPOINT:
            raise RuntimeError("Expected official GLM endpoint and configured comparison model")
        hooks = provider._client._client.event_hooks
        hooks["request"].append(audit.request)
        hooks["response"].append(audit.response)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    results = {"model": MODEL if live else "fixed", "sdk_cap": 48, "prior_attempts": prior_attempts,
               "round_sdk_cap": 8, "frozen_sha256": sha256((root / "frozen.json").read_bytes()).hexdigest(),
               "shanghai_date": today.isoformat(), "groups": {}}
    if schedule is None:
        schedule = [("fixed", case, "production") for case in frozen["fixed"]]
        if live:
            schedule = [("spot", case, "production") for case in frozen["spot"]]
            for idx, case in enumerate(frozen["paired"]):
                schedule.extend(("paired-" + arm, case, arm) for arm in (ARMS if idx % 2 == 0 else ARMS[::-1]))
    with ExitStack() as stack:
        if live:
            stack.callback(provider._client.close)
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=provider))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        stack.enter_context(patch.object(conversation_memory, "store_conversation", return_value=None))
        stack.enter_context(patch.object(team_tools, "trace_tool_call", audit.trace_tool(team_tools.trace_tool_call)))
        from fast_server import app

        calls = []
        with serve(start_request_capture(app, calls)) as origin, httpx.Client(
                base_url=origin, timeout=100, trust_env=False) as client:
            if live and not preflight(client, provider, audit, output):
                return 1
            calls.clear()
            for group, case, arm in schedule:
                folder = output / group / case["id"]
                folder.mkdir(parents=True)
                path = folder / "sandbox.sqlite3"
                captures = {"reads": [], "receipts": [], "batches": [], "sdk": []}
                readers = []
                audit.case = f"{group}/{case['id']}"
                if fixed_provider is not None:
                    fixed_provider.start_case(case)
                calls.clear()
                with ExitStack() as turn:
                    turn.enter_context(patch.object(database.settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(path)))
                    for attr, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                                           ("FUNC_LOG_FILE", "functions.jsonl")):
                        turn.enter_context(patch.object(observability, attr, folder / filename))
                    owner = seeded(database, listings, password, case, today)
                    before = t1.snapshot(path)
                    oracle = sql_oracle(path, case, today)
                    t1.capture_runtime(turn, captures, listings, provider, audit if live else None)
                    for name in ("read_sales", "read_listing", "read_facts"):
                        turn.enter_context(patch.object(listings, name, reader_probe(getattr(listings, name), name, audit, readers)))
                    if arm == "direct":
                        turn.enter_context(patch.object(listing_run.DelegationRun, "tasks_left", return_value=0))
                    audit.enabled = live
                    started = monotonic()
                    try:
                        response = round_response(client, folder, origin, case, password, path,
                                                  case["id"] == "browser-complete" or case.get("browser_approval", False))
                    except (httpx.HTTPError, subprocess.TimeoutExpired) as exc:
                        response = {"runner_error": type(exc).__name__}
                    finally:
                        audit.enabled = False
                    elapsed = round(monotonic() - started, 3)
                    after = t1.snapshot(path)
                traces = t1.read_traces(folder)
                trace_id = next((event.get("trace_id") for event in reversed(response.get("events", []))
                                 if event.get("trace_id")), None)
                trace = next((row for row in traces if row["trace_id"] == trace_id), {})
                rows = [row for row in audit.rows() if row["case"] == audit.case] if live else captures.pop("sdk")
                summary = evaluate(case, response, before, after, oracle, trace, rows, captures, readers, owner, today, live, arm)
                summary.update(seconds=elapsed, trace_id=trace_id, trace_seconds=trace.get("total_duration"),
                               sse_seconds=next((row.get("seconds") for row in calls if row["phase"] == "workgroup"), None),
                               sdk_retries=sum(row.get("sdk_retry", 0) > 0 for row in rows))
                results["groups"].setdefault(group, {})[case["id"]] = {
                    key: value for key, value in summary.items() if key not in ("actual_tool_calls", "handoff")}
                write(folder / "audit.json", redactor.clean({"case": case, "summary": summary, "response": response,
                      "trace": trace, "http": calls, "reader_invocations": readers, "runtime": captures,
                      "sql_oracle": oracle, "sql_before": before, "sql_after": after, "sdk": rows}))
                if not summary["passed"]:
                    write(folder / "repro.json", {"message": case["message"], "variant": case["variant"], "arm": arm,
                          "request": response.get("request"), "first_deviation": summary["first_deviation"],
                          "actual_tool_calls": summary["actual_tool_calls"], "trace_id": trace_id})
                print(json.dumps({group + "/" + case["id"]: results["groups"][group][case["id"]]},
                                 ensure_ascii=False), flush=True)
                if live:
                    write(output / "sdk.json", audit.rows())
                write(output / "report.json", results)
                if audit.stop:
                    results["provider_stop"] = audit.stop
                    break
    results.update(passed=sum(row["passed"] for group in results["groups"].values() for row in group.values()),
                   evaluated=sum(map(len, results["groups"].values())), expected=len(schedule), sdk_attempts=len(audit.rows()))
    write(output / "report.json", results)
    return int(results["passed"] != results["expected"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["freeze", "fixed", "live"])
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--run-name")
    parser.add_argument("--prior-ledger", action="append", type=Path, default=[])
    args = parser.parse_args()
    root = args.output_dir.resolve()
    if args.stage == "freeze":
        root.mkdir(parents=True, exist_ok=True)
        with (root / "frozen.json").open("x", encoding="utf-8") as stream:
            json.dump(freeze(), stream, ensure_ascii=False, indent=2)
        return 0
    frozen = read(root / "frozen.json")
    if frozen != freeze():
        parser.error("Frozen questions or scoring rules changed")
    if args.stage == "live":
        fixed = read(root / "fixed-matrix" / "report.json")
        if fixed["passed"] != fixed["expected"] or not read(root / "fixed" / "summary.json")["passed"]:
            parser.error("Fixed matrix and approval regressions must pass before live execution")
        args.prior_ledger.extend(root.rglob("glm-sdk-ledger.sqlite3"))
    output = root / (args.run_name or ("fixed-matrix" if args.stage == "fixed" else "live"))
    output.mkdir(exist_ok=False)
    return execute(args.stage, output, frozen, root, args.prior_ledger)


if __name__ == "__main__":
    raise SystemExit(main())
