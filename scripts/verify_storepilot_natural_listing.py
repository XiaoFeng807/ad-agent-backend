"""Replay three natural Listing requests against GLM on an isolated local database."""

import argparse
import json
import os
import subprocess
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
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
from scripts.run_task27_t5 import GLMAudit, MODEL, browser_env
from scripts.verify_task42_t2_live import OWNER, capture_http, snapshot


class NaturalAudit(GLMAudit):
    run_cap = 24

    def case_cap(self, case):
        return 8


def verify(output, fixed_browser):
    output.mkdir(parents=True, exist_ok=False)
    path = output / "sandbox.sqlite3"
    configure(path, "mock" if fixed_browser else "glm")
    os.environ["LISTING_SIGNIFICANT_DECLINE_PCT"] = "20"
    if not fixed_browser:
        credentials = dotenv_values(".env")
        os.environ.setdefault("GLM_API_KEY", credentials.get("GLM_API_KEY") or "")
        os.environ.setdefault("GLM_MODEL", credentials.get("GLM_MODEL") or MODEL)
        if not os.environ["GLM_API_KEY"]:
            raise RuntimeError("GLM_API_KEY is required")

    from backend import observability
    from backend.agent import llm_provider, team_runtime
    from backend.captcha import captcha
    from backend.database import database, listing_operations
    from tests.home_diagnosis_server import demo_captcha
    from tests.test_listing_workgroup import ListingModel
    from tests.test_storepilot_natural_listing import PARAPHRASE, QUESTION

    password = token_urlsafe(24)
    redactor = Redactor([os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY"), password])
    database.init_db()
    current_end = datetime.now(ZoneInfo(listing_operations.SALES_TIMEZONE)).date() - timedelta(days=1)
    with closing(database.get_db()) as conn, conn:
        owner = conn.execute("INSERT INTO users (username,password) VALUES (?,?)",
                             (OWNER, database.hash_password(password))).lastrowid
        listing_operations.seed_test_product(conn, owner, current_end=current_end.isoformat())

    provider = ListingModel() if fixed_browser else llm_provider.ProviderFactory.create()
    audit = None
    with ExitStack() as stack:
        if not fixed_browser:
            audit = NaturalAudit(output / "glm-sdk-ledger.sqlite3", redactor, uuid4().hex, NaturalAudit.run_cap)
            hooks = provider._client._client.event_hooks
            hooks["request"].append(audit.request)
            hooks["response"].append(audit.response)
            stack.callback(provider._client.close)
            for module in (llm_provider, team_runtime):
                stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=provider))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        stack.enter_context(patch("backend.memory.conversation_memory.store_conversation"))
        stack.enter_context(patch.object(observability, "TRACE_LOG_FILE", output / "traces.jsonl"))
        stack.enter_context(patch.object(observability, "ERROR_LOG_FILE", output / "errors.jsonl"))
        from fast_server import app

        cases = [("browser-natural", QUESTION), ("paraphrase", PARAPHRASE), ("missing-day", QUESTION)]
        if fixed_browser:
            cases = cases[:1]
        results = {}
        with serve(app) as origin, httpx.Client(base_url=origin, timeout=90, trust_env=False) as client:
            for name, message in cases:
                folder = output / name
                folder.mkdir()
                if name == "missing-day":
                    missing = (current_end - timedelta(days=3)).isoformat()
                    with closing(database.get_db()) as conn, conn:
                        conn.execute("UPDATE listing_test_sales SET series=json_remove(series, ?) WHERE sku=?",
                                     (f'$."{missing}"', listing_operations.TEST_SKU))
                before = snapshot(path)
                if audit:
                    audit.case = name
                    audit.enabled = True
                started = monotonic()
                if name == "browser-natural":
                    env = {**browser_env(), "AD_TASK42_USERNAME": OWNER, "AD_TASK42_PASSWORD": password,
                           "AD_TASK42_SQLITE": str(path), "AD_TASK42_MESSAGE": message, "AD_TASK42_NATURAL": "1",
                           "AD_TASK42_PENDING_ONLY": "1"}
                    process = subprocess.run(["node", "tests/task42_listing_browser.cjs", origin, str(folder)],
                                             env=env, capture_output=True, timeout=150, check=False)
                    write(folder / "browser-process.json", redactor.clean({"exit_code": process.returncode,
                          "stderr": process.stderr.decode(errors="replace")}))
                    response = read(folder / "listing-browser.json") if (folder / "listing-browser.json").exists() else {}
                else:
                    login = client.post("/api/login", json={"username": OWNER, "password": password})
                    login.raise_for_status()
                    response = capture_http(client, {"Authorization": "Bearer " + login.json()["data"]["token"]},
                                            {"id": name, "message": message, "natural": True})
                elapsed = monotonic() - started
                rows = [row for row in audit.rows() if row["case"] == name] if audit else []
                if audit:
                    audit.enabled = False
                    write(output / "sdk.redacted.json", audit.rows())
                after = snapshot(path)
                terminal = next((row for row in reversed(response.get("events", [])) if row["type"] in ("done", "error")), {})
                trace_id = terminal.get("trace_id")
                trace_file = output / "traces.jsonl"
                traces = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()] if trace_file.exists() else []
                trace = next((trace for trace in traces if trace["trace_id"] == trace_id), {})
                steps = trace.get("steps", [])
                reads = [step for step in steps if step["stage"] == "business_read"]
                workers = [step for step in steps if step["stage"] == "worker" and step["action"] == "created"]
                evidence = response.get("evidence") or {}
                proposal = evidence.get("proposal")
                metrics = evidence.get("metrics") or {}
                expected = {"get_listing_sales"} if name == "missing-day" else {
                    "get_listing_sales", "get_listing_current", "get_listing_facts"}
                checks = {
                    "original_message": response.get("request", {}).get("message") == message,
                    "natural_entry": "listing_operations" not in response.get("request", {}),
                    "completed": terminal.get("type") == "done",
                    "actual_tools": {step["action"] for step in reads} == expected,
                    "actual_scope": bool(reads) and all(
                        all(step["evidence"]["arguments"][key] == value for key, value in evidence["target"].items())
                        for step in reads),
                    "zero_product_writes": before["products"] == after["products"] and before["receipts"] == after["receipts"],
                    "sdk_requests_succeeded": fixed_browser or bool(rows) and all(row["response"].get("status") == 200 for row in rows),
                }
                sales_reads = [step["evidence"]["arguments"] for step in reads if step["action"] == "get_listing_sales"]
                checks["actual_windows"] = len(sales_reads) == 1 and [
                    sales_reads[0][key] for key in ("baseline_start", "baseline_end", "current_start", "current_end")
                ] == evidence.get("windows")
                drafts = [json.loads(row["body"]["messages"][-1]["content"])
                          for row in rows if row.get("step") == "listing_draft"]
                if name == "missing-day":
                    checks["stops_at_sales"] = (not drafts and not proposal and before["proposals"] == after["proposals"]
                        and metrics.get("current", {}).get("covered_days") == 6 and metrics.get("units_change_pct") is None
                        and any(missing in event.get("content", "") for event in response.get("events", [])))
                else:
                    facts = {row["fact_id"]: row["options"] for row in evidence.get("facts", {}).get("facts", [])}
                    bullets = (proposal or {}).get("bullets", [])
                    checks["five_supported_bullets"] = len({row["fact_id"] for row in bullets}) == len(bullets) == 5 and all(
                        row["text"] in facts[row["fact_id"]] for row in bullets)
                    checks["model_facts_match"] = fixed_browser or len(drafts) == 1 and all(
                        drafts[0][key] == evidence[key] for key in ("facts", "listing", "metrics", "artifacts"))
                    checks["one_pending_proposal"] = len(after["proposals"]) - len(before["proposals"]) == 1
                    checks["listing_version_bound"] = bool(proposal) and proposal["original_version"] == evidence["listing"]["version"]
                    checks["counts"] = (metrics.get("baseline", {}).get("units"), metrics.get("current", {}).get("units")) == (70, 49)
                if name == "browser-natural":
                    checks["browser_pending_refresh"] = bool(response.get("passed") and response.get("refresh", {}).get("state") == "pending")
                result = {"passed": all(checks.values()), "checks": checks, "trace_id": trace_id,
                          "path": "delegated" if workers else "direct" if reads else "no_read",
                          "worker_count": len(workers), "requests": len(rows) if audit else len(provider.requests),
                          "seconds": round(elapsed, 3)}
                results[name] = result
                write(folder / "audit.json", redactor.clean({"result": result, "response": response, "trace": trace,
                      "sql_before": before, "sql_after": after, "sdk_attempt_ids": [row["attempt"] for row in rows]}))
                write(output / "report.json", results)
                print(json.dumps({name: result}, ensure_ascii=False), flush=True)
                if audit and audit.stop:
                    break
    return len(results) == len(cases) and all(result["passed"] for result in results.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixed-browser", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.output_dir.resolve(), args.fixed_browser) else 1)
