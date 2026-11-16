import json
import os
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.agent.agent_pool import AgentPool
from backend.agent.llm_provider import ProviderFactory
from backend.database import chat_groups, database, orders
from tests.test_olist_sales import OlistSalesTests as SalesFixtures, PARAMETERS, PRODUCT, csv_window


class OlistModel:
    name = "Mock"

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.requests = []
        self.arguments = {"product_id": PRODUCT}

    def create(self, **payload):
        self.requests.append(payload)
        reply = SimpleNamespace(content=None, tool_calls=[{
            "id": "olist-test-call", "type": "function", "function": {
                "name": "get_olist_product_sales", "arguments": json.dumps(self.arguments),
            },
        }])
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)])


class OlistWorkgroupTests(unittest.TestCase):
    def setUp(self):
        SalesFixtures.setUp(self)
        from fast_server import app

        self.addCleanup(self.client.close)
        self.model = OlistModel()
        with patch.object(ProviderFactory, "create", return_value=self.model):
            pool = AgentPool(min_size=1, max_size=1)
        self.enterContext(patch("fast_server.agent_pool", pool))
        self.enterContext(patch("backend.memory.conversation_memory.store_conversation"))
        self.assertIs(self.client.app, app)

    login = SalesFixtures.login

    def send(self, headers, message):
        group = self.client.post("/api/chat/conversations", headers=headers, json={"title": "Olist 核对"})
        conversation = group.json()["data"]["conversation_id"]
        base = f"/api/chat/conversations/{conversation}"
        response = self.client.post("/api/chat/stream", headers=headers,
                                    json={"conversation_id": conversation, "message": message})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])
        trace = events[-1]["trace_id"]
        return events[1]["content"], base, f"{base}/runs/{events[-1]['run_id']}/evidence", trace

    def test_olist_receipt_matches_source_and_is_read_only(self):
        def business_snapshot():
            with closing(database.get_db(read_only=True)) as conn:
                return tuple(row for row in conn.iterdump()
                             if not row.startswith(('INSERT INTO "chat_', 'INSERT INTO "sqlite_sequence"')))

        before = business_snapshot()
        msg = f"查 Olist 商品 {PRODUCT} 从2018-04-11到2018-05-08的销量"
        answer, base, evidence_url, trace = self.send(self.owner, msg)
        expected = self.client.get(evidence_url, headers=self.owner).json()["data"]
        self.assertEqual(expected["kind"], orders.OLIST_SALES_EVIDENCE)
        for name in ("baseline", "current"):
            actual = expected["metrics"][name]
            independent = csv_window(self.source, PRODUCT, PARAMETERS[f"{name}_start"], PARAMETERS[f"{name}_end"],
                                     ["delivered", "shipped"])
            self.assertEqual(actual, independent)
            self.assertIn(f"{independent['totals']['units']} 件、{independent['totals']['orders']} 单", answer)
        history = self.client.get(base + "/history", headers=self.owner).json()["data"]
        entry, = [entry for entry in history["entries"] if entry["kind"] == "assistant"]
        self.assertEqual(entry["content"], answer)
        self.assertEqual(entry["evidence_kind"], orders.OLIST_SALES_EVIDENCE)
        self.assertNotIn("pause_actions", entry)
        self.assertNotIn("listing_actions", entry)
        self.assertEqual(self.client.get(evidence_url, headers=self.other).status_code, 404)
        self.assertEqual(before, business_snapshot())
        payload, = self.model.requests
        self.assertEqual([item["function"]["name"] for item in payload["tools"]], ["get_olist_product_sales"])
        if output := os.environ.get("AD_OLIST_GROUP_REPORT"):
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"trace_id": trace, "evidence": expected, "reply": answer,
                                        "model_input": payload}, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_unknown_wrong_scope_and_incomplete_dates(self):
        base = "查 Olist 商品 {} 从2018-04-11到2018-05-08的销量"
        for headers, message, expected in (
                (self.other, base.format(PRODUCT), "有权查看"),
                (self.owner, base.format("0" * 32), "有权查看"),
                (self.owner, f"Olist 商品 {PRODUCT} 最近两周销量", "历史数据"),
                (self.owner, base.format(PRODUCT) + "，店铺 " + "0" * 32, "有权查看"),
                (self.owner, base.format(PRODUCT) + "，店铺 other-shop", "有权查看"),
                (self.owner, base.format(PRODUCT) + "，只统计已取消", "支持已送达"),
                (self.owner, base.format(PRODUCT) + "，排除 shipped", "支持已送达"),
        ):
            with self.subTest(message=message, expected=expected):
                answer, _, evidence, _ = self.send(headers, message)
                self.assertIn(expected, answer)
                self.assertEqual(self.client.get(evidence, headers=headers).status_code, 404)
        self.assertEqual(self.model.requests, [])

    def test_wrong_model_arguments_are_blocked_and_revoked_shop_cannot_read_evidence(self):
        msg = f"查 Olist 商品 {PRODUCT} 从2018-04-11到2018-05-08的销量"
        original = orders.olist_sales
        with patch.object(orders, "olist_sales", wraps=original) as sales:
            for arguments in ({"product_id": "0" * 32}, {"product_id": PRODUCT, "user_id": 17}):
                self.model.arguments = arguments
                answer, _, evidence_url, _ = self.send(self.owner, msg)
                self.assertIn("没有取得可核验", answer)
                self.assertEqual(self.client.get(evidence_url, headers=self.owner).status_code, 404)
            sales.assert_not_called()
        self.model.arguments = {"product_id": PRODUCT}
        answer, group, evidence_url, _ = self.send(self.owner, msg)
        self.assertEqual(self.client.get(evidence_url, headers=self.owner).status_code, 200)
        with closing(database.get_db()) as conn, conn:
            conn.execute("DELETE FROM order_shop_access WHERE source=?", (orders.OLIST,))
        self.assertEqual(self.client.get(evidence_url, headers=self.owner).status_code, 404)
        history = self.client.get(group + "/history", headers=self.owner).json()["data"]["entries"]
        user, assistant = history
        self.assertNotEqual(assistant["content"], answer)
        self.assertFalse(assistant.get("has_evidence"))
        with closing(database.get_db(read_only=True)) as conn:
            owner = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            self.assertIsNone(chat_groups.committed_turn(conn, owner, group.split("/")[-1], user["id"], assistant["id"]))
