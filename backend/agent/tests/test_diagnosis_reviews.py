"""Exercise local review proposals through the real FastAPI routes and SQLite."""

import asyncio
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from httpx import ASGITransport, AsyncClient

from backend.agent.tests.test_ad_diagnosis_eval import SEVEN_DAYS, THREE_DAYS, StatefulDiagnosisModel
from backend.agent.tests.test_home_sse_tracing import HomeSSEFixture
from backend.auth import auth
from backend.database import database


RULE = "ctr-review-v1#low-ctr"
PROPOSALS = "/api/diagnosis/review-proposals"
REVIEWS = "/api/diagnosis/reviews"
DEVELOPMENT_KEY = "ad_agent_secret_key_2026"
PASSWORD = "offline-review-password"


class DiagnosisReviewTests(HomeSSEFixture):
    model_class = StatefulDiagnosisModel

    async def asyncSetUp(self):
        await super().asyncSetUp()
        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM sessions")
            conn.executemany("INSERT INTO users (id, username, password) VALUES (?, ?, ?)", [
                (17, "owner", database.hash_password(PASSWORD)),
                (18, "other", database.hash_password(PASSWORD)),
            ])
            conn.execute("INSERT INTO ad_plans (id, plan_name, user_id) VALUES (101, 'demo plan', 17)")
        self.citation_id = "source-A"
        self.client = await self.enterAsyncContext(AsyncClient(
            transport=ASGITransport(app=self.server.app), base_url="http://test",
        ))
        self.token = await self.login("owner")
        self.other_token = await self.login("other")

    async def login(self, username):
        response = await self.client.post("/api/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 200)
        return response.json()["data"]["token"]

    def tool_overrides(self):
        return {
            "get_plan_detail": lambda **kwargs: {"plan_name": "demo plan"},
            "get_daily_report_by_date": lambda **kwargs: {"reports": [{"impressions": 200, "clicks": 10}]},
            "search_knowledge": lambda query: json.dumps({"evidence": [{
                "rule_id": "ctr-review#low-ctr", "version": "v1", "chunk_id": self.citation_id, "title": "复核规则",
            }]}),
        }

    async def diagnose(self, task_id=None, *, plan_id="101"):
        params = ({"task_id": task_id, **THREE_DAYS} if task_id
                  else {"plan_id": plan_id, "rule_id": RULE, **SEVEN_DAYS})
        response = await self.client.post(
            "/api/chat/stream", json={"diagnosis": params}, headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]

    def headers(self, user_id=17, *, token=None):
        if token is None:
            token = self.token if user_id == 17 else self.other_token
        return {"Authorization": f"Bearer {token}"}

    async def confirm(self, action_id, user_id=17, **kwargs):
        return await self.client.post(f"{PROPOSALS}/{action_id}/confirm", headers=self.headers(user_id), **kwargs)

    async def read(self, review_id, user_id=17):
        return await self.client.get(f"{REVIEWS}/{review_id}", headers=self.headers(user_id))

    def review_count(self):
        with closing(self.connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM diagnosis_reviews").fetchone()[0]

    def committed_sessions(self):
        with closing(self.connect()) as conn:
            return dict(conn.execute("SELECT user_id, token FROM sessions").fetchall())

    def row_counts(self):
        tables = ("sessions", "diagnosis_tasks", "diagnosis_turns", "diagnosis_review_proposals", "diagnosis_reviews")
        with closing(self.connect()) as conn:
            return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables)

    async def review_targets(self):
        first = (await self.diagnose())[-1]["review_proposal"]
        confirmed = await self.confirm(first["action_id"])
        self.assertEqual(confirmed.status_code, 200)
        pending = (await self.diagnose(first["task_id"]))[-1]["review_proposal"]
        return pending["action_id"], confirmed.json()["data"]["review_id"]

    async def assert_auth_rejected(self, token, targets):
        action_id, review_id = targets
        before = self.row_counts()
        headers = self.headers(token=token)
        diagnosis = await self.client.post("/api/chat/stream", headers=headers, json={
            "diagnosis": {"plan_id": "101", "rule_id": RULE, **SEVEN_DAYS},
        })
        self.assertEqual(diagnosis.status_code, 401)
        self.assertEqual(self.row_counts(), before)
        confirm = await self.client.post(f"{PROPOSALS}/{action_id}/confirm", headers=headers)
        self.assertEqual(confirm.status_code, 401)
        self.assertEqual(self.row_counts(), before)
        read = await self.client.get(f"{REVIEWS}/{review_id}", headers=headers)
        self.assertEqual(read.status_code, 401)
        self.assertEqual(self.row_counts(), before)

    async def test_login_commits_session_and_completes_review(self):
        self.assertEqual(self.committed_sessions(), {17: self.token, 18: self.other_token})
        self.assertEqual(self.row_counts(), (2, 0, 0, 0, 0))
        proposal = (await self.diagnose())[-1]["review_proposal"]
        response = await self.confirm(proposal["action_id"])
        self.assertEqual(response.status_code, 200)
        receipt = response.json()["data"]
        response = await self.read(receipt["review_id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], receipt)
        self.assertEqual(self.row_counts(), (2, 1, 1, 1, 1))

    async def test_development_signature_is_rejected_with_matching_session(self):
        targets = await self.review_targets()
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        token = jwt.encode({"user_id": 17, "exp": expiry}, DEVELOPMENT_KEY, algorithm="HS256")
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE sessions SET token=? WHERE user_id=17", (token,))
        await self.assert_auth_rejected(token, targets)

    async def test_unconfigured_and_development_keys_disable_login_and_verification(self):
        targets = await self.review_targets()
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        token = jwt.encode({"user_id": 17, "exp": expiry}, DEVELOPMENT_KEY, algorithm="HS256")
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE sessions SET token=? WHERE user_id=17", (token,))
        for key in ("", DEVELOPMENT_KEY):
            with self.subTest(configured=bool(key)), patch.object(auth.settings, "JWT_SECRET", key):
                sessions = self.committed_sessions()
                response = await self.client.post("/api/login", json={"username": "owner", "password": PASSWORD})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(self.committed_sessions(), sessions)
                await self.assert_auth_rejected(token, targets)

    async def test_missing_or_uncommitted_session_is_rejected(self):
        targets = await self.review_targets()
        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE user_id=17")
        await self.assert_auth_rejected(self.token, targets)
        with closing(self.connect()) as conn:
            conn.execute("INSERT INTO sessions (user_id, token) VALUES (?, ?)", (17, self.token))
            await self.assert_auth_rejected(self.token, targets)

    async def test_expired_or_invalid_claims_are_rejected_with_matching_session(self):
        targets = await self.review_targets()
        now = datetime.now(timezone.utc)
        for claims in (
            {"user_id": 17, "exp": now - timedelta(seconds=1)},
            {"user_id": 17},
            {"exp": now + timedelta(hours=1)},
            {"user_id": "17", "exp": now + timedelta(hours=1)},
            {"user_id": 2**63, "exp": now + timedelta(hours=1)},
        ):
            with self.subTest(claims=claims):
                token = jwt.encode(claims, self.signing_key, algorithm="HS256")
                with closing(self.connect()) as conn, conn:
                    conn.execute("UPDATE sessions SET token=? WHERE user_id=17", (token,))
                await self.assert_auth_rejected(token, targets)

    async def test_same_second_relogin_replaces_token(self):
        targets = await self.review_targets()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with patch.object(auth, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            old = await self.login("owner")
            self.token = await self.login("owner")
        self.assertNotEqual(self.token, old)
        self.assertEqual(self.committed_sessions(), {17: self.token, 18: self.other_token})
        await self.assert_auth_rejected(old, targets)
        self.assertEqual((await self.confirm(targets[0])).status_code, 200)
        self.assertEqual((await self.read(targets[1])).status_code, 200)

    async def test_concurrent_confirmation_keeps_first_round_and_is_owner_only(self):
        events = await self.diagnose()
        self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])
        a = events[-1]["review_proposal"]
        self.assertEqual(a["source_trace_id"], events[0]["trace_id"])
        self.assertEqual(a["citations"], events[1]["citations"])
        self.assertEqual(a["evidence_gaps"], events[1]["evidence_gaps"])
        self.assertTrue(a["evidence_gaps"])

        self.citation_id = "source-B"
        b = (await self.diagnose(a["task_id"]))[-1]["review_proposal"]
        self.assertNotEqual(a["action_id"], b["action_id"])
        self.assertNotEqual(a["citations"], b["citations"])
        self.assertNotEqual(a["source_trace_id"], b["source_trace_id"])
        self.assertEqual({key: b[key] for key in THREE_DAYS}, THREE_DAYS)
        self.assertEqual(self.review_count(), 0)

        action_id = a["action_id"]
        foreign = await self.confirm(action_id, user_id=18, json={"user_id": 17})
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json(), (await self.confirm("unknown")).json())
        self.assertEqual(self.review_count(), 0)
        responses = await asyncio.gather(*(self.confirm(action_id) for _ in range(6)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        receipt = responses[0].json()["data"]
        self.assertTrue(all(response.json()["data"] == receipt for response in responses))
        self.assertEqual(self.review_count(), 1)
        self.assertEqual({key: receipt[key] for key in a}, a)
        self.assertEqual({key: receipt[key] for key in SEVEN_DAYS}, SEVEN_DAYS)
        review_id = receipt["review_id"]
        self.assertEqual((await self.read(review_id)).json()["data"], receipt)
        self.assertEqual((await self.confirm(action_id)).json()["data"], receipt)
        self.assertEqual((await self.confirm(action_id, user_id=18)).status_code, 404)
        self.assertEqual((await self.read(review_id, user_id=18)).status_code, 404)
        self.assertEqual((await self.client.get(f"{REVIEWS}/{review_id}")).status_code, 401)
        self.assertEqual((await self.client.post(f"{PROPOSALS}/{action_id}/confirm")).status_code, 401)

    async def test_confirmation_checks_current_plan_ownership(self):
        proposal = (await self.diagnose())[-1]["review_proposal"]
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE ad_plans SET user_id=18 WHERE id=101")
        action_id = proposal["action_id"]
        self.assertEqual((await self.confirm(action_id)).status_code, 404)
        self.assertEqual((await self.confirm(action_id, user_id=18)).status_code, 404)
        self.assertNotIn("review_proposal", (await self.diagnose())[-1])
        self.assertEqual(self.review_count(), 0)

    async def test_insert_failure_rolls_back_and_can_retry(self):
        proposal = (await self.diagnose())[-1]["review_proposal"]
        with closing(self.connect()) as conn, conn:
            conn.execute("""CREATE TRIGGER fail_review AFTER INSERT ON diagnosis_reviews
                BEGIN SELECT RAISE(FAIL, 'test failure after row write'); END""")
        self.assertEqual((await self.confirm(proposal["action_id"])).status_code, 503)
        self.assertEqual(self.review_count(), 0)
        with closing(self.connect()) as conn, conn:
            conn.execute("DROP TRIGGER fail_review")
        response = await self.confirm(proposal["action_id"])
        self.assertEqual(response.status_code, 200)
        receipt = response.json()["data"]
        self.assertEqual((await self.read(receipt["review_id"])).json()["data"], receipt)
        self.assertEqual(self.review_count(), 1)

    async def test_proposal_failure_rolls_back_reply_commit(self):
        with closing(self.connect()) as conn, conn:
            conn.execute("""CREATE TRIGGER fail_proposal AFTER INSERT ON diagnosis_review_proposals
                BEGIN SELECT RAISE(FAIL, 'test failure after proposal write'); END""")
        events = await self.diagnose()
        self.assertEqual(events[-1]["type"], "error")
        self.assertNotIn("review_proposal", events[-1])
        with closing(self.connect()) as conn:
            self.assertIsNone(conn.execute("SELECT reply FROM diagnosis_turns").fetchone()[0])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM diagnosis_review_proposals").fetchone()[0], 0)
        self.assertEqual(self.review_count(), 0)
