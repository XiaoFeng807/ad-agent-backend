"""Start the real homepage on loopback with isolated data and the existing fixed model."""

import socket
import sys
import os
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import uvicorn
from PIL import Image, ImageDraw

from backend import observability
from backend.agent.llm_provider import ProviderFactory
from backend.agent.tests.test_ad_diagnosis_eval import SEVEN_DAYS, THREE_DAYS, StatefulDiagnosisModel
from backend.config.config import settings
from backend.database import database
from backend.memory import conversation_memory, memory_manager
from backend.rag import rag_knowledge
from backend.rag.review_rule_demo import seed_review_rules
from scripts.eval_review_retrieval import FixedReviewEmbedding


def demo_captcha():
    image = Image.new("RGB", (120, 40), "white")
    ImageDraw.Draw(image).text((12, 10), "0726", fill="black")
    return image, "0726"


def prepare_pause_sandbox(directory):
    from backend.agent.tests.test_plan_metrics import synthetic_artifact
    from backend.database.diagnosis_pauses import register_sandbox_target

    database.init_db()
    settings.PLAN_METRICS_DB_PATH = str(directory / "synthetic.sqlite3")
    with closing(database.get_db()) as conn, conn:
        if conn.execute("SELECT 1 FROM users WHERE id=17").fetchone() is not None:
            return
        _, _, manifest = synthetic_artifact(directory, (
            (1000, 100), (1000, 80), (2000, 200), (2000, 160), (2000, 100), (2000, 95),
        ))
        conn.executemany("INSERT INTO users (id, username, password) VALUES (?, ?, ?)", [
            (user_id, username, database.hash_password("demo-password"))
            for user_id, username in [(17, "diagnosis-demo"), (18, "second-demo")]
        ])
        conn.executemany("INSERT INTO ad_plans (id, plan_name, user_id) VALUES (?, ?, ?)", [
            (101, "暂停审批演示计划", 17), (102, "同属主未登记计划", 17), (201, "第二用户计划", 18),
        ])
        conn.executemany("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, 'fixture-campaign')", [
            (plan_id, manifest["source_id"], manifest["data_version"]) for plan_id in (101, 102, 201)
        ])
    register_sandbox_target(17, 101)
    register_sandbox_target(18, 201)


def prepare_diagnosis_demo():
    database.init_db()
    with closing(database.get_db()) as conn, conn:
        if conn.execute("SELECT 1 FROM users WHERE id=17").fetchone() is None:
            conn.executemany("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                             [(user_id, username, database.hash_password("demo-password"))
                              for user_id, username in [(17, "diagnosis-demo"), (18, "second-demo")]])
            conn.execute("INSERT INTO ad_plans (id, plan_name, user_id) VALUES (101, '演示计划', 17)")
            conn.executemany(
                "INSERT INTO daily_reports (report_date, user_id, impressions, clicks) VALUES (?, 17, 200, 10)",
                [(window["start_date"],) for window in [*SEVEN_DAYS.values(), THREE_DAYS["baseline"]]],
            )

    model_class = StatefulDiagnosisModel
    if artifact := os.environ.get("PLAN_METRICS_DEMO_ARTIFACT"):
        from backend.agent.tests.test_plan_metrics_flow import PlanMetricsDiagnosisModel
        from scripts.bind_plan_metrics import bind
        settings.PLAN_METRICS_DB_PATH = str(Path(artifact).resolve())
        bind(database.DB_PATH, artifact, 101, 17, "13289562")
        model_class = PlanMetricsDiagnosisModel
    return model_class


def serve(directory, *, pause_sandbox=False):
    database.DB_PATH = directory / "demo.sqlite3"
    memory_manager.MEMORY_DIR = directory / "memory"
    conversation_memory.DB_PATH = str(directory / "conversation_memory")
    observability.TRACE_LOG_FILE = directory / "traces.jsonl"
    settings.RAG_DB_PATH = str(directory / "chroma")
    settings.DIAGNOSIS_SANDBOX_DB_PATH = str(directory / "pause.sqlite3") if pause_sandbox else ""
    if pause_sandbox:
        from backend.agent.tests.test_plan_metrics_flow import PlanMetricsDiagnosisModel

        prepare_pause_sandbox(directory)
        model_class = PlanMetricsDiagnosisModel
    else:
        model_class = prepare_diagnosis_demo()

    with (patch.object(ProviderFactory, "create", side_effect=lambda *args, **kwargs: model_class()),
          patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding),
          patch.object(conversation_memory, "BGEEmbedding", FixedReviewEmbedding),
          patch("backend.captcha.captcha.generate_captcha", demo_captcha),
          closing(rag_knowledge.get_rag_knowledge()) as rag,
          socket.socket() as listener):
        seed_review_rules(rag)
        from fast_server import app

        listener.bind(("127.0.0.1", 0))
        listener.listen()
        print(f"DEMO_URL=http://127.0.0.1:{listener.getsockname()[1]}", flush=True)
        # The demo uses the real routes with its own seeds instead of the app's startup mock reports.
        uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning")).run(sockets=[listener])


if __name__ == "__main__":
    serve(Path(sys.argv[1]), pause_sandbox="--pause-sandbox" in sys.argv[2:])
