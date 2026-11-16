"""Drive the natural homepage with DeepSeek, isolated data and a fixed-rule control."""

import argparse
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
from fractions import Fraction
import hashlib
import json
import logging
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import socket
import sqlite3
import subprocess
import sys
from threading import Thread
from urllib.parse import urlsplit

from scripts.run_acceptance import verify_artifact
from scripts.run_real_diagnosis import ROOT, CheckFailed, Redactor, SDKWireAudit, require


CAMPAIGNS = {
    "normal": ("15398570", "10341182", "25172774"),
    "anomaly": ("32009848", "27589150", "13289562"),
    "missing": ("32009848", "27589150", "13289562"),
}


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixed_reference(artifact, manifest, campaigns):
    """Independent SQL control. Fixed latest 7 + previous 7, exact arithmetic and v1 thresholds."""
    latest = date.fromisoformat(manifest["complete_end_date"])
    starts = [latest - timedelta(days=13), latest - timedelta(days=6)]
    ranking, gaps = [], []
    with closing(sqlite3.connect(artifact.as_uri() + "?mode=ro", uri=True)) as conn:
        for plan, campaign in campaigns:
            totals = []
            for start in starts:
                end = start + timedelta(days=6)
                dates = {str(start + timedelta(days=day)) for day in range(7)}
                rows = conn.execute(
                    """SELECT metric_date, impressions, clicks FROM plan_daily_metrics
                       WHERE source_plan_id=? AND metric_date BETWEEN ? AND ?""",
                    (campaign, str(start), str(end)),
                ).fetchall()
                missing = sorted(dates - {row[0] for row in rows})
                if missing:
                    gaps.append({"plan_id": plan, "missing_dates": missing})
                totals.append({"start_date": str(start), "end_date": str(end), "coverage_days": len(rows),
                               "impressions": sum(row[1] for row in rows), "clicks": sum(row[2] for row in rows)})
            base, current = totals
            b, c = (Fraction(item["clicks"], item["impressions"]) if item["impressions"] else Fraction(0)
                    for item in totals)
            if not b or min(base["impressions"], current["impressions"]) < 1000:
                gaps.append({"plan_id": plan, "reason": "insufficient_sample"})
            ranking.append(((b - c) / b * 100 if b else Fraction(0), {
                "plan_id": plan, "source_plan_id": campaign, "baseline": base, "current": current,
                "baseline_ctr_pct": float(b * 100), "current_ctr_pct": float(c * 100),
                "decline_pp": float((b - c) * 100),
            }))
    ranking.sort(key=lambda item: item[0], reverse=True)
    decline, best = ranking[0]
    state = "insufficient" if gaps or decline == ranking[1][0] else "matched" if decline >= 20 else "normal"
    return {"state": state, "ranking": [{**row, "relative_decline_pct": float(value)} for value, row in ranking],
            "eligible_plan_id": best["plan_id"] if state == "matched" else None, "gaps": gaps,
            "rule": {"version": "v1", "min_impressions": 1000, "min_relative_decline_pct": 20},
            "model_calls": 0, "metric_queries": 2 * len(campaigns)}


def db_snapshot(database):
    with closing(database.get_db(read_only=True)) as conn:
        return {
            "plans": [dict(row) for row in conn.execute("SELECT id, user_id, status FROM ad_plans ORDER BY id")],
            "pause_registrations": [dict(row) for row in conn.execute(
                "SELECT plan_id, user_id FROM diagnosis_sandbox_targets ORDER BY plan_id"
            )],
            "counts": {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
                "diagnosis_review_proposals", "diagnosis_pause_proposals", "diagnosis_reviews",
                "diagnosis_pause_receipts",
            )},
        }


def serve(directory, scenario, artifact, inject_temporary_failure=False):
    """Expose only the real app on a loopback socket. The browser closes stdin to stop it."""
    console = sys.stdout
    report = {"scenario": scenario, "sdk_requests": [], "approval_snapshots": []}
    redactor = Redactor()
    database = provider = rag = None
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            logging.disable(logging.CRITICAL)
            os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False")
            from unittest.mock import patch

            import uvicorn
            from backend import observability
            from backend.agent import natural_diagnosis
            from backend.agent.llm_provider import ProviderFactory
            from backend.config.config import settings
            from backend.database import database
            from backend.database.diagnosis_pauses import register_sandbox_target
            from backend.rag import rag_knowledge
            from backend.rag.review_rule_demo import seed_review_rules
            from scripts.bind_plan_metrics import bind
            from scripts.eval_review_retrieval import FixedReviewEmbedding
            from tests.home_diagnosis_server import demo_captcha

            config = settings.llm_config()
            redactor.secrets.extend(getattr(settings, key) for key in
                                    ("API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "JWT_SECRET"))
            report.update(provider=config.provider, model=config.model, embedding="fixed")
            require(config.provider == "deepseek", "configured_provider_is_not_deepseek")
            require(bool(config.api_key.strip()) and not config.api_key.lower().startswith(("your_", "replace")),
                    "deepseek_credential_missing")
            endpoint = urlsplit(config.base_url)
            require(endpoint.scheme == "https" and endpoint.hostname == "api.deepseek.com", "unexpected_deepseek_endpoint")

            verify_artifact(artifact)
            campaigns = list(zip((101, 102, 103), CAMPAIGNS[scenario], strict=True))
            with closing(sqlite3.connect(artifact.as_uri() + "?mode=ro", uri=True)) as conn:
                manifest = json.loads(conn.execute("SELECT manifest FROM metadata").fetchone()[0])
            if scenario == "missing":
                missing_date = str(date.fromisoformat(manifest["complete_end_date"]) - timedelta(days=1))
                derivative = directory / "missing-metrics.sqlite3"
                shutil.copyfile(artifact, derivative)
                patch_id = f"omit:{campaigns[1][1]}:{missing_date}"
                manifest["data_version"] = hashlib.sha256((manifest["data_version"] + patch_id).encode()).hexdigest()
                manifest["sampling"] += "; diagnostic fixture with one omitted campaign-day"
                with closing(sqlite3.connect(derivative)) as conn, conn:
                    conn.execute("DELETE FROM plan_daily_metrics WHERE source_plan_id=? AND metric_date=?",
                                 (campaigns[1][1], missing_date))
                    conn.execute("UPDATE metadata SET manifest=?", (json.dumps(manifest),))
                artifact = derivative
                report["injected_omission"] = {"plan_id": campaigns[1][0], "date": missing_date}
            report["fixed_rule"] = fixed_reference(artifact, manifest, campaigns)
            report["metrics_source"] = {"source_id": manifest["source_id"], "data_version": manifest["data_version"]}

            settings.PLAN_METRICS_DB_PATH = str(artifact)
            settings.DIAGNOSIS_SANDBOX_DB_PATH = str(directory / "sandbox.sqlite3")
            settings.RAG_DB_PATH = str(directory / "chroma")
            settings.JWT_SECRET = token_urlsafe(32)
            redactor.secrets.append(settings.JWT_SECRET)
            observability.TRACE_LOG_FILE = directory / "traces.jsonl"
            observability.FUNC_LOG_FILE = Path(os.devnull)
            observability.ERROR_LOG_FILE = Path(os.devnull)
            database.init_db()
            with closing(database.get_db()) as conn, conn:
                conn.execute("INSERT INTO users (id, username, password) VALUES (17, 'diagnosis-demo', ?)",
                             (database.hash_password("demo-password"),))
                conn.execute("INSERT INTO users (id, username, password) VALUES (18, 'other-demo', 'unused')")
                conn.executemany("INSERT INTO ad_plans (id, plan_name, user_id) VALUES (?, ?, ?)", [
                    (101, "候选 A", 17), (102, "候选 B", 17), (103, "未登记控制计划", 17),
                    (201, "另一属主控制计划", 18),
                ])
            for plan, campaign in campaigns:
                bind(database.sandbox_db_path(), artifact, plan, 17, campaign)
            for plan in (101, 102):
                register_sandbox_target(17, plan)
            report["foreign_binding"] = bind(database.sandbox_db_path(), artifact, 201, 18, "15959625")
            register_sandbox_target(18, 201)
            report["db_start"] = db_snapshot(database)
            provider = ProviderFactory.create()
            audit = SDKWireAudit(redactor, capture_reply=True, capture_fixture_inputs=True)
            audit.attach(provider)
            report["sdk_requests"] = audit.rows
            with (ExitStack() as faults, patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding),
                  patch("backend.captcha.captcha.generate_captcha", demo_captcha),
                  closing(rag_knowledge.get_rag_knowledge()) as rag, socket.socket() as listener):
                seed_review_rules(rag)
                if inject_temporary_failure:
                    original_query = natural_diagnosis.query_plan_metrics

                    def fail_once(**kwargs):
                        if kwargs["plan_id"] == 102 and kwargs["end_date"] == manifest["complete_end_date"]:
                            args = {key: kwargs[key] for key in ("plan_id", "start_date", "end_date")}
                            if "injected_failure" not in report:
                                report["injected_failure"] = {"arguments": args, "attempts": [args]}
                                raise TimeoutError
                            report["injected_failure"]["attempts"].append(args)
                        return original_query(**kwargs)

                    faults.enter_context(patch.object(natural_diagnosis, "query_plan_metrics", fail_once))
                from fast_server import app

                @app.middleware("http")
                async def inspect_approval(request, call_next):
                    path = request.url.path
                    if request.method != "POST" or not path.startswith("/api/diagnosis/") or not path.endswith("/confirm"):
                        return await call_next(request)
                    before = db_snapshot(database)
                    response = await call_next(request)
                    report["approval_snapshots"].append({"path": path, "status": response.status_code,
                                                         "before": before, "after": db_snapshot(database)})
                    return response

                listener.bind(("127.0.0.1", 0))
                listener.listen()
                server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical"))

                def stop_on_eof():
                    sys.stdin.read()
                    server.should_exit = True

                Thread(target=stop_on_eof, daemon=True).start()
                print(f"DEMO_URL=http://127.0.0.1:{listener.getsockname()[1]}", file=console, flush=True)
                server.run(sockets=[listener])
            report["db_end"] = db_snapshot(database)
            with closing(database.get_db()) as conn, conn:
                conn.execute("DELETE FROM sessions")
            for row in audit.rows:
                if "status" not in row:
                    row["failure_reason"] = "provider_transport_failure_or_timeout"
    except Exception as error:
        report["failure"] = str(error) if isinstance(error, CheckFailed) else type(error).__name__
    finally:
        if provider:
            provider._client.close()
        write_json(directory / "server.json", redactor.clean(report))
    return int("failure" in report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "logs/task23-real")
    parser.add_argument("--plan-metrics-db", type=Path, default=ROOT / "data/plan_metrics/criteo.sqlite3")
    parser.add_argument("--serve", choices=CAMPAIGNS)
    parser.add_argument("--scenario", choices=CAMPAIGNS)
    parser.add_argument("--inject-temporary-failure", action="store_true")
    args = parser.parse_args(argv)
    if args.inject_temporary_failure and (args.serve or args.scenario) != "normal":
        parser.error("--inject-temporary-failure requires the normal scenario")
    directory, artifact = args.output_dir.resolve(), args.plan_metrics_db.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.serve:
        return serve(directory, args.serve, artifact, args.inject_temporary_failure)
    env = {**os.environ, "PYTHON": sys.executable, "PLAYWRIGHT_BROWSERS_PATH": "0"}
    return subprocess.run(["node", str(ROOT / "tests/home_natural_diagnosis_browser.cjs"),
                           str(directory), str(artifact), args.scenario or "",
                           *(["--inject-temporary-failure"] if args.inject_temporary_failure else [])],
                          cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
