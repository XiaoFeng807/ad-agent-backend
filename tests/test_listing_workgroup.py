import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from backend import observability
from backend.agent.agent_pool import AgentPool
from backend.agent.llm_provider import ProviderFactory
from backend.database import database, listing_operations as listing


MESSAGE = "看一下 SKU TASK42-BOTTLE 最近两周卖得怎么样，结合商品资料重写英文五点，确认后再应用。"
READS = ["get_listing_sales", "get_listing_current", "get_listing_facts"]


class ListingModel:
    name = "Mock"

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.requests = []
        self.mode = "direct"

    def create(self, **payload):
        self.requests.append(payload)
        tools = {item["function"]["name"] for item in payload.get("tools") or []}
        messages = payload["messages"]
        calls = []
        content = None
        sku = listing.TEST_SKU
        if messages[0]["content"].startswith("Produce only a JSON object"):
            facts = json.loads(messages[-1]["content"])["facts"]["facts"]
            bullets = [{"fact_id": row["fact_id"], "text": row["options"][1]} for row in facts[:5]]
            if self.mode == "unsupported":
                bullets[0]["text"] = "Guaranteed medical benefit."
            content = json.dumps({"bullets": bullets})
        elif "temporary worker" in messages[0]["content"]:
            if any(row["role"] == "tool" for row in messages) or not tools:
                content = json.dumps({"status": "succeeded", "output": "Returned verified test evidence."})
            else:
                calls = [(tool, {"sku": sku}) for tool in tools]
        elif self.mode == "delegate":
            calls = [("delegate_tasks", {"reason": "Independent evidence", "tasks": [
                {"task_id": "evidence", "objective": "Read test data", "capabilities": READS,
                 "read_scope": [{"tool": tool, "arguments": {"sku": sku}} for tool in READS],
                 "expected_output": "Receipts", "stop_condition": "Read once"},
                {"task_id": "inspect", "objective": "Verify the product evidence", "capabilities": [],
                 "dependencies": ["evidence"], "input_refs": ["evidence"],
                 "expected_output": "Short review", "stop_condition": "Inputs reviewed"},
            ]})]
        else:
            calls = [(tool, {"sku": sku}) for tool in READS if tool in tools
                     and (self.mode != "simple" or tool == "get_listing_sales")]
        result = SimpleNamespace(content=content, tool_calls=[
            {"id": f"call_{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for index, (name, args) in enumerate(calls)])
        return SimpleNamespace(choices=[SimpleNamespace(message=result)])


class ListingWorkgroupTests(unittest.TestCase):
    def setUp(self):
        import fast_server

        Path("logs").mkdir(exist_ok=True)
        root = Path(self.enterContext(TemporaryDirectory(dir="logs"))).resolve()
        self.path = root / "test.sqlite3"
        self.enterContext(patch.object(database.settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(self.path)))
        self.enterContext(patch.object(database.settings, "JWT_SECRET", "task42-test-key" * 5))
        self.enterContext(patch.object(observability, "TRACE_LOG_FILE", root / "traces.jsonl"))
        self.enterContext(patch("backend.memory.conversation_memory.store_conversation"))
        database.init_db()
        database.seed_data()
        self.model = ListingModel()
        with patch.object(ProviderFactory, "create", return_value=self.model):
            pool = AgentPool(min_size=1, max_size=1)
        self.enterContext(patch.object(fast_server, "agent_pool", pool))
        self.client = TestClient(fast_server.app)
        self.addCleanup(self.client.close)
        self.boss = self.login("boss", "admin123")
        self.other = self.login("lisi", "user123")
        self.seed()

    def login(self, username, password):
        response = self.client.post("/api/login", json={"username": username, "password": password})
        return {"Authorization": "Bearer " + response.json()["data"]["token"]}

    def seed(self, variant="original"):
        current_end = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()
        with closing(database.get_db()) as conn, conn:
            owner = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            listing.seed_test_product(conn, owner, variant=variant, current_end=current_end)
            other = conn.execute("SELECT id FROM users WHERE username='lisi'").fetchone()[0]
            listing.seed_test_product(conn, other, shop_id="other-shop", current_end=current_end)

    def run_round(self, mode="direct", message=MESSAGE, *, explicit=True):
        self.model.mode = mode
        group = self.client.post("/api/chat/conversations", headers=self.boss, json={"title": "Test SKU"}).json()["data"]
        base = "/api/chat/conversations/" + group["conversation_id"]
        stream = self.client.post("/api/chat/stream", headers=self.boss, json={
            "conversation_id": group["conversation_id"], "message": message,
            **({"listing_operations": True} if explicit else {}),
        })
        events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        self.assertEqual([row["type"] for row in events], ["thinking", "text", "done"], events)
        history = self.client.get(base + "/history", headers=self.boss).json()["data"]
        entry = next(row for row in history["entries"] if row["kind"] == "assistant")
        self.assertEqual(entry["content"], events[1]["content"])
        run = base + "/runs/" + events[-1]["run_id"]
        evidence = self.client.get(run + "/evidence", headers=self.boss).json()["data"]
        self.assertEqual(evidence["entry_id"], entry["entry_id"])
        actions = entry["listing_actions"]
        return evidence, run + "/listing-actions/" + actions[0]["action_id"] if actions else None

    def rows(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            products = [dict(row) for row in conn.execute("SELECT * FROM listing_test_products ORDER BY shop_id")]
            receipts = conn.execute("SELECT count(*) FROM listing_change_receipts").fetchone()[0]
            return products, receipts

    def test_direct_sales_and_worker_receipt_handoff(self):
        sales, action = self.run_round("simple", "查一下SKU TASK42-BOTTLE的销量。", explicit=False)
        self.assertIsNone(action)
        self.assertEqual(set(sales["artifacts"]), {"get_listing_sales"})
        self.assertEqual(len(self.model.requests), 1)
        evidence, action = self.run_round("delegate")
        self.assertIsNotNone(action)
        self.assertEqual({name for name in evidence["artifacts"]}, set(READS))
        worker_messages = [json.loads(call["messages"][1]["content"]) for call in self.model.requests
                           if "temporary worker" in call["messages"][0]["content"]]
        handoff = next(row["inputs"]["evidence"] for row in worker_messages if row["task"]["task_id"] == "inspect")
        self.assertTrue({row["artifact_id"] for row in evidence["artifacts"].values()}
                        <= {row["artifact_id"] for row in handoff})

    def test_new_tool_batch_rechecks_previous_sales(self):
        message = ("查看 SKU TASK42-BOTTLE 的销量，只有两窗数据完整才重写英文五点。")
        completions = self.model.chat.completions
        create = completions.create
        changed = False

        def mutate_during_second_batch(**payload):
            nonlocal changed
            if len(self.model.requests) == 1:
                with closing(database.get_db()) as conn, conn:
                    conn.execute("UPDATE listing_test_sales SET version=version+1 WHERE shop_id=? AND sku=?",
                                 (listing.TEST_SHOP, listing.TEST_SKU))
                changed = True
            return create(**payload)

        with patch.object(completions, "create", mutate_during_second_batch):
            evidence, action = self.run_round(message=message)
        self.assertTrue(changed)
        self.assertNotIn("metrics", evidence)
        self.assertNotIn("get_listing_sales", evidence["artifacts"])
        self.assertIsNone(evidence["proposal"])
        self.assertIsNone(action)
        self.assertFalse(any(request["messages"][0]["content"].startswith("Produce only a JSON object")
                             for request in self.model.requests))

    def test_facts_change_missing_and_unsupported_claims(self):
        original, _ = self.run_round()
        self.seed("changed")
        changed, _ = self.run_round()
        self.assertIn("500 mL", original["proposal"]["after_bullets"][0])
        self.assertIn("750 mL", changed["proposal"]["after_bullets"][0])
        rejected, action = self.run_round("unsupported")
        self.assertIsNone(action)
        self.assertIsNone(rejected["proposal"])
        self.seed("missing")
        missing, action = self.run_round()
        self.assertIsNone(action)
        self.assertIn("insufficient_listing_facts", missing["gaps"])
        self.assertEqual(self.rows()[1], 0)

    def test_concurrent_approval_and_independent_get(self):
        from backend.database.diagnosis_pauses import pause_enabled

        self.assertTrue(pause_enabled())
        initial, receipts = self.rows()
        evidence, action = self.run_round()
        self.assertEqual(self.rows(), (initial, receipts))
        self.assertEqual(self.client.get(action, headers=self.other).status_code, 404)
        self.assertEqual(self.client.post(action + "/confirm", headers=self.other).status_code, 404)
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(lambda _: self.client.post(action + "/confirm", headers=self.boss), range(4)))
        self.assertEqual([response.status_code for response in responses], [200] * 4)
        actual = self.client.get(action, headers=self.boss).json()["data"]
        self.assertEqual({response.json()["data"]["receipt_id"] for response in responses}, {actual["receipt_id"]})
        self.assertEqual(actual["listing"]["bullets"], evidence["proposal"]["after_bullets"])
        after, receipts = self.rows()
        self.assertEqual(receipts, 1)
        with closing(database.get_db(read_only=True)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM chat_group_pause_proposals").fetchone()[0], 0)
        self.assertEqual(after[0], initial[0])
        self.assertEqual({key for key in after[1] if after[1][key] != initial[1][key]}, {"bullets", "version"})

    def test_receipt_failure_rolls_back_and_stale_version_conflicts(self):
        _, action = self.run_round()
        before = self.rows()
        with closing(database.get_db()) as conn, conn:
            conn.execute("CREATE TRIGGER fail_listing_receipt BEFORE INSERT ON listing_change_receipts "
                         "BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        self.assertEqual(self.client.post(action + "/confirm", headers=self.boss).status_code, 503)
        self.assertEqual(self.rows(), before)
        self.assertIsNone(self.client.get(action, headers=self.boss).json()["data"]["receipt"])
        with closing(database.get_db()) as conn, conn:
            conn.execute("DROP TRIGGER fail_listing_receipt")
            conn.execute("UPDATE listing_test_products SET version=2 WHERE sku=? AND shop_id=?",
                         (listing.TEST_SKU, listing.TEST_SHOP))
        self.assertEqual(self.client.post(action + "/confirm", headers=self.boss).status_code, 409)
        self.assertEqual(self.rows()[1], 0)


if __name__ == "__main__":
    unittest.main()
