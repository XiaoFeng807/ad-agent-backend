"""Read diagnosis history through the real FastAPI app and isolated SQLite."""

from contextlib import closing
import importlib
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from backend import observability
from backend.agent.diagnosis import DIAGNOSIS_REQUEST
from backend.agent.llm_provider import ProviderFactory
from backend.config.config import settings
from backend.database import database
from backend.database.diagnosis_pauses import PAUSE_RULE_ID, register_sandbox_target
from backend.database.diagnosis_tasks import accept_diagnosis, save_diagnosis_reply


TASKS = "/api/diagnosis/tasks"
FIRST = {"start_date": "2026-10-01", "end_date": "2026-10-01"}
SECOND = {"start_date": "2026-10-02", "end_date": "2026-10-02"}
THIRD = {"start_date": "2026-10-03", "end_date": "2026-10-03"}
CITATION = {"chunk_id": "ctr-v1", "title": "CTR 复核规则", "version": "v1", "rule_id": PAUSE_RULE_ID}
EVIDENCE = {
    "citations": [CITATION], "evidence_gaps": [],
    "metrics": {"baseline": {"clicks": 100}, "current": {"clicks": 80}},
    "calculation": {"relative_decline_pct": 20},
    "rule_evaluation": {"rule_id": PAUSE_RULE_ID, "chunk_id": "ctr-v1", "matched": True},
}


class DiagnosisHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        self.sandbox = folder / "sandbox.sqlite3"
        self.enterContext(patch.object(database, "DB_PATH", folder / "app.sqlite3"))
        self.enterContext(patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(self.sandbox)))
        self.enterContext(patch.object(settings, "JWT_SECRET", token_urlsafe(32)))
        self.trace_path = folder / "traces.jsonl"
        self.enterContext(patch.object(observability, "TRACE_LOG_FILE", self.trace_path))
        self.enterContext(patch.object(observability, "_traces", {}))
        database.init_db()
        for user_id in (17, 18):
            self.write("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                       (user_id, f"user-{user_id}", database.hash_password("demo-password")))
            self.write("INSERT INTO ad_plans (id, plan_name, user_id) VALUES (?, ?, ?)",
                       (user_id, f"plan-{user_id}", user_id))
            register_sandbox_target(user_id, user_id)
        with patch.object(ProviderFactory, "create", return_value=None):
            app = importlib.import_module("fast_server").app
        self.client = await self.enterAsyncContext(AsyncClient(transport=ASGITransport(app=app), base_url="http://test"))
        self.owner = await self.login(17)
        self.other = await self.login(18)

    def write(self, sql, args=()):
        with closing(database.get_db()) as conn, conn:
            conn.execute(sql, args)

    async def login(self, user_id):
        response = await self.client.post("/api/login", json={
            "username": f"user-{user_id}", "password": "demo-password",
        })
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["data"]["token"]}

    def accept(self, *, user_id=17, task_id=None, plan_id=None, current=SECOND):
        selection = ({"task_id": task_id} if task_id else
                     {"plan_id": plan_id or user_id, "rule_id": PAUSE_RULE_ID})
        params = DIAGNOSIS_REQUEST.validate_python({**selection, "baseline": FIRST, "current": current})
        return accept_diagnosis(user_id, params, "保存的提问", trace_id="original-trace")

    def commit(self, run, *, user_id=17, evidence=EVIDENCE):
        return save_diagnosis_reply(user_id, run, "保存的回复", "original-trace", evidence)

    async def get(self, url=TASKS, *, headers=None, **kwargs):
        response = await self.client.get(url, headers=self.owner if headers is None else headers, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    async def detail(self, run):
        return await self.get(f"{TASKS}/{run.snapshot.task_id}")

    async def test_list_detail_require_current_session_account_and_owner(self):
        own = self.accept()
        other = self.accept(user_id=18)
        url = f"{TASKS}/{own.snapshot.task_id}"
        self.assertEqual([row["task_id"] for row in (await self.get())["tasks"]], [own.snapshot.task_id])
        self.assertEqual([row["task_id"] for row in (await self.get(headers=self.other))["tasks"]],
                         [other.snapshot.task_id])
        unknown = await self.client.get(f"{TASKS}/unknown", headers=self.other)
        foreign = await self.client.get(url, headers=self.other)
        self.assertEqual((unknown.status_code, foreign.status_code), (404, 404))
        self.assertEqual(unknown.json(), foreign.json())
        for path in (TASKS, url):
            self.assertEqual((await self.client.get(path)).status_code, 401)
        previous, self.owner = self.owner, await self.login(17)
        self.assertEqual((await self.client.get(url, headers=previous)).status_code, 401)
        self.assertEqual((await self.client.get(url, headers=self.owner)).headers["cache-control"], "no-store")
        self.write("DELETE FROM users WHERE id=17")
        for path in (TASKS, url):
            self.assertEqual((await self.client.get(path, headers=self.owner)).status_code, 401)

    async def test_keyset_cursor_keeps_creation_order_through_new_tasks_and_continuation(self):
        initial = [self.accept().snapshot.task_id for _ in range(4)]
        self.accept(user_id=18)
        first = await self.get(params={"limit": 2})
        self.assertEqual([row["task_id"] for row in first["tasks"]], initial[2:][::-1])
        newer = self.accept().snapshot.task_id
        self.accept(task_id=initial[0], current=THIRD)
        second = await self.get(params={"limit": 2, "cursor": first["next_cursor"]})
        self.assertEqual([row["task_id"] for row in second["tasks"]], initial[:2][::-1])
        self.assertEqual(second["tasks"][-1]["current"], THIRD)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual((await self.get(params={"limit": 1}))["tasks"][0]["task_id"], newer)
        for query in ({"limit": 0}, {"limit": 51}, {"cursor": "bad"}, {"cursor": "9223372036854775808:key"}):
            self.assertEqual((await self.client.get(TASKS, headers=self.owner, params=query)).status_code, 422)

    async def test_each_committed_turn_recovers_its_own_snapshot_even_without_a_proposal(self):
        first = self.accept()
        proposal = self.commit(first)["review_proposal"]
        self.write("DELETE FROM ad_plans WHERE id=17")
        second = self.accept(task_id=first.snapshot.task_id, current=THIRD)
        self.assertEqual(self.commit(second), {})
        task = await self.detail(first)
        self.assertEqual(task["current"], THIRD)
        original, following = task["turns"]
        self.assertEqual(original["snapshot"], {k: v for k, v in proposal.items() if k not in ("action_id", "turn_id")})
        self.assertEqual(original["snapshot"]["current"], SECOND)
        self.assertEqual(following["snapshot"], {
            **second.snapshot.model_dump(), "source_trace_id": "original-trace", **EVIDENCE,
        })
        self.assertEqual(following["reply"], "保存的回复")
        self.assertIsNone(following["review"])

    async def test_legacy_migration_preserves_reply_and_falls_back_to_original_proposal(self):
        first = self.accept()
        self.commit(first)
        legacy = self.accept(task_id=first.snapshot.task_id, current=THIRD)
        self.write("UPDATE diagnosis_turns SET reply='旧回复' WHERE id=?", (legacy.turn_id,))
        self.write("ALTER TABLE diagnosis_turns DROP COLUMN snapshot")
        database.init_db()
        database.init_db()
        original, missing = (await self.detail(first))["turns"]
        self.assertEqual(original["snapshot"]["current"], SECOND)
        self.assertEqual(original["snapshot"]["citations"], [CITATION])
        self.assertEqual(missing, {
            "turn_id": legacy.turn_id, "message": "保存的提问", "reply": "旧回复",
            "status": "committed", "snapshot": None, "review": None, "pause": None,
        })

    async def test_uncommitted_round_restores_dates_and_trace_without_actions(self):
        run = self.accept()
        self.commit(run)
        # Even if orphaned proposals exist, a missing reply prohibits offering them.
        pending = self.accept(task_id=run.snapshot.task_id, current=THIRD)
        self.write("UPDATE diagnosis_review_proposals SET turn_id=?", (pending.turn_id,))
        turn = (await self.detail(run))["turns"][1]
        self.assertEqual(turn["status"], "uncommitted")
        self.assertEqual(turn["snapshot"], {**pending.snapshot.model_dump(), "source_trace_id": "original-trace"})
        self.assertIsNone(turn["reply"])
        self.assertIsNone(turn["review"])
        self.assertIsNone(turn["pause"])

    async def test_pending_availability_rechecks_sandbox_status_registration_and_ownership(self):
        run = self.accept()
        self.commit(run)

        async def turn():
            return (await self.detail(run))["turns"][0]

        self.assertEqual((await turn())["pause"]["state"], "pending")
        with patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", ""), patch.object(database, "DB_PATH", self.sandbox):
            self.assertEqual((await turn())["pause"]["reason"], "sandbox_disabled")
        self.write("UPDATE ad_plans SET status='paused' WHERE id=17")
        self.assertEqual((await turn())["pause"]["reason"], "status_changed")
        self.assertEqual((await turn())["review"]["state"], "pending")
        self.write("UPDATE ad_plans SET status='active' WHERE id=17")
        self.write("DELETE FROM diagnosis_sandbox_targets WHERE plan_id=17")
        register_sandbox_target(17, 17)
        self.assertEqual((await turn())["pause"]["reason"], "target_unavailable")
        self.write("UPDATE ad_plans SET user_id=18 WHERE id=17")
        self.assertEqual((await turn())["review"]["state"], "unavailable")
        self.assertEqual((await turn())["pause"]["state"], "unavailable")

    async def test_history_is_read_only_and_receipts_use_independent_gets(self):
        run = self.accept()
        proposals = self.commit(run)
        review_url = "/api/diagnosis/review-proposals/" + proposals["review_proposal"]["action_id"]
        pause_url = "/api/diagnosis/pause-actions/" + proposals["pause_proposal"]["action_id"]
        review = (await self.client.post(review_url + "/confirm", headers=self.owner)).json()["data"]
        pause = (await self.client.post(pause_url + "/confirm", headers=self.owner)).json()["data"]

        def dump():
            with closing(database.get_db(read_only=True)) as conn:
                return list(conn.iterdump())

        before, trace = dump(), self.trace_path.read_bytes()
        with patch.object(ProviderFactory, "create", side_effect=AssertionError("history must not load a model")) as model:
            for _ in range(2):
                await self.get()
                turn = (await self.detail(run))["turns"][0]
                self.assertEqual(turn["review"], {
                    "state": "approved", "review_id": review["review_id"], "action_id": review["action_id"],
                })
                self.assertEqual(turn["pause"]["state"], "approved")
                self.assertNotIn("receipt_id", turn["pause"])
            model.assert_not_called()
        self.assertEqual((dump(), self.trace_path.read_bytes()), (before, trace))
        self.assertEqual(await self.get("/api/diagnosis/reviews/" + review["review_id"]), review)
        self.assertEqual(await self.get(pause_url), {"receipt": pause, "actual_status": "paused"})
        self.assertEqual(dump(), before)

        async def assert_approved(reason, receipt_status):
            action = (await self.detail(run))["turns"][0]["pause"]
            self.assertEqual((action["state"], action["reason"]), ("approved", reason))
            self.assertEqual((await self.client.get(pause_url, headers=self.owner)).status_code, receipt_status)

        # A later plan status change does not prevent reading the committed receipt.
        self.write("UPDATE ad_plans SET status='active' WHERE id=17")
        await assert_approved(None, 200)
        self.assertEqual((await self.get(pause_url))["actual_status"], "active")
        with patch.object(settings, "DIAGNOSIS_SANDBOX_DB_PATH", ""), patch.object(database, "DB_PATH", self.sandbox):
            await assert_approved("sandbox_disabled", 404)

        with closing(database.get_db(read_only=True)) as conn:
            registration = conn.execute(
                "SELECT registration_id FROM diagnosis_sandbox_targets WHERE plan_id=17"
            ).fetchone()["registration_id"]
        self.write("DELETE FROM diagnosis_sandbox_targets WHERE plan_id=17")
        await assert_approved("target_unavailable", 404)
        self.write("INSERT INTO diagnosis_sandbox_targets (plan_id, user_id, registration_id) VALUES (17, 17, ?)",
                   (registration,))
        await assert_approved(None, 200)
        self.write("UPDATE ad_plans SET user_id=18 WHERE id=17")
        await assert_approved("target_unavailable", 404)
