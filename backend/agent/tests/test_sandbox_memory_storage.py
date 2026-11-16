"""Process-level isolation of synthetic SQLite, JSON and local Chroma stores."""

from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


def run_instance(root, instance, phase):
    from backend.database import database
    from backend.memory import conversation_memory, memory_manager
    from scripts.eval_review_retrieval import FixedReviewEmbedding

    root = Path(root)
    database.DB_PATH = str(root / "normal.sqlite")
    sandbox = root / (instance + ".sqlite")
    database.settings.DIAGNOSIS_SANDBOX_DB_PATH = (
        "" if instance == "normal" else
        str(sandbox) if phase == "write" else str(root / "alias" / ".." / sandbox.name)
    )
    database.settings.PLAN_METRICS_DB_PATH = ""
    memory_manager.MEMORY_DIR = str(root / "normal-json")
    conversation_memory.DB_PATH = str(root / "normal-chroma")
    conversation_memory.BGEEmbedding = FixedReviewEmbedding
    marker = "MARKER_" + instance.upper()
    question, answer = "历史 " + marker, "答复 " + marker
    paths = {"json": [], "temporary": [], "chroma": []}
    get_path = memory_manager._get_path
    temporary = memory_manager.NamedTemporaryFile
    client = conversation_memory.chromadb.PersistentClient

    def record_json(user_id):
        path = get_path(user_id)
        paths["json"].append(str(Path(path).resolve().parent))
        return path

    def record_temporary(*args, **kwargs):
        paths["temporary"].append(str(Path(kwargs["dir"]).resolve()))
        return temporary(*args, **kwargs)

    def record_client(*args, **kwargs):
        paths["chroma"].append(str(Path(kwargs["path"]).resolve()))
        return client(*args, **kwargs)

    with patch.object(memory_manager, "_get_path", side_effect=record_json), \
         patch.object(memory_manager, "NamedTemporaryFile", side_effect=record_temporary), \
         patch.object(conversation_memory.chromadb, "PersistentClient", side_effect=record_client):
        if phase == "write":
            database.init_db()
            with closing(database.get_db()) as conn, conn:
                conn.execute("INSERT INTO conversations (id, user_id, role, content) VALUES (1, 17, 'user', ?)", (question,))
                conn.execute("INSERT INTO conversations (id, user_id, role, content) VALUES (2, 17, 'assistant', ?)", (answer,))
                conn.execute("INSERT INTO conversation_turns VALUES (1, 2, 17)")
            memory_manager.store_conversation_summary(17, question, answer)
        conversation_memory.store_conversation(17, question, answer, user_message_id=1, assistant_message_id=2)
        recalled = conversation_memory.search_conversation("历史", 17, through_message_id=3)
        summary = memory_manager.get_conversation_summaries(17)
        collection = conversation_memory._get_collection(conversation_memory._get_client())
        count = collection.count()
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE conversations SET content='changed' WHERE id=2")
        rejected = conversation_memory.search_conversation("历史", 17, through_message_id=3)
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE conversations SET content=? WHERE id=2", (answer,))
    return {"paths": paths, "summary": summary, "profile": memory_manager.get_profile_text(17),
            "recalled": recalled, "count": count, "rejected": rejected}


def run_home_instance(root, instance, phase, output):
    from backend.agent.tests.test_chat_memory_isolation import IsolationModel, STORE, raw
    from backend.agent.tests.test_home_sse_tracing import HomeSSEFixture
    from backend.database import database
    from backend.memory import conversation_memory, memory_manager
    from scripts.eval_review_retrieval import FixedReviewEmbedding

    root = Path(root)
    db_path = root / (instance + ".sqlite")
    marker = "HOME_MARKER_" + instance.upper()

    class HomeCase(HomeSSEFixture):
        model_class = IsolationModel

        def connect(self):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn

        async def asyncSetUp(self):
            if db_path.exists():
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.execute("DELETE FROM sessions")
            self.enterContext(patch.object(database, "DB_PATH", str(root / "normal.sqlite")))
            self.enterContext(patch.object(database.settings, "DIAGNOSIS_SANDBOX_DB_PATH",
                                           "" if instance == "normal" else str(db_path)))
            self.enterContext(patch.object(database.settings, "PLAN_METRICS_DB_PATH", ""))
            await super().asyncSetUp()
            self.enterContext(patch.object(memory_manager, "MEMORY_DIR", str(root / "normal-json")))
            self.enterContext(patch.object(conversation_memory, "DB_PATH", str(root / "normal-chroma")))
            self.enterContext(patch.object(conversation_memory, "BGEEmbedding", FixedReviewEmbedding))
            self.memory_store.side_effect = STORE

        async def test_home(self):
            if phase == "home_write":
                await self.request(message="什么是历史 " + marker)
                with closing(self.connect()) as conn, conn:
                    conn.executemany("INSERT INTO conversations (user_id, role, content) VALUES (17, 'user', ?)",
                                     [(f"padding {index}",) for index in range(55)])
            else:
                await self.request(message="还记得历史标记吗")
                self.assertIn(marker, raw(self.model.inputs))
                for other in {"normal", "sandbox_a", "sandbox_b"} - {instance}:
                    self.assertNotIn("HOME_MARKER_" + other.upper(), raw(self.model.inputs))
            self.assertEqual(self.events[-1]["type"], "done")
            self.assertIn(marker, memory_manager.get_conversation_summaries(17))
            Path(output).write_text(json.dumps({"ok": True}), encoding="utf-8")

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(HomeCase))
    if not result.wasSuccessful():
        raise SystemExit(1)


class SandboxMemoryStorageTests(unittest.TestCase):
    def test_three_instances_restart_and_idempotency(self):
        with TemporaryDirectory(dir=Path.cwd(), ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            (root / "alias").mkdir()
            (root / "normal-json").mkdir()
            (root / "normal-json" / "user_17.json").write_text(json.dumps({
                "user_id": 17, "short_term": [],
                "long_term_profile": {"frequent_actions": [{"action": "LEGACY_FORMAT", "count": 1}],
                                      "preferences": [], "total_interactions": 1},
                "business_memory": {"accounts": {}, "recent_decisions": [], "alert_history": [], "key_metrics": []},
            }), encoding="utf-8")
            results = {}
            env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", HF_HUB_OFFLINE="1",
                       TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False", PYTHONUTF8="1")
            for phase in ("write", "restart"):
                for instance in ("normal", "sandbox_a", "sandbox_b"):
                    output = root / (phase + "-" + instance + ".json")
                    process = subprocess.run([sys.executable, "-m", __name__, str(root), instance, phase, str(output)],
                                             env=env, timeout=90, capture_output=True, text=True, encoding="utf-8")
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    results[phase, instance] = json.loads(output.read_text(encoding="utf-8"))
            for phase in ("write", "restart"):
                for instance in ("normal", "sandbox_a", "sandbox_b"):
                    data = results[phase, instance]
                    marker = "MARKER_" + instance.upper()
                    self.assertIn(marker, data["summary"])
                    self.assertIn(marker, json.dumps(data["recalled"]))
                    self.assertEqual(data["count"], 1)
                    self.assertEqual(data["rejected"], [])
                    if instance == "normal":
                        self.assertIn("LEGACY_FORMAT", data["profile"])
                    for other in {"normal", "sandbox_a", "sandbox_b"} - {instance}:
                        self.assertNotIn("MARKER_" + other.upper(), json.dumps(data))
                    expected = root / ("normal-json" if instance == "normal" else instance + ".sqlite.memory/user_memory")
                    chroma = root / ("normal-chroma" if instance == "normal" else instance + ".sqlite.memory/conv_memory")
                    self.assertEqual(set(data["paths"]["json"]), {str(expected.resolve())})
                    self.assertEqual(set(data["paths"]["chroma"]), {str(chroma.resolve())})
                    if phase == "write":
                        self.assertEqual(set(data["paths"]["temporary"]), {str(expected.resolve())})

    def test_home_chat_three_instances_restart(self):
        with TemporaryDirectory(dir=Path.cwd(), ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", HF_HUB_OFFLINE="1",
                       TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False", PYTHONUTF8="1")
            for phase in ("home_write", "home_restart"):
                for instance in ("normal", "sandbox_a", "sandbox_b"):
                    output = root / (phase + "-" + instance + ".json")
                    process = subprocess.run([sys.executable, "-m", __name__, str(root), instance, phase, str(output)],
                                             env=env, timeout=90, capture_output=True, text=True, encoding="utf-8")
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    self.assertTrue(json.loads(output.read_text(encoding="utf-8"))["ok"])


if __name__ == "__main__":
    if len(sys.argv) == 5:
        if sys.argv[3].startswith("home_"):
            run_home_instance(*sys.argv[1:])
        else:
            result = run_instance(*sys.argv[1:4])
            Path(sys.argv[4]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    else:
        unittest.main()
