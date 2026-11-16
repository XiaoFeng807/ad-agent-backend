"""Exercise plan and account ownership through the real homepage routes and SSE."""

import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import httpx
from unittest.mock import patch

from backend.agent.tests.test_home_sse_tracing import FixedModel, HomeSSEFixture, tool_call
from backend.database.database import hash_password
from backend.tools import tools


ACCOUNTS = [(401, "账户甲", 17), (402, "账户乙", 18), (403, "无属主账户", None)]
PLANS = [(501, "计划甲", 17, 401), (502, "错绑计划乙", 18, 401),
         (503, "计划乙", 18, 402), (504, "无属主计划", None, 401)]
USERS = {17: "scope-first", 18: "scope-second"}
PASSWORD = "scope-demo-password"


def seed_scope(conn):
    with conn:
        conn.executemany("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                         [(uid, name, hash_password(PASSWORD)) for uid, name in USERS.items()])
        conn.executemany("INSERT INTO ad_accounts (id, account_name, user_id, platform) VALUES (?, ?, ?, 'Google Ads')",
                         ACCOUNTS)
        conn.executemany("INSERT INTO ad_plans (id, plan_name, user_id, account_id) VALUES (?, ?, ?, ?)", PLANS)


def business_rows(conn):
    return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("ad_plans", "ad_accounts")}


class AccountModel(FixedModel):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def create(self, **payload):
        self.inputs.append(deepcopy(payload))
        names = {tool["function"]["name"] for tool in payload.get("tools") or []}
        for msg in payload["messages"]:
            if msg["role"] == "tool":
                self.receipts[msg["tool_call_id"]] = json.loads(msg["content"])
        if "query_data" in names:
            if "data" not in self.receipts:
                return self.response(None, [tool_call("data", name="query_data", query="查账户与投放计划")])
            return self.response("已核对投放账户", [])
        if "get_account_detail" in names:
            return self.response(None, [
                tool_call("owned", name="get_account_detail", account_id=401),
                tool_call("foreign", name="get_account_detail", account_id=402, user_id=18),
                tool_call("missing", name="get_account_detail", account_id=999999),
            ])
        if "owned" in self.receipts:
            return self.response(json.dumps(self.receipts["owned"], ensure_ascii=False), [])
        return self.response('{"steps": []}', [])


class HomePlanScopeTests(HomeSSEFixture):
    model_class = AccountModel

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db = self.enterContext(closing(self.connect()))
        seed_scope(self.db)
        self.enterContext(patch.object(tools, "get_db", self.connect))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app), base_url="http://test",
        ))
        if archive := getattr(self, "archive_model_inputs", None):
            self.addCleanup(archive, self.id(), self.model.inputs)

    async def call(self, method, path, *, token=None, **kwargs):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def login(self, user_id):
        result = (await self.call("POST", "/api/login", json={
            "username": USERS[user_id], "password": PASSWORD,
        })).json()
        self.assertEqual(result["data"]["user"]["user_id"], user_id)
        return result["data"]["token"]

    def version(self):
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    def record_rows(self, before, after, **results):
        print("TASK16_DB " + json.dumps({"case": self.id(), "before": before, "after": after, **results},
                                       ensure_ascii=False), flush=True)

    async def assert_rejected(self, token):
        before, version = business_rows(self.db), self.version()
        status = {}
        for method, path, payload in (
            ("GET", "/api/plans", None), ("GET", "/api/accounts", None),
            ("POST", "/api/plans", {"plan_name": "denied", "user_id": 18}),
            ("POST", "/api/plans/501/toggle", None), ("DELETE", "/api/plans/501", None),
            ("POST", "/api/chat/stream", {"message": "查账户", "user_id": 18}),
        ):
            response = await self.call(method, path, token=token, json=payload)
            self.assertEqual(response.status_code, 401, (method, path, response.text))
            status[f"{method} {path}"] = response.status_code
        after = business_rows(self.db)
        self.assertEqual(before, after)
        self.assertEqual(self.version(), version)
        self.assertEqual(self.model.inputs, [])
        self.record_rows(before, after, status=status, data_version_before=version, data_version_after=self.version())

    async def test_missing_session_is_rejected(self):
        await self.assert_rejected(None)

    async def test_expired_session_is_rejected_even_if_committed(self):
        expired = self.sign_token(user_id=17, exp=datetime.now(timezone.utc) - timedelta(seconds=1))
        with self.db:
            self.db.execute("UPDATE sessions SET token=? WHERE user_id=17", (expired,))
        await self.assert_rejected(expired)

    async def test_relogin_revokes_previous_session(self):
        current = await self.login(17)
        self.assertNotEqual(self.token, current)
        await self.assert_rejected(self.token)
        self.assertEqual((await self.call("GET", "/api/plans", token=current)).status_code, 200)

    async def test_each_user_lists_creates_toggles_and_deletes_own_plan(self):
        before = business_rows(self.db)
        operations = []
        for user_id, plan_ids, account_id in ((17, [501], 401), (18, [502, 503], 402)):
            token = await self.login(user_id)
            plans = (await self.call("GET", "/api/plans", token=token)).json()["data"]
            accounts = (await self.call("GET", "/api/accounts", token=token)).json()["data"]
            self.assertEqual([(row["id"], row["user_id"]) for row in plans], [(pid, user_id) for pid in plan_ids])
            self.assertEqual([(row["id"], row["user_id"]) for row in accounts], [(account_id, user_id)])
            name = f"created-by-{user_id}"
            created = await self.call("POST", "/api/plans", token=token, json={"plan_name": name})
            self.assertEqual((created.status_code, created.json()["code"]), (200, 200))
            row = self.db.execute("SELECT * FROM ad_plans WHERE plan_name=?", (name,)).fetchone()
            self.assertEqual((row["user_id"], row["status"]), (user_id, "active"))
            plan_id = row["id"]
            plan_path = f"/api/plans/{plan_id}"
            statuses = [row["status"]]
            for status in ("paused", "active"):
                response = await self.call("POST", f"{plan_path}/toggle", token=token)
                self.assertEqual((response.status_code, response.json()["data"]["new_status"]), (200, status))
                self.assertEqual(self.db.execute("SELECT status FROM ad_plans WHERE id=?", (plan_id,)).fetchone()[0],
                                 status)
                statuses.append(status)
            self.assertEqual((await self.call("DELETE", plan_path, token=token)).status_code, 200)
            self.assertEqual((await self.call("DELETE", plan_path, token=token)).status_code, 404)
            self.assertIsNone(self.db.execute("SELECT 1 FROM ad_plans WHERE id=?", (plan_id,)).fetchone())
            operations.append({"owner": user_id, "plan_ids": plan_ids, "account_id": account_id,
                               "created_id": plan_id, "statuses": statuses, "deleted": True})
        after = business_rows(self.db)
        self.assertEqual(before, after)
        self.record_rows(before, after, operations=operations)

    async def test_forged_owner_is_rejected_without_business_changes(self):
        before, version = business_rows(self.db), self.version()
        response = await self.call("POST", "/api/plans?user_id=18", token=self.token, json={
            "plan_name": "forged-owner", "user_id": 18, "account_id": 402,
        })
        self.assertEqual((response.status_code, response.json()), (400, {"detail": "计划属主由当前会话确定"}))
        for path, row_id in (("plans", 501), ("accounts", 401)):
            response = await self.call("GET", f"/api/{path}?user_id=18", token=self.token)
            self.assertEqual([(row["id"], row["user_id"]) for row in response.json()["data"]], [(row_id, 17)])
        after = business_rows(self.db)
        self.assertEqual(before, after)
        self.assertEqual(self.version(), version)
        self.record_rows(before, after, status={"POST /api/plans": 400}, data_version_before=version,
                         data_version_after=self.version())

    async def test_invalid_create_and_out_of_range_ids_leave_owner_list_usable(self):
        before, version = business_rows(self.db), self.version()
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app, raise_app_exceptions=False),
            base_url="http://test",
        ))
        invalid_bodies = {
            "malformed_json": b"{",
            "null_body": b"null",
            "array_body": b"[]",
            "numeric_name": b'{"plan_name":123}',
            "invalid_unicode_name": b'{"plan_name":"\\ud800"}',
            "object_platform": b'{"plan_name":"bad platform","platform":{}}',
            "invalid_budget": b'{"plan_name":"bad budget","daily_budget":"invalid"}',
            "boolean_budget": b'{"plan_name":"bad budget","daily_budget":true}',
            "infinite_budget": b'{"plan_name":"infinite budget","daily_budget":"inf"}',
            "nan_budget": b'{"plan_name":"nan budget","daily_budget":"nan"}',
        }
        anonymous = await self.call("POST", "/api/plans", content=invalid_bodies["malformed_json"])
        self.assertEqual(anonymous.status_code, 401)
        create_status = {}
        for name, content in invalid_bodies.items():
            response = await self.call("POST", "/api/plans", token=self.token, content=content)
            create_status[name] = response.status_code
        id_status = {}
        for plan_id in (2**63, -(2**63) - 1):
            for method, suffix in (("POST", "/toggle"), ("DELETE", "")):
                response = await self.call(method, f"/api/plans/{plan_id}{suffix}", token=self.token)
                id_status[f"{method} {plan_id}"] = response.status_code
        listed = await self.call("GET", "/api/plans", token=self.token)
        after = business_rows(self.db)
        self.assertEqual(create_status, dict.fromkeys(invalid_bodies, 400))
        self.assertEqual(id_status, dict.fromkeys(id_status, 422))
        self.assertEqual(before, after)
        self.assertEqual(self.version(), version)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row["id"] for row in listed.json()["data"]], [501])
        self.record_rows(before, after, create_status=create_status, invalid_id_status=id_status,
                         owner_list_status=listed.status_code, data_version_before=version,
                         data_version_after=self.version())

    async def test_foreign_and_missing_writes_are_identical_and_leave_zero_changes(self):
        other = await self.login(18)
        before, version = business_rows(self.db), self.version()
        status = {}
        for token, owner, foreign_id in ((self.token, 18, 502), (other, 17, 501)):
            for method, suffix in (("POST", "/toggle"), ("DELETE", "")):
                responses = [await self.call(method, f"/api/plans/{pid}{suffix}?user_id={owner}", token=token,
                                             json={"user_id": owner}) for pid in (foreign_id, 999999)]
                self.assertEqual([r.status_code for r in responses], [404, 404])
                self.assertEqual(responses[0].content, responses[1].content)
                self.assertEqual(responses[0].json(), {"detail": "计划不存在"})
                status[f"{method} owner={owner}"] = [r.status_code for r in responses]
        after = business_rows(self.db)
        self.assertEqual(before, after)
        self.assertEqual(self.version(), version)
        self.record_rows(before, after, status=status, data_version_before=version, data_version_after=self.version())

    async def test_concurrent_owned_toggles_apply_to_current_database_status(self):
        before = business_rows(self.db)
        responses = await asyncio.gather(*(self.call("POST", "/api/plans/501/toggle", token=self.token)
                                           for _ in range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        statuses = [r.json()["data"]["new_status"] for r in responses]
        self.assertCountEqual(statuses, ["paused", "active"])
        after = business_rows(self.db)
        self.assertEqual(before, after)
        self.record_rows(before, after, statuses=sorted(statuses))

    async def test_sse_receipts_and_complete_model_inputs_filter_cross_owner_links(self):
        before = business_rows(self.db)
        await self.request(body={"message": "查看广告账户和关联投放计划", "user_id": 18})
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        self.assertEqual(self.assistant_replies(), ["已核对投放账户"])
        received = self.model.receipts
        self.assertEqual(received["owned"]["user_id"], 17)
        self.assertEqual([(plan["id"], plan["plan_name"]) for plan in received["owned"]["plans"]], [(501, "计划甲")])
        self.assertEqual(received["foreign"], received["missing"])
        self.assertEqual(received["foreign"], {"success": False, "message": "未找到"})
        self.assertEqual(json.loads(received["data"]["result"]), received["owned"])
        for input_payload in self.model.inputs:
            self.assertTrue(input_payload["messages"])
        full_input = json.dumps(self.model.inputs, ensure_ascii=False, default=vars)
        for private_name in ("错绑计划乙", "计划乙", "账户乙", "无属主计划", "无属主账户"):
            self.assertNotIn(private_name, full_input)
        self.assertIn("计划甲", full_input)
        steps = self.assert_trace("completed", expected_calls=None)
        for call_id in ("owned", "foreign", "missing"):
            self.assertEqual([(step["stage"], step["action"]) for step in steps if step.get("tool_call_id") == call_id],
                             [("tool_call", "get_account_detail"), ("tool_result", "get_account_detail")])
        after = business_rows(self.db)
        self.assertEqual(before, after)
        self.record_rows(before, after, tool_receipts=received, model_inputs=len(self.model.inputs))
