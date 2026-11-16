"""Check homepage reads with real FastAPI routes, sessions and temporary SQLite."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
import jwt

from backend.agent.llm_provider import ProviderFactory
from backend.agent.tests.test_home_sse_tracing import FixedModel
from backend.auth.auth import settings
from backend.database import database


USERS = {1: ("business-boss", "boss"), 2: ("business-admin", "admin"),
         17: ("business-first", "user"), 18: ("business-second", "user")}
PASSWORD = "business-demo-password"
ROUTES = ("/api/daily_reports", "/api/timeline", "/api/materials", "/api/decisions", "/api/users")


def seed_business(conn):
    with conn:
        conn.executemany("INSERT INTO users (id, username, role, password) VALUES (?, ?, ?, ?)", [
            (uid, name, role, database.hash_password(PASSWORD)) for uid, (name, role) in USERS.items()
        ])
        conn.executemany(
            "INSERT INTO ad_plans (id, plan_name, user_id, status, start_date) VALUES (?, ?, ?, ?, ?)",
            [(500, "boss-plan", 1, "active", "2026-11-07"),
             (501, "first-plan", 17, "active", "2026-11-03"),
             (502, "second-plan", 18, "active", "2026-11-06"),
             (503, "orphan-plan", None, "active", "2026-11-07"),
             (504, "first-paused-plan", 17, "paused", "2026-11-05")],
        )
        conn.executemany(
            "INSERT INTO daily_reports (user_id, report_date, cost, impressions, clicks, sales) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(17, "2026-11-05", 100, 1000, 20, 50), (17, "2026-11-05", 20, 200, 5, 10),
             (17, "2026-11-01", 3, 30, 2, 4), (18, "2026-11-06", 18000, 500, 50, 100),
             (18, "2026-11-05", 60, 600, 12, 30), (None, "2026-11-05", 99, 300, 11, 30),
             (None, "2026-11-07", 999, 999, 99, 99), (1, "2026-11-07", 101, 0, 0, 0)],
        )
        conn.executemany("INSERT INTO materials (name, plan_id, user_id) VALUES (?, ?, ?)", [
            ("first-linked", 501, 17), ("first-zero", 0, 17), ("first-null", None, 17),
            ("first-foreign-link", 502, 17), ("first-orphan-link", 503, 17),
            ("first-missing-link", 9999, 17), ("second-linked", 502, 18), ("orphan-material", 501, None),
        ])
        conn.executemany("INSERT INTO decision_logs (user_id, suggestion, status) VALUES (?, ?, ?)", [
            (17, "first-pending", "pending"), (17, "first-executed", "executed"),
            (17, "first-verified", "verified"),
            *((18, f"second-decision-{i:02}", "pending") for i in range(1, 23)),
            (None, "orphan-decision", "verified"),
        ])


class HomeBusinessScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(database, "DB_PATH", directory / "business.sqlite3"))
        self.secret = token_urlsafe(32)
        self.enterContext(patch.object(settings, "JWT_SECRET", self.secret))
        with patch.object(ProviderFactory, "create", side_effect=lambda *a, **kw: FixedModel()):
            server = importlib.import_module("fast_server")
        database.init_db()
        self.db = self.enterContext(closing(database.get_db()))
        seed_business(self.db)
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test",
        ))

    async def login(self, user_id):
        response = await self.client.post("/api/login", json={
            "username": USERS[user_id][0], "password": PASSWORD,
        })
        self.assertEqual(response.json()["data"]["user"]["user_id"], user_id)
        return response.json()["data"]["token"]

    async def get(self, path, token=None, **query):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await self.client.get(path, headers=headers, params=query)

    async def read(self, path, user_id, **query):
        response = await self.get(path, await self.login(user_id), user_id=18 if user_id == 17 else 17, **query)
        self.record(path=path, user_id=user_id, status=response.status_code, body=response.json())
        self.assertEqual((response.status_code, response.json()["code"]), (200, 200))
        return response.json()["data"]

    def record(self, **result):
        print("TASK17_API " + json.dumps({"case": self.id().rsplit(".", 1)[-1], **result},
                                        ensure_ascii=False), flush=True)

    async def test_daily_reports_scope_before_aggregation_order_and_limit(self):
        first = await self.read("/api/daily_reports", 17, days=1)
        second = await self.read("/api/daily_reports", 18, days=1)
        self.assertEqual(first, [{"report_date": "2026-11-05", "cost": 120.0, "impressions": 1200,
                                  "clicks": 25, "sales": 60.0}])
        self.assertEqual(second, [{"report_date": "2026-11-06", "cost": 18000.0, "impressions": 500,
                                   "clicks": 50, "sales": 100.0}])
        self.assertEqual(await self.read("/api/daily_reports", 2), [])
        token = await self.login(17)
        counts = {}
        for days in (0, -1, -(2**63), 2**63 - 1):
            response = await self.get("/api/daily_reports", token, days=days)
            self.assertEqual(response.status_code, 200)
            counts[days] = len(response.json()["data"])
        self.assertEqual(counts, {0: 0, -1: 2, -(2**63): 2, 2**63 - 1: 2})
        invalid = {}
        for days in (-(2**63) - 1, 2**63):
            response = await self.get("/api/daily_reports", token, days=days)
            invalid[days] = response.status_code
        self.record(compatible_day_counts=counts, invalid_day_statuses=invalid)
        self.assertEqual(invalid, {-(2**63) - 1: 422, 2**63: 422})

    async def test_timeline_contains_only_owned_plans_in_activity_order(self):
        for uid, expected in ((17, [501, 504]), (18, [502]), (2, [])):
            data = await self.read("/api/timeline", uid)
            self.assertTrue(data["success"])
            self.assertEqual([row["id"] for row in data["plans"]], expected)

    async def test_materials_keep_owned_rows_and_mask_unowned_plan_links(self):
        first = await self.read("/api/materials", 17)
        second = await self.read("/api/materials", 18)
        self.assertEqual([(r["name"], r["user_id"], r["plan_id"], r["plan_name"]) for r in first], [
            ("first-missing-link", 17, None, None), ("first-orphan-link", 17, None, None),
            ("first-foreign-link", 17, None, None), ("first-null", 17, None, None),
            ("first-zero", 17, None, None), ("first-linked", 17, 501, "first-plan"),
        ])
        self.assertEqual([(r["name"], r["user_id"], r["plan_id"], r["plan_name"]) for r in second], [
            ("second-linked", 18, 502, "second-plan"),
        ])

    async def test_decisions_scope_before_limit_and_keep_all_own_statuses(self):
        first = await self.read("/api/decisions", 17)
        second = await self.read("/api/decisions", 18)
        self.assertEqual([(r["user_id"], r["suggestion"], r["status"]) for r in first], [
            (17, "first-verified", "verified"), (17, "first-executed", "executed"),
            (17, "first-pending", "pending"),
        ])
        self.assertEqual([(r["user_id"], r["suggestion"]) for r in second],
                         [(18, f"second-decision-{i:02}") for i in range(22, 2, -1)])

    async def test_directory_role_matrix_and_exact_projection(self):
        expected = [{"id": uid, "username": name, "role": role} for uid, (name, role) in USERS.items()]
        for uid, status in ((1, 200), (2, 200), (17, 403), (18, 403)):
            response = await self.get("/api/users", await self.login(uid), user_id=1, role="boss")
            self.record(user_id=uid, status=response.status_code, body=response.json())
            self.assertEqual(response.status_code, status)
            if status == 200:
                self.assertEqual(response.json(), {"code": 200, "data": expected})
            else:
                self.assertNotIn("data", response.json())

    async def test_directory_uses_current_role_and_rejects_removed_account(self):
        admin, first = await self.login(2), await self.login(17)
        with self.db:
            self.db.execute("UPDATE users SET role='user' WHERE id=2")
            self.db.execute("UPDATE users SET role='admin' WHERE id=17")
        demoted = await self.get("/api/users", admin)
        promoted = await self.get("/api/users", first)
        self.record(demoted=demoted.status_code, promoted=promoted.status_code)
        self.assertEqual((demoted.status_code, promoted.status_code), (403, 200))
        self.assertIn({"id": 17, "username": "business-first", "role": "admin"}, promoted.json()["data"])
        with self.db:
            self.db.execute("DELETE FROM users WHERE id=17")
        removed = await self.get("/api/users", first)
        self.record(removed=removed.status_code)
        self.assertEqual(removed.status_code, 401)

    async def test_logs_use_current_role_and_require_a_committed_session(self):
        with self.db:
            self.db.executemany("INSERT INTO operation_logs (user_id, action) VALUES (?, 'business-test')",
                                [(uid,) for uid in (1, 2, 17, 18, None)])
        tokens = {uid: await self.login(uid) for uid in USERS}

        async def log_owners(uid):
            response = await self.get("/api/logs", tokens[uid], user_id=1, role="boss")
            self.assertEqual(response.status_code, 200)
            return [row["user_id"] for row in response.json()["data"]]

        before = {uid: await log_owners(uid) for uid in USERS}
        with self.db:
            self.db.execute("UPDATE users SET role='user' WHERE id=2")
            self.db.execute("UPDATE users SET role='admin' WHERE id=17")
        after = {uid: await log_owners(uid) for uid in USERS}
        with self.db:
            self.db.execute("DELETE FROM users WHERE id=17")
        uncommitted = jwt.encode({"user_id": 1, "role": "boss", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                                 self.secret, algorithm="HS256")
        rejected = {name: (await self.get("/api/logs", token)).status_code
                    for name, token in (("deleted", tokens[17]), ("uncommitted", uncommitted), ("missing", None))}
        self.record(before=before, after=after, rejected=rejected)
        self.assertEqual(before, {1: [None, 18, 17, 2, 1], 2: [18, 17, 2], 17: [17], 18: [18]})
        self.assertEqual(after, {1: [None, 18, 17, 2, 1], 2: [2], 17: [18, 17, 2], 18: [18]})
        self.assertEqual(rejected, {"deleted": 401, "uncommitted": 401, "missing": 401})

    async def test_missing_uncommitted_expired_and_replaced_sessions_get_401(self):
        replaced = await self.login(17)
        await self.login(17)
        now = datetime.now(timezone.utc)
        uncommitted = jwt.encode({"user_id": 18, "exp": now + timedelta(minutes=5)}, self.secret, algorithm="HS256")
        expired = jwt.encode({"user_id": 18, "exp": now - timedelta(seconds=1)}, self.secret, algorithm="HS256")
        with self.db:
            self.db.execute("INSERT INTO sessions (user_id, token) VALUES (18, ?)", (expired,))
        version = self.db.execute("PRAGMA data_version").fetchone()[0]
        for label, token in (("missing", None), ("uncommitted", uncommitted),
                             ("expired", expired), ("replaced", replaced)):
            statuses = {path: (await self.get(path, token)).status_code for path in ROUTES}
            self.record(session=label, statuses=statuses)
            self.assertEqual(statuses, dict.fromkeys(ROUTES, 401))
        self.assertEqual(self.db.execute("PRAGMA data_version").fetchone()[0], version)
