"""Run the frozen operations Agent business matrix against live GLM and isolated SQLite."""

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import ExitStack, closing
from datetime import date, datetime, timedelta
from functools import wraps
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from time import monotonic
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values

from scripts.accept_task42_t1 import configure, read, serve, write
from scripts.run_real_diagnosis import Redactor
from scripts.run_task27_t5 import ENDPOINT, GLMAudit, MODEL, browser_env


GROUPS = "/api/chat/conversations"
SKU = "TASK42-BOTTLE"
OWNER = "task42-t2-operator"
FIRST = "2026-10-30"
LAST = "2026-11-12"
RANGE = f"{FIRST} 到 {LAST}"
COMPLETE = f"查 SKU {SKU} 在 {RANGE} 的销量，结合商品资料重写英文五点，先给我看，确认后再应用。"
PRIORITY = f"结合 SKU {SKU} 在 {RANGE} 的销量和商品资料重写英文五点，容量放第一条，确认后再应用。"
SALES = "get_listing_sales"
LISTING = "get_listing_current"
FACTS = "get_listing_facts"
CAP = 48


class LiveAudit(GLMAudit):
    run_cap = CAP

    def case_cap(self, case):
        return 8


def freeze():
    cases = [
        ("browser-complete", "original", COMPLETE, [SALES, LISTING, FACTS], "draft", True),
        ("sales-direct", "original", f"查 SKU {SKU} 在 {RANGE} 的销量，只告诉我两窗销量。", [SALES], "sales", None),
        ("read-original-bullets", "original", f"请查看 SKU {SKU} 当前原有的英文五点。", [LISTING], "listing", None),
        ("explicit-no-draft", "original", f"只查询 SKU {SKU} 在 {RANGE} 的销量，不要改英文五点，也不要生成草案。", [SALES], "sales", None),
        ("capacity-original", "original", PRIORITY, [SALES, LISTING, FACTS], "capacity", "500 mL"),
        ("capacity-changed", "changed", PRIORITY, [SALES, LISTING, FACTS], "capacity", "750 mL"),
        ("insufficient-facts", "missing", COMPLETE, [SALES, LISTING, FACTS], "gap", "insufficient_listing_facts"),
        ("unsupported-waterproof", "original", f"请结合 SKU {SKU} 在 {RANGE} 的销量重写英文五点，必须写防水；资料不支持就告诉我，不要出草案。", [SALES, LISTING, FACTS], "unsupported", None),
        ("other-shop-same-sku", "original", f"只查店铺 task42-t2-other 的 SKU {SKU} 在 {RANGE} 的销量。", [], "rejected", None),
        ("relative-thirty-days", "long", f"查看 SKU {SKU} 最近30天的销量。", [SALES], "sales", "30"),
        ("sales-gap-conditional", "sales-gap", f"查看 SKU {SKU} 在 {RANGE} 的销量，只有两窗数据完整才重写英文五点，缺数据就别出草案。", [SALES], "gap", "metrics_incomplete"),
        ("unknown-sku", "original", "请查询 SKU TASK42-T2-UNKNOWN 的销量。", [], "rejected", None),
    ]
    return [{"id": case, "variant": variant, "message": message, "message_sha256": sha256(message.encode()).hexdigest(),
             "expected_reads": tools, "expected": expected, "expect_value": value}
            for case, variant, message, tools, expected, value in cases]


def snapshot(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return {
            "products": [dict(row) for row in conn.execute("SELECT * FROM listing_test_products ORDER BY shop_id, sku")],
            "proposals": [dict(row) for row in conn.execute("SELECT * FROM listing_change_proposals ORDER BY action_id")],
            "receipts": [dict(row) for row in conn.execute("SELECT * FROM listing_change_receipts ORDER BY receipt_id")],
        }


def capture_http(client, headers, case):
    group = client.post(GROUPS, headers=headers, json={"title": case["id"]})
    group.raise_for_status()
    conversation = group.json()["data"]["conversation_id"]
    request = {"conversation_id": conversation, "message": case["message"]}
    if not case.get("natural"):
        request["listing_operations"] = True
    response = client.post("/api/chat/stream", headers=headers, json=request)
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    terminal = next((event for event in events if event["type"] in {"done", "error", "stopped", "superseded"}), {})
    result = {"http_status": response.status_code, "request": request, "events": events}
    if run := terminal.get("run_id"):
        endpoint = f"{GROUPS}/{conversation}/runs/{run}/evidence"
        evidence = client.get(endpoint, headers=headers)
        result.update(evidence_http_status=evidence.status_code, evidence=evidence.json().get("data"))
    history = client.get(f"{GROUPS}/{conversation}/history", headers=headers)
    result["history"] = history.json().get("data")
    return result


def score(case, result, before, after, trace, rows, shanghai_date):
    issues = []
    if "runner_error" in result:
        return {"issues": [result["runner_error"]], "attempts": len(rows), "passed": False}

    def check(predicate, message):
        if not predicate:
            issues.append(message)

    events = result.get("events", [])
    check((result.get("request") or {}).get("message") == case["message"], "request_text_drift")
    check([e["type"] for e in events if e["type"] in {"done", "error", "stopped", "superseded"}] == ["done"],
          "sse_terminal_not_single_done")
    evidence = result.get("evidence") or {}
    steps = trace.get("steps", [])
    reads = [step["action"] for step in steps if step["stage"] == "business_read"]
    workers = [step for step in steps if step["stage"] == "worker" and step["action"] == "created"]
    reclaimed = [step for step in steps if step["stage"] == "worker" and step["action"] == "reclaimed"]
    check(len(workers) == len(reclaimed), "worker_not_reclaimed")
    check(all(row["response"].get("status") == 200 for row in rows), "provider_transport_failure")
    check(Counter(reads) == Counter(case["expected_reads"]), "wrong_tool_reads")
    for name, artifact in evidence.get("artifacts", {}).items():
        check(any(step["stage"] == "tool_result" and step["action"] == name
                  and step.get("runtime_call_id") == artifact["runtime_call_id"] and step["status"] == "ok"
                  for step in steps), f"trace_artifact_mismatch:{name}")
    for row in rows:
        if row.get("step") != "listing_draft":
            continue
        content = json.loads(row["body"]["messages"][-1]["content"])
        check(all(content.get(key) == evidence.get(key) for key in ("metrics", "listing", "facts", "artifacts")),
              "draft_input_evidence_mismatch")
    operation = case["expected"]
    proposal = evidence.get("proposal")
    value = case["expect_value"]
    draft_requests = [row for row in rows if row.get("step") == "listing_draft"]
    texts = [event.get("content", "") for event in events if event["type"] == "text"]
    if (history := result.get("history")) is not None:
        assistants = [entry["content"] for entry in history["entries"] if entry["kind"] == "assistant"]
        check(assistants == texts, "group_history_sse_mismatch")
    if operation in {"sales", "draft", "capacity"}:
        metrics = evidence.get("metrics") or {}
        if value == "30":
            end = shanghai_date - timedelta(days=1)
            check([(metrics.get(name) or {}).get(field) for name in ("baseline", "current")
                   for field in ("start_date", "end_date")] ==
                  [day.isoformat() for day in (end - timedelta(days=29), end - timedelta(days=15),
                                               end - timedelta(days=14), end)], "wrong_relative_window")
            check([(metrics.get(name) or {}).get("units") for name in ("baseline", "current")] == [345, 120],
                  "wrong_relative_window_totals")
        else:
            check([(metrics.get(key) or {}).get("units") for key in ("baseline", "current")] == [70, 49]
                  and metrics.get("units_change_pct") == -30 and metrics.get("conversion_rate") is None,
                  "metrics_oracle_mismatch")
    if operation in {"draft", "capacity"}:
        check(bool(proposal and len(proposal["bullets"]) == 5), "no_valid_draft")
        if proposal:
            options = {row["fact_id"]: row["options"] for row in evidence["facts"]["facts"]}
            check(len({bullet["fact_id"] for bullet in proposal["bullets"]}) == 5 and all(
                bullet["text"] in options.get(bullet["fact_id"], []) for bullet in proposal["bullets"]),
                  "unverified_draft_claim")
            if operation == "capacity":
                check(proposal["bullets"][0]["fact_id"] == "capacity" and value in proposal["bullets"][0]["text"],
                      "required_fact_and_order_missing")
    else:
        check(proposal is None and before["proposals"] == after["proposals"], "unexpected_approvable_draft")
    if operation == "listing":
        check(bool(evidence.get("listing")) and not evidence.get("facts") and not evidence.get("metrics"),
              "read_scope_should_only_show_current_listing")
    if operation in {"sales", "listing"}:
        check(len(rows) <= 1 and not workers and not draft_requests, "read_only_routed_through_draft_workflow")
    if operation == "gap":
        if case["variant"] == "sales-gap":
            metrics = evidence.get("metrics") or {}
            check((metrics.get("current") or {}).get("units", -1) is None and any("不完整" in msg for msg in texts),
                  "missing_sales_day_not_reported")
            check("incomplete_listing_sales" in evidence.get("gaps", []), "missing_sales_gap_code")
            check(not draft_requests, "explicit_sales_condition_reached_draft_model")
        else:
            check(value in evidence.get("gaps", []), "missing_facts_not_reported")
    if operation == "unsupported":
        check(any("防水" in msg for msg in texts) and "unverified_listing_requirement" in evidence.get("gaps", []),
              "unsupported_requested_fact_not_reported")
        check(not draft_requests, "unsupported_requirement_reached_draft_model")
    if operation == "rejected":
        check(not rows and not evidence.get("artifacts"), "rejected_scope_reached_model_or_reads")
        check(any("SKU" in msg and ("有权" in msg or "店铺" in msg) for msg in texts),
              "rejected_scope_not_explained")
    if case["id"] == "browser-complete":
        check(result.get("passed") and len(after["receipts"]) == len(before["receipts"]) + 1,
              "browser_did_not_complete_approval_readback")
    else:
        check(before["products"] == after["products"] and before["receipts"] == after["receipts"],
              "unexpected_committed_listing_change")
    return {"issues": issues, "passed": not issues, "reads": reads, "workers": len(workers),
            "reclaimed": len(reclaimed), "attempts": len(rows), "trace_id": trace.get("trace_id"),
            "draft_requests": len(draft_requests),
            "tokens": sum((row["response"].get("body", {}).get("usage") or {}).get("total_tokens", 0) for row in rows)}


def seed(database, listings, password, variant, shanghai_date):
    database.init_db()
    database.seed_data()
    with closing(database.get_db()) as conn, conn:
        user_id = conn.execute("INSERT INTO users (username,password) VALUES (?,?)",
                               (OWNER, database.hash_password(password))).lastrowid
        other = conn.execute("SELECT id FROM users WHERE username='lisi'").fetchone()[0]
        listings.seed_test_product(conn, user_id, variant=variant if variant in {"missing", "changed"} else "original")
        listings.seed_test_product(conn, other, shop_id="task42-t2-other")
        conn.execute("UPDATE listing_test_sales SET series=? WHERE shop_id=?", (
            json.dumps({(date.fromisoformat(FIRST) + timedelta(days=n)).isoformat(): 999 for n in range(14)}),
            "task42-t2-other"))
        if variant not in {"long", "sales-gap"}:
            return
        row = conn.execute("SELECT series FROM listing_test_sales WHERE shop_id=?", (listings.TEST_SHOP,)).fetchone()
        points = json.loads(row["series"])
        if variant == "long":
            end = shanghai_date - timedelta(days=1)
            points.update({(end - timedelta(days=n)).isoformat(): n + 1 for n in range(30)})
        else:
            del points[LAST]
        conn.execute("UPDATE listing_test_sales SET series=?,version=version+1 WHERE shop_id=?",
                     (json.dumps(points), listings.TEST_SHOP))


def observe_reader(original, name, audit, invocations):
    @wraps(original)
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        if audit.enabled:
            invocations.append({"case": audit.case, "read": name, "during_tool_call": bool(audit.tool_link.get()),
                                "args": list(args[1:]), "found": result is not None})
        return result

    return observed


def run(output, selected, baseline, prior_ledgers):
    credentials = dotenv_values(".env")
    configure(output / "sandbox.sqlite3", "glm")
    if credentials.get("GLM_API_KEY"):
        os.environ.setdefault("GLM_API_KEY", credentials["GLM_API_KEY"])
    os.environ.setdefault("GLM_MODEL", credentials.get("GLM_MODEL") or MODEL)
    password = token_urlsafe(24)
    redactor = Redactor([os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY"), password])
    from backend import observability
    from backend.agent import llm_provider, team_runtime, team_tools
    from backend.captcha import captcha
    from backend.database import database, listing_operations as listings
    from backend.memory import conversation_memory
    from tests.home_diagnosis_server import demo_captcha

    provider = llm_provider.ProviderFactory.create()
    if provider._model != MODEL or str(provider._client.base_url).rstrip("/") != ENDPOINT:
        raise RuntimeError("Expected configured official GLM endpoint and glm-4.5-air")
    prior_attempts = 0
    for path in {item.resolve(strict=True) for item in prior_ledgers}:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            prior_attempts += conn.execute("SELECT COUNT(*) FROM sdk").fetchone()[0]
    if prior_attempts >= CAP:
        raise RuntimeError("GLM audit attempt limit already reached")
    audit = LiveAudit(output / "glm-sdk-ledger.sqlite3", redactor, uuid4().hex, CAP)
    audit.run_cap = CAP - prior_attempts
    hooks = provider._client._client.event_hooks
    hooks["request"].append(audit.request)
    hooks["response"].append(audit.response)
    shanghai_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    summary = {"model": MODEL, "sdk_cap": CAP, "prior_attempts": prior_attempts,
               "shanghai_date": shanghai_date.isoformat(), "cases": {},
               "listing_source_sha256": sha256(Path("backend/agent/listing_run.py").read_bytes()).hexdigest()}
    invocations = []
    with ExitStack() as stack:
        stack.callback(provider._client.close)
        if baseline:
            name = "backend.agent.listing_run"
            loader = importlib.machinery.SourceFileLoader(name, str(baseline))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            stack.enter_context(patch.dict(sys.modules, {name: module}))
            exec(compile(baseline.read_bytes(), str(baseline), "exec"), module.__dict__)
            summary["listing_source_sha256"] = sha256(baseline.read_bytes()).hexdigest()
        for module in (llm_provider, team_runtime):
            stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        stack.enter_context(patch.object(team_tools, "trace_tool_call", audit.trace_tool(team_tools.trace_tool_call)))
        for name in ("read_sales", "read_listing", "read_facts"):
            stack.enter_context(patch.object(listings, name, observe_reader(getattr(listings, name), name, audit, invocations)))
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=provider))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        stack.enter_context(patch.object(conversation_memory, "store_conversation", return_value=None))
        for name, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                               ("FUNC_LOG_FILE", "functions.jsonl")):
            stack.enter_context(patch.object(observability, name, output / filename))
        from fast_server import app

        with serve(app) as origin, httpx.Client(base_url=origin, timeout=90, trust_env=False) as client:
            for case in selected:
                folder = output / case["id"]
                folder.mkdir()
                database_path = folder / "sandbox.sqlite3"
                database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(database_path)
                seed(database, listings, password, case["variant"], shanghai_date)
                before = snapshot(database_path)
                audit.case = case["id"]
                audit.enabled = True
                started = monotonic()
                try:
                    if case["id"] == "browser-complete":
                        env = {**browser_env(), "AD_TASK42_USERNAME": OWNER, "AD_TASK42_PASSWORD": password,
                               "AD_TASK42_SQLITE": str(database_path), "AD_TASK42_MESSAGE": case["message"]}
                        result = subprocess.run(["node", "tests/task42_listing_browser.cjs", origin, str(folder)],
                                                env=env, timeout=120, capture_output=True)
                        write(folder / "browser-process.json", redactor.clean({"exit_code": result.returncode,
                              "stderr": result.stderr.decode(errors="replace")}))
                        response = read(folder / "listing-browser.json") if (folder / "listing-browser.json").exists() else {}
                    else:
                        login = client.post("/api/login", json={"username": OWNER, "password": password})
                        login.raise_for_status()
                        headers = {"Authorization": "Bearer " + login.json()["data"]["token"]}
                        response = capture_http(client, headers, case)
                except (httpx.HTTPError, subprocess.TimeoutExpired) as error:
                    response = {"runner_error": type(error).__name__}
                finally:
                    audit.enabled = False
                after = snapshot(database_path)
                trace_id = next((row.get("trace_id") for row in reversed(response.get("events", [])) if row.get("trace_id")), None)
                trace = next((entry for line in (output / "traces.jsonl").read_text(encoding="utf-8").splitlines()
                              if (entry := json.loads(line))["trace_id"] == trace_id), {}) if trace_id else {}
                rows = [row for row in audit.rows() if row["case"] == case["id"]]
                result = score(case, response, before, after, trace, rows, shanghai_date)
                readers = [row for row in invocations if row["case"] == case["id"]]
                result["reader_invocations"] = len(readers)
                result["seconds"] = round(monotonic() - started, 3)
                summary["cases"][case["id"]] = result
                print(json.dumps({case["id"]: result}, ensure_ascii=False), flush=True)
                write(folder / "audit.json", redactor.clean({"result": result, "response": response, "trace": trace,
                      "sql_before": before, "sql_after": after, "sdk_attempt_ids": [row["attempt"] for row in rows],
                      "reader_invocations": readers}))
                write(output / "sdk.json", audit.rows())
                write(output / "report.json", summary)
                if audit.stop:
                    summary["provider_stop"] = audit.stop
                    break
    summary.update(passed=sum(case["passed"] for case in summary["cases"].values()),
                   evaluated=len(summary["cases"]), sdk_attempts=len(audit.rows()),
                   cumulative_attempts=prior_attempts + len(audit.rows()),
                   tokens=sum(case.get("tokens", 0) for case in summary["cases"].values()))
    write(output / "report.json", summary)
    return 0 if summary["passed"] == len(selected) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--prior-ledger", action="append", type=Path, default=[])
    args = parser.parse_args()
    cases = [case for case in freeze() if not args.case or case["id"] in args.case]
    if not cases or set(args.case) - {case["id"] for case in cases}:
        parser.error("Unknown case")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    write(output / "frozen.json", cases)
    return run(output, cases, args.baseline_source, args.prior_ledger)


if __name__ == "__main__":
    raise SystemExit(main())
