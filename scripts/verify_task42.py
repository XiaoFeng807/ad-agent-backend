"""Run the local Listing workflow through real FastAPI and Chromium."""

import argparse
import json
import os
import socket
import subprocess
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from threading import Thread
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from scripts.run_real_diagnosis import Redactor


GLM_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def audit_listing(output, rows):
    browser = json.loads((output / "listing-browser.json").read_text(encoding="utf-8"))
    evidence = browser["evidence"]
    traces = [json.loads(line) for line in (output / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    source_id = browser["readback"]["receipt"]["source_trace_id"]
    execution_id = browser["readback"]["receipt"]["execution_trace_id"]
    source = next(row for row in traces if row["trace_id"] == source_id)
    execution = next(row for row in traces if row["trace_id"] == execution_id)
    assert any(step["action"] == "applied" for step in execution["steps"])
    result = {"passed": browser["passed"], "source_trace_id": source_id, "execution_trace_id": execution_id,
              "sdk_attempts": len(rows), "worker_count": sum(step["stage"] == "worker" and step["action"] == "created"
                                                             for step in source["steps"]),
              "artifact_ids": {name: row["artifact_id"] for name, row in evidence["artifacts"].items()}}
    if rows:
        assert all(row["response"]["status"] == 200 and row["sdk_retry"] == 0 for row in rows)
        generated = next(row for row in rows if row["step"] == "listing_draft")
        actual_input = json.loads(generated["body"]["messages"][-1]["content"])
        assert all(actual_input[key] == evidence[key] for key in ("metrics", "listing", "facts", "artifacts"))
        actual_output = json.loads(generated["response"]["body"]["choices"][0]["message"]["content"])
        assert actual_output["bullets"] == [{key: row[key] for key in ("text", "fact_id")}
                                            for row in evidence["proposal"]["bullets"]]
        for name, row in evidence["artifacts"].items():
            assert any(step["stage"] == "tool_result" and step["action"] == name
                       and step["runtime_call_id"] == row["runtime_call_id"] and step["status"] == "ok"
                       for step in source["steps"])
        result["sdk_input_evidence_matches"] = True
        result["sdk_draft_matches"] = True
        result["sdk_tokens"] = sum(row["response"]["body"]["usage"]["total_tokens"] for row in rows)
    (output / "listing-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify(output, live):
    from backend import observability
    from backend.agent import llm_provider, team_runtime
    from backend.captcha import captcha
    from backend.database import database, listing_operations as listings
    from backend.memory import conversation_memory
    from scripts.run_task27_t5 import GLMAudit, MODEL, browser_env
    from tests.home_diagnosis_server import demo_captcha

    username, password = "task42-operator", token_urlsafe(24)
    redactor = Redactor([password, os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY")])
    provider = llm_provider.ProviderFactory.create() if live else None
    if live and (provider._model != MODEL or str(provider._client.base_url).rstrip("/") + "/chat/completions" != GLM_ENDPOINT):
        raise RuntimeError("Expected the configured official GLM endpoint and glm-4.5-air")
    if not live:
        from tests.test_listing_workgroup import ListingModel

        provider = ListingModel()
        provider.mode = "delegate"

    database.init_db()
    database.seed_data()
    current_end = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()
    with closing(database.get_db()) as conn, conn:
        owner = conn.execute("INSERT INTO users (username,password) VALUES (?,?)",
                             (username, database.hash_password(password))).lastrowid
        listings.seed_test_product(conn, owner, current_end=current_end)
        other = conn.execute("SELECT id FROM users WHERE username='lisi'").fetchone()[0]
        listings.seed_test_product(conn, other, shop_id="task42-other", current_end=current_end)

    audit = GLMAudit(output / "glm-sdk-ledger.sqlite3", redactor, uuid4().hex, 12) if live else None
    if audit:
        audit.case = "listing"
        audit.enabled = True
        hooks = provider._client._client.event_hooks
        hooks["request"].append(audit.request)
        hooks["response"].append(audit.response)

    import uvicorn

    with ExitStack() as stack:
        if audit:
            stack.callback(provider._client.close)
            for module in (llm_provider, team_runtime):
                stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=provider))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        # This task tests Listing evidence and application. Keep acceptance independent of embedding downloads.
        stack.enter_context(patch.object(conversation_memory, "store_conversation", return_value=None))
        for name, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                               ("FUNC_LOG_FILE", "functions.jsonl")):
            stack.enter_context(patch.object(observability, name, output / filename))
        from fast_server import app

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        with socket.socket() as local:
            local.bind(("127.0.0.1", 0))
            port = local.getsockname()[1]
            local.listen()
            thread = Thread(target=server.run, kwargs={"sockets": [local]}, daemon=True)
            thread.start()
            try:
                env = browser_env()
                env.update(AD_TASK42_USERNAME=username, AD_TASK42_PASSWORD=password,
                           AD_TASK42_SQLITE=str(database.sandbox_db_path()),
                           AD_BROWSER_CHANNEL=env.get("AD_BROWSER_CHANNEL", "msedge"))
                import httpx
                from time import monotonic, sleep

                ready = monotonic() + 20
                while not server.started and thread.is_alive() and monotonic() < ready:
                    sleep(0.05)
                if not server.started or httpx.get(f"http://127.0.0.1:{port}/api/health").status_code != 200:
                    raise RuntimeError("Local FastAPI did not start")
                result = subprocess.run(["node", "tests/task42_listing_browser.cjs", f"http://127.0.0.1:{port}", str(output)],
                                        env=env, timeout=110, check=False)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                if audit:
                    (output / "sdk.json").write_text(json.dumps(audit.rows(), ensure_ascii=False, indent=2), encoding="utf-8")
    if not result.returncode:
        audit_listing(output, audit.rows() if audit else [])
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ.update(AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH=str(output / "listing.sqlite3"), JWT_SECRET=token_urlsafe(48),
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False",
                      LLM_PROVIDER="glm" if args.live else "mock")
    if args.live:
        values = dotenv_values(".env")
        os.environ["GLM_MODEL"] = "glm-4.5-air"
        if values.get("GLM_API_KEY"):
            os.environ.setdefault("GLM_API_KEY", values["GLM_API_KEY"])
        if not os.environ.get("GLM_API_KEY"):
            parser.error("Set GLM_API_KEY locally for real model acceptance")
    return verify(output, args.live)


if __name__ == "__main__":
    raise SystemExit(main())
