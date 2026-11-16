"""Submit a standalone task sheet to real DeepSeek with isolated SQLite data."""

import argparse
import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from urllib.parse import urlsplit

from backend.agent.llm_provider import DeepSeekProvider
from backend.agent.team_runtime import DelegationRun, delegate_tasks
from backend.config.config import settings
from backend.database import database
from backend.observability import get_trace
from scripts.run_real_diagnosis import CheckFailed, Redactor, SDKWireAudit, require


USER_ID, PLAN_ID = 17, 101
CAPABILITIES = ("get_daily_report_by_date", "get_plan_detail")
TASK_SHEET = {"reason": "Read the plan's current totals from the appropriate source", "tasks": [{
    "task_id": "plan-totals", "objective": "Read owned plan 101 and summarize its status, impressions and clicks.",
    "capabilities": CAPABILITIES, "dependencies": [], "input_refs": [],
    "expected_output": "Current status, impressions and clicks for plan 101, backed by a tool receipt.",
    "stop_condition": "A valid receipt for the plan is available, or the required evidence is missing.",
}]}


def seed_fixture():
    database.init_db()
    with closing(database.get_db()) as conn, conn:
        conn.execute("INSERT INTO ad_plans (id, plan_name, user_id, impressions, clicks) VALUES (?, ?, ?, ?, ?)",
                     (PLAN_ID, "Delegation acceptance fixture", USER_ID, 1500, 120))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("logs/task26-r1/real"))
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = Path(mkdtemp(prefix="run-", dir=args.output_dir.resolve())) / "report.json"
    config = settings.llm_config("deepseek")
    redactor = Redactor((config.api_key, settings.OPENAI_API_KEY, settings.JWT_SECRET))
    audit = SDKWireAudit(redactor)
    report = {"ok": False, "provider": config.provider, "requested_model": config.model,
              "task_sheet": TASK_SHEET, "sdk_requests": audit.rows, "fixture": "isolated_synthetic_sqlite"}
    original_path, original_sandbox = database.DB_PATH, settings.DIAGNOSIS_SANDBOX_DB_PATH
    try:
        endpoint = urlsplit(config.base_url or "")
        require(config.api_key and endpoint.scheme == "https" and endpoint.netloc == "api.deepseek.com",
                "official_deepseek_configuration_required")
        with TemporaryDirectory(dir=output.parent) as fixture:
            database.DB_PATH = Path(fixture) / "business.sqlite3"
            settings.DIAGNOSIS_SANDBOX_DB_PATH = ""
            seed_fixture()
            provider = DeepSeekProvider(config)
            try:
                audit.attach(provider)
                run = DelegationRun(user_id=USER_ID, provider=provider, allowed_capabilities=CAPABILITIES)
                result = delegate_tasks(TASK_SHEET, run=run)
                audit.trace_id = result["trace_id"]
                for row in audit.rows:
                    row["trace_id"] = audit.trace_id
                report.update(result=result, trace=get_trace(audit.trace_id))
                require(result["status"] == "completed", "delegation_did_not_complete")
                receipts = [row for row in result["receipts"] if row["source"] == "get_plan_detail"]
                require(len(receipts) == 1 and receipts[0]["arguments"] == {"plan_id": PLAN_ID},
                        "expected_actual_plan_tool_call")
                receipt = run.get_artifact(receipts[0]["artifact_id"])
                require(receipt["status"] == "succeeded" and
                        (receipt["result"]["id"], receipt["result"]["status"], receipt["result"]["impressions"],
                         receipt["result"]["clicks"]) == (PLAN_ID, "active", 1500, 120), "fixture_receipt_mismatch")
                require(audit.rows and all(row.get("status") == 200 for row in audit.rows), "deepseek_http_failure")
                require(any(call["function"]["name"] == "get_plan_detail"
                            and json.loads(call["function"]["arguments"]) == receipt["arguments"]
                            for row in audit.rows for call in row.get("response_calls", [])), "missing_wire_tool_call")
                report["verified_tool_receipt"] = receipt
                report["ok"] = True
            finally:
                provider._client.close()
    except Exception as error:
        report["failure"] = str(error) if isinstance(error, CheckFailed) else type(error).__name__
    finally:
        database.DB_PATH, settings.DIAGNOSIS_SANDBOX_DB_PATH = original_path, original_sandbox
        output.write_text(json.dumps(redactor.clean(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Evidence: {output}")
    return int(not report["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
