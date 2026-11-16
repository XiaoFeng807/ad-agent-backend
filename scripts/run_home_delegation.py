"""Serve isolated ordinary chat for the real Chromium and DeepSeek acceptance."""

import argparse
import json
import os
import socket
import sys
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from secrets import token_urlsafe
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

from scripts.run_real_diagnosis import CheckFailed, Redactor, SDKWireAudit, require


def serve(directory, scenario):
    console = sys.stdout
    report = {"scenario": scenario}
    redactor = Redactor()
    provider = None
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            import uvicorn

            from backend import observability
            from backend.agent.llm_provider import ProviderFactory
            from backend.config.config import settings
            from backend.database import database
            from backend.rag import rag_knowledge
            from scripts.eval_review_retrieval import FixedReviewEmbedding
            from tests.home_diagnosis_server import demo_captcha

            config = settings.llm_config()
            endpoint = urlsplit(config.base_url or "")
            require(config.provider == "deepseek" and config.api_key and
                    endpoint.scheme == "https" and endpoint.netloc == "api.deepseek.com",
                    "official_deepseek_configuration_required")
            settings.JWT_SECRET = token_urlsafe(32)
            redactor.secrets.extend((config.api_key, settings.OPENAI_API_KEY, settings.JWT_SECRET))
            settings.DIAGNOSIS_SANDBOX_DB_PATH = str(directory / "sandbox.sqlite3")
            settings.PLAN_METRICS_DB_PATH = ""
            settings.RAG_DB_PATH = str(directory / "knowledge")
            observability.TRACE_LOG_FILE = directory / "traces.jsonl"
            observability.FUNC_LOG_FILE = observability.ERROR_LOG_FILE = Path(os.devnull)
            database.init_db()
            with closing(database.get_db()) as conn, conn:
                conn.execute("INSERT INTO users (id, username, password) VALUES (17, 'delegation-demo', ?)",
                             (database.hash_password("demo-password"),))
                clicks, sales = (120, 400) if scenario == "normal" else (1, 0)
                conn.execute(
                    "INSERT INTO ad_plans (id, plan_name, user_id, platform, daily_budget, impressions, clicks, cost, sales) "
                    "VALUES (101, '演示搜索广告', 17, 'search', 300, 1500, ?, 100, ?)", (clicks, sales),
                )
            provider = ProviderFactory.create()
            audit = SDKWireAudit(redactor, capture_reply=True, capture_fixture_inputs=True)
            audit.attach(provider)
            report["sdk_requests"] = audit.rows
            with (patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding),
                  patch("backend.captcha.captcha.generate_captcha", demo_captcha),
                  socket.socket() as listener):
                from fast_server import app

                listener.bind(("127.0.0.1", 0))
                listener.listen()
                server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))

                def stop_on_stdin_close():
                    sys.stdin.read()
                    server.should_exit = True

                Thread(target=stop_on_stdin_close, daemon=True).start()
                print(f"DEMO_URL=http://127.0.0.1:{listener.getsockname()[1]}", file=console, flush=True)
                server.run(sockets=[listener])
            with closing(database.get_db()) as conn:
                report["committed_replies"] = [row[0] for row in conn.execute(
                    "SELECT content FROM conversations WHERE user_id=17 AND role='assistant' ORDER BY id"
                )]
            if observability.TRACE_LOG_FILE.exists():
                report["traces"] = [json.loads(row) for row in observability.TRACE_LOG_FILE.read_text(
                    encoding="utf-8"
                ).splitlines()]
            return 0
    except Exception as error:
        report["failure"] = str(error) if isinstance(error, CheckFailed) else type(error).__name__
        return 1
    finally:
        if provider is not None:
            provider._client.close()
        (directory / "server.json").write_text(json.dumps(redactor.clean(report), ensure_ascii=False, indent=2),
                                               encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("scenario", choices=("normal", "anomaly"))
    args = parser.parse_args()
    raise SystemExit(serve(args.directory.resolve(), args.scenario))
