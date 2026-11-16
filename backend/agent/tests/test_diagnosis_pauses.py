"""Approval and readback through real FastAPI routes and separate SQLite files."""

import asyncio
from contextlib import closing
import importlib
import json
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from backend import observability
from backend.agent.agent_pool import AgentPool
from backend.agent.llm_provider import ProviderFactory
from backend.agent.tests.test_plan_metrics_flow import PlanMetricsDiagnosisModel
from backend.config.config import settings
from backend.database import database
from backend.database.diagnosis_pauses import PAUSE_RULE_ID, register_sandbox_target
from backend.memory import memory_manager
from backend.rag import rag_knowledge
from backend.rag.review_rule_demo import seed_review_rules
from scripts.eval_review_retrieval import FixedReviewEmbedding
from scripts.import_criteo_metrics import sha256_file
from tests.home_diagnosis_server import prepare_pause_sandbox


class DiagnosisPauseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.sandbox_path = directory / "pause.sqlite3"
        self.enterContext(patch.object(database, "DB_PATH", directory / "normal.sqlite3"))
        self.enterContext(patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", ""))
        database.init_db()
        with closing(database.get_db()) as conn, conn:
            conn.execute("INSERT INTO ad_plans (id, user_id, plan_name) VALUES (101, 17, 'ordinary plan')")
        settings.DIAGNOSIS_SANDBOX_DB_PATH = str(self.sandbox_path)
        self.enterContext(patch.object(settings, "PLAN_METRICS_DB_PATH", ""))
        prepare_pause_sandbox(directory)

        self.enterContext(patch.object(settings, "JWT_SECRET", token_urlsafe(32)))
        self.enterContext(patch.object(settings, "RAG_DB_PATH", str(directory / "chroma")))
        self.enterContext(patch.object(memory_manager, "MEMORY_DIR", str(directory / "memory")))
        self.enterContext(patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding))
        self.addCleanup(rag_knowledge._open_knowledge.cache_clear)
        rag = self.enterContext(closing(rag_knowledge.get_rag_knowledge()))
        seed_review_rules(rag)
        self.trace_path = directory / "traces.jsonl"
        self.enterContext(patch.object(observability, "TRACE_LOG_FILE", self.trace_path))
        self.enterContext(patch.object(observability, "_traces", {}))
        if archive := getattr(self, "archive_traces", None):
            self.addCleanup(archive, self.id(), self.trace_path)
        self.model = PlanMetricsDiagnosisModel()
        with patch.object(ProviderFactory, "create", return_value=self.model):
            self.server = importlib.import_module("fast_server")
            self.enterContext(patch.object(self.server, "agent_pool", AgentPool(min_size=1, max_size=1)))
        self.client = await self.enterAsyncContext(AsyncClient(
            transport=ASGITransport(app=self.server.app), base_url="http://test",
        ))
        self.owner = await self.login("diagnosis-demo")
        self.other = await self.login("second-demo")

    async def login(self, username):
        response = await self.client.post("/api/login", json={"username": username, "password": "demo-password"})
        self.assertEqual(response.json()["code"], 200)
        return {"authorization": "Bearer " + response.json()["data"]["token"]}

    def rows(self, sql, params=()):
        with closing(database.get_db()) as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def execute(self, sql, params=()):
        with closing(database.get_db()) as conn, conn:
            conn.execute(sql, params)

    def plans(self):
        return self.rows("SELECT * FROM ad_plans ORDER BY id")

    def receipts(self):
        return self.rows("SELECT * FROM diagnosis_pause_receipts")

    async def diagnose(self, *, plan_id=101, rule=PAUSE_RULE_ID, days=(1, 2), task_id=None):
        params = {"task_id": task_id} if task_id else {"plan_id": plan_id, "rule_id": rule}
        for key, day in zip(("baseline", "current"), days):
            date = f"2026-10-{day:02}"
            params[key] = {"start_date": date, "end_date": date}
        response = await self.client.post("/api/chat/stream", headers=self.owner, json={"diagnosis": params})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])
        return events[-1]

    async def action(self, action_id, *, read=False, headers=None, **kwargs):
        path = f"/api/diagnosis/pause-actions/{action_id}"
        return await self.client.request("GET" if read else "POST", path if read else path + "/confirm",
                                         headers=self.owner if headers is None else headers, **kwargs)

    async def propose(self):
        return (await self.diagnose())["pause_proposal"]

    async def test_pinned_evidence_and_only_approved_target_changes(self):
        before = self.plans()
        first = await self.diagnose()
        proposal = first["pause_proposal"]
        review = first["review_proposal"]
        self.assertNotEqual(proposal["action_id"], review["action_id"])
        self.assertEqual(proposal["source_trace_id"], first["trace_id"])
        self.assertEqual(proposal["turn_id"], review["turn_id"])
        self.assertEqual(proposal["target"]["expected_status"], "active")
        self.assertEqual(self.rows("SELECT reply IS NOT NULL AS saved FROM diagnosis_turns WHERE id=?",
                                   (proposal["turn_id"],)), [{"saved": 1}])
        second = await self.diagnose(days=(5, 6), task_id=first["task_id"])
        self.assertNotIn("pause_proposal", second)
        review_response = await self.client.post(
            f"/api/diagnosis/review-proposals/{review['action_id']}/confirm", headers=self.owner,
        )
        self.assertEqual(review_response.status_code, 200)
        self.assertEqual(self.plans(), before)
        self.assertEqual(self.receipts(), [])
        self.assertEqual((await self.action(proposal["action_id"], read=True)).status_code, 404)
        response = await self.action(proposal["action_id"], json={"plan_id": 201, "status": "active"})
        self.assertEqual(response.status_code, 200)
        receipt = response.json()["data"]
        self.assertEqual({key: receipt[key] for key in proposal}, proposal)
        self.assertEqual(receipt["calculation"]["relative_decline_pct"], 20)
        self.assertEqual(receipt["committed_status"], "paused")
        self.assertEqual(len(self.receipts()), 1)
        before[0]["status"] = "paused"
        self.assertEqual(self.plans(), before)
        with patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", ""):
            self.assertEqual([(p["plan_name"], p["status"]) for p in self.plans()], [("ordinary plan", "active")])
        read = (await self.action(proposal["action_id"], read=True)).json()["data"]
        self.assertEqual(read, {"actual_status": "paused", "receipt": receipt})
        trace = observability.get_trace(receipt["execution_trace_id"])
        self.assertEqual(trace["status"], "completed")
        self.assertIn(first["trace_id"], trace["steps"][0]["detail"])

    async def test_evidence_gaps_nonmatch_and_other_rule_block_pause(self):
        missing = await self.diagnose(days=(1, 10))
        self.assertTrue(missing["review_proposal"]["evidence_gaps"])
        nonmatch = await self.diagnose(days=(5, 6))
        self.assertIs(nonmatch["review_proposal"]["rule_evaluation"]["matched"], False)
        v2 = await self.diagnose(rule="ctr-review-v2#low-ctr", days=(3, 4))
        self.assertIs(v2["review_proposal"]["rule_evaluation"]["matched"], True)
        for event in (missing, nonmatch, v2):
            self.assertNotIn("pause_proposal", event)
        self.assertEqual(self.rows("SELECT * FROM diagnosis_pause_proposals"), [])
        self.assertEqual([p["status"] for p in self.plans()], ["active"] * 3)

    async def test_unregistered_or_other_owner_plan_has_no_pause_proposal(self):
        unregistered = await self.diagnose(plan_id=102)
        self.assertTrue(unregistered["review_proposal"]["rule_evaluation"]["matched"])
        self.assertNotIn("pause_proposal", unregistered)
        foreign = await self.diagnose(plan_id=201)
        self.assertNotIn("review_proposal", foreign)
        self.assertNotIn("pause_proposal", foreign)
        with self.assertRaisesRegex(ValueError, "同属主"):
            register_sandbox_target(17, 201)
        self.assertEqual(self.rows("SELECT * FROM diagnosis_pause_proposals"), [])

    async def test_disabled_mode_ignores_existing_registry_and_actions(self):
        proposal = await self.propose()
        with patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", ""), \
                patch.object(database, "DB_PATH", self.sandbox_path):
            event = await self.diagnose()
            self.assertTrue(event["review_proposal"]["rule_evaluation"]["matched"])
            self.assertNotIn("pause_proposal", event)
            self.assertEqual((await self.action(proposal["action_id"])).status_code, 404)
            self.assertEqual((await self.action(proposal["action_id"], read=True)).status_code, 404)
            self.assertEqual(self.receipts(), [])

    async def test_sandbox_cannot_target_normal_or_metrics_database(self):
        for path in (database.DEFAULT_DB_PATH, database.DB_PATH, settings.PLAN_METRICS_DB_PATH):
            with patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(path)):
                with self.assertRaisesRegex(ValueError, "独立"):
                    database.get_db()

        # The importer output remains a metrics artifact even without an active binding.
        artifact = Path(settings.PLAN_METRICS_DB_PATH)
        digest = sha256_file(artifact)

        def schema_tables():
            with closing(sqlite3.connect(f"{artifact.as_uri()}?mode=ro", uri=True)) as conn:
                return conn.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()

        original_tables = schema_tables()
        self.assertEqual(original_tables, [("metadata",), ("plan_daily_metrics",)])
        with patch.object(settings, "PLAN_METRICS_DB_PATH", ""), \
                patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(artifact)):
            with self.assertRaisesRegex(ValueError, "指标库"):
                database.init_db()
        self.assertEqual(sha256_file(artifact), digest)
        self.assertEqual(schema_tables(), original_tables)
        for suffix in ("-wal", "-shm"):
            self.assertFalse(artifact.with_name(artifact.name + suffix).exists())

    async def test_cross_user_and_missing_session_are_denied(self):
        proposal = await self.propose()
        action_id = proposal["action_id"]
        before = self.plans()
        rejected = await self.action(action_id, headers=self.other)
        missing = await self.action("absent", headers=self.other)
        self.assertEqual((rejected.status_code, rejected.json()), (missing.status_code, missing.json()))
        self.assertEqual(rejected.status_code, 404)
        self.assertEqual((await self.action(action_id, headers={})).status_code, 401)
        self.assertEqual(self.plans(), before)
        self.assertEqual(self.receipts(), [])
        self.assertEqual((await self.action(action_id)).status_code, 200)
        self.assertEqual((await self.action(action_id, read=True, headers=self.other)).status_code, 404)
        self.assertEqual((await self.action(action_id, headers=self.other)).status_code, 404)

    async def test_session_replaced_between_request_check_and_transaction(self):
        action_id = (await self.propose())["action_id"]
        before = self.plans()
        validate = self.server.auth_instance.validate_token

        def revoke_after_precheck(token, *, connection=None):
            payload = validate(token, connection=connection)
            if connection is None and payload:
                self.execute("DELETE FROM sessions WHERE user_id=?", (payload["user_id"],))
            return payload

        with patch.object(self.server.auth_instance, "validate_token", side_effect=revoke_after_precheck):
            self.assertEqual((await self.action(action_id)).status_code, 401)
        self.assertEqual(self.plans(), before)
        self.assertEqual(self.receipts(), [])
        self.owner = await self.login("diagnosis-demo")
        self.assertEqual((await self.action(action_id)).status_code, 200)

    async def test_state_change_before_confirm_conflicts(self):
        action_id = (await self.propose())["action_id"]
        changed = await self.client.post("/api/plans/101/toggle", headers=self.owner)
        self.assertEqual(changed.json()["data"]["new_status"], "paused")
        before_confirm = self.plans()
        self.assertEqual((await self.action(action_id)).status_code, 409)
        self.assertEqual(self.plans(), before_confirm)
        self.assertEqual(self.receipts(), [])
        self.assertNotIn("pause_proposal", await self.diagnose())

    async def test_ownership_change_and_reregistration_invalidate_old_action(self):
        action_id = (await self.propose())["action_id"]
        self.execute("UPDATE ad_plans SET user_id=18 WHERE id=101")
        self.assertEqual((await self.action(action_id)).status_code, 404)
        self.assertEqual((await self.action(action_id, headers=self.other)).status_code, 404)
        self.execute("UPDATE ad_plans SET user_id=17 WHERE id=101")
        register_sandbox_target(17, 101)
        self.assertEqual((await self.action(action_id)).status_code, 404)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.plans()[0]["status"], "active")

    async def test_concurrent_confirmations_and_repeat_share_one_receipt(self):
        action_id = (await self.propose())["action_id"]
        self.execute("CREATE TABLE pause_writes (id INTEGER)")
        self.execute("CREATE TRIGGER track_pause AFTER UPDATE OF status ON ad_plans "
                     "BEGIN INSERT INTO pause_writes VALUES (NEW.id); END")
        responses = await asyncio.gather(*(self.action(action_id) for _ in range(6)))
        self.assertEqual([response.status_code for response in responses], [200] * 6)
        receipt = responses[0].json()
        self.assertTrue(all(response.json() == receipt for response in responses))
        self.assertEqual((await self.action(action_id)).json(), receipt)
        self.assertEqual(self.rows("SELECT * FROM pause_writes"), [{"id": 101}])
        self.assertEqual(len(self.receipts()), 1)

    async def test_receipt_insert_failure_rolls_back_pause_then_retry_succeeds(self):
        action_id = (await self.propose())["action_id"]
        before = self.plans()
        self.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON diagnosis_pause_receipts "
                     "BEGIN SELECT RAISE(ABORT, 'synthetic receipt failure'); END")
        self.assertEqual((await self.action(action_id)).status_code, 503)
        self.assertEqual(self.plans(), before)
        self.assertEqual(self.receipts(), [])
        self.assertEqual((await self.action(action_id, read=True)).status_code, 404)
        self.execute("DROP TRIGGER reject_receipt")
        self.assertEqual((await self.action(action_id)).status_code, 200)
        self.assertEqual(len(self.receipts()), 1)

    async def test_get_reads_actual_status_separately_from_immutable_receipt(self):
        action_id = (await self.propose())["action_id"]
        confirmed = (await self.action(action_id)).json()["data"]
        changed = await self.client.post("/api/plans/101/toggle", headers=self.owner)
        self.assertEqual(changed.json()["data"]["new_status"], "active")
        read = (await self.action(action_id, read=True)).json()["data"]
        self.assertEqual(read, {"actual_status": "active", "receipt": confirmed})
        self.assertEqual((await self.action(action_id)).json()["data"], confirmed)
        self.assertEqual(self.plans()[0]["status"], "active")
