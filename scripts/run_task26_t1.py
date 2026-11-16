"""Isolated homepage fixture for the TASK-026-T1 Chromium/DeepSeek matrix."""

import argparse
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
from secrets import token_urlsafe
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

from scripts.run_real_diagnosis import CheckFailed, Redactor, SDKWireAudit, require


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "data/plan_metrics/criteo.sqlite3"
SOURCE_PLAN = "13289562"
MISSING_DAY = "2026-10-27"
WINDOWS = (("2026-10-17", "2026-10-23"), ("2026-10-24", "2026-10-30"))
SHIFTED_WINDOWS = (("2026-10-16", "2026-10-22"), ("2026-10-23", "2026-10-29"))


def read_only(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def metrics_fixture(directory, missing, windows=WINDOWS):
    source_hash = hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()
    provenance = json.loads((ROOT / "docs/criteo-provenance.json").read_text(encoding="utf-8"))
    require(source_hash == provenance["artifact_sha256"], "unexpected_public_artifact_hash")
    path = directory / "history.sqlite3"
    shutil.copyfile(ARTIFACT, path)
    if missing:
        with closing(sqlite3.connect(path)) as conn, conn:
            deleted = conn.execute("DELETE FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date=?",
                                   (SOURCE_PLAN, MISSING_DAY)).rowcount
            require(deleted == 1, "missing_day_fixture_delete_failed")
    totals = []
    with closing(read_only(path)) as conn:
        manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
        require(manifest["kind"] == "public", "public_manifest_required")
        for start, end in windows:
            args = (SOURCE_PLAN, start, end)
            count, impressions, clicks = conn.execute(
                """SELECT COUNT(*), SUM(impressions), SUM(clicks) FROM plan_daily_metrics
                   WHERE source_plan_id=? AND metric_date BETWEEN ? AND ?""", args,
            ).fetchone()
            days = {row[0] for row in conn.execute(
                """SELECT metric_date FROM plan_daily_metrics
                   WHERE source_plan_id=? AND metric_date BETWEEN ? AND ?""", args,
            )}
            first = date.fromisoformat(start)
            expected = {(first + timedelta(days=i)).isoformat() for i in range(7)}
            totals.append({"start_date": start, "end_date": end, "observed_days": count,
                           "missing_dates": sorted(expected - days), "impressions": impressions,
                           "clicks": clicks, "ctr": clicks / impressions if impressions else None})
    require(totals[0]["missing_dates"] == [] and
            totals[1]["missing_dates"] == ([MISSING_DAY] if missing else []), "unexpected_sql_coverage")
    return path, {"source_sha256": source_hash, "fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "manifest": manifest, "source_plan_id": SOURCE_PLAN, "windows": totals,
                  "relative_drop": None if missing else 1 - totals[1]["ctr"] / totals[0]["ctr"]}


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
            from backend.rag.review_rule_demo import DEMO_RULES, seed_review_rules
            from scripts.eval_review_retrieval import FixedReviewEmbedding
            from tests.home_diagnosis_server import demo_captcha

            config = settings.llm_config()
            endpoint = urlsplit(config.base_url or "")
            require(config.provider == "deepseek" and config.api_key and config.model == "deepseek-flash"
                    and endpoint.scheme == "https" and endpoint.netloc == "api.deepseek.com",
                    "official_deepseek_flash_configuration_required")
            settings.JWT_SECRET = token_urlsafe(32)
            redactor.secrets.extend((config.api_key, settings.OPENAI_API_KEY, settings.JWT_SECRET))
            settings.DIAGNOSIS_SANDBOX_DB_PATH = str(directory / "sandbox.sqlite3")
            settings.RAG_DB_PATH = str(directory / "knowledge")
            metrics, report["sql"] = metrics_fixture(directory, scenario == "missing",
                                                     SHIFTED_WINDOWS if scenario == "shifted" else WINDOWS)
            settings.PLAN_METRICS_DB_PATH = str(metrics)
            observability.TRACE_LOG_FILE = directory / "traces.jsonl"
            observability.FUNC_LOG_FILE = observability.ERROR_LOG_FILE = Path(os.devnull)
            database.init_db()
            with closing(database.get_db()) as conn, conn:
                conn.execute("INSERT INTO users (id, username, password) VALUES (17, 'delegation-demo', ?)",
                             (database.hash_password("demo-password"),))
                clicks, sales = (120, 400) if scenario == "normal" else (1, 0)
                conn.execute(
                    "INSERT INTO ad_plans (id, plan_name, user_id, platform, daily_budget, impressions, clicks, cost, sales) "
                    "VALUES (101, '隔离演示计划', 17, 'search', 300, 1500, ?, 100, ?)", (clicks, sales),
                )
                manifest = report["sql"]["manifest"]
                conn.execute("INSERT INTO plan_metric_bindings VALUES (101, ?, ?, ?)",
                             (manifest["source_id"], manifest["data_version"], SOURCE_PLAN))
            provider = ProviderFactory.create()
            audit = SDKWireAudit(redactor, capture_reply=True, capture_fixture_inputs=True)
            audit.attach(provider)
            report["sdk_requests"] = audit.rows
            with (patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding),
                  patch("backend.captcha.captcha.generate_captcha", demo_captcha),
                  socket.socket() as listener):
                from fast_server import app

                rag = rag_knowledge.get_rag_knowledge()
                seed_review_rules(rag)
                report["rule_originals"] = {}
                for chunk_id, _, version, _, content in DEMO_RULES:
                    if version == "v1" and chunk_id.startswith("ctr-review-"):
                        chunk = rag.get_chunk(chunk_id)
                        require(chunk and chunk["content"] == content, "rule_original_mismatch")
                        report["rule_originals"][chunk_id] = content

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
                    encoding="utf-8").splitlines()]
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
    parser.add_argument("scenario", choices=("normal", "available", "missing", "control", "shifted", "single"))
    args = parser.parse_args()
    raise SystemExit(serve(args.directory.resolve(), args.scenario))
