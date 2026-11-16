import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.agent.listing_run import CURRENT, FACTS, SALES
from backend.config.config import settings
from backend.database import database, listing_operations as listing
from tests import test_task42_t2_listing as fixture


QUESTION = "看看这款水杯最近两周卖得怎么样，如果下滑明显，结合商品资料帮我改一版英文五点，先给我确认。"
PARAPHRASE = "帮我对比这款水壶过去14天的出货量，若销量下降至少20%，按商品资料拟写英文五条描述，先让我审核。"
ORIGINAL = "SKU TASK42-BOTTLE 在 2026-10-30 到 2026-11-12 的前后两周分别卖了多少件？"
SECOND = "TASK42-SECOND"
READERS = {SALES: "read_sales", CURRENT: "read_listing", FACTS: "read_facts"}


class NaturalModel(fixture.ScopeModel):
    def create(self, **payload):
        tools = {item["function"]["name"] for item in payload.get("tools") or []}
        worker = "temporary worker" in payload["messages"][0]["content"]
        if self.mode != "conditional_delegate" or worker:
            return super().create(**payload)
        if "delegate_tasks" not in tools:
            self.mode = "direct"
            try:
                return super().create(**payload)
            finally:
                self.mode = "conditional_delegate"
        self.requests.append(payload)
        task = {"task_id": "product", "objective": "Read the scoped product", "capabilities": [CURRENT, FACTS],
                "expected_output": "Verified receipts", "stop_condition": "Both reads complete",
                "read_scope": [{"tool": name, "arguments": {"sku": self.target}} for name in (CURRENT, FACTS)]}
        call = {"id": "delegate-product", "function": {"name": "delegate_tasks",
                "arguments": json.dumps({"reason": "Parallel product evidence", "tasks": [task]})}}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call], content=None))])


class NaturalListingTests(unittest.TestCase):
    login = fixture.Task42T2ListingTests.login
    rows = fixture.Task42T2ListingTests.rows
    seed = fixture.Task42T2ListingTests.seed
    round = fixture.Task42T2ListingTests.round
    write_report = fixture.Task42T2ListingTests.write_report

    def setUp(self):
        fixture.Task42T2ListingTests.setUp(self)
        import fast_server

        self.model = NaturalModel()
        with patch.object(fixture.fixture.ProviderFactory, "create", return_value=self.model):
            pool = fixture.fixture.AgentPool(min_size=1, max_size=1)
        self.enterContext(patch.object(fast_server, "agent_pool", pool))
        self.enterContext(patch.object(settings, "LISTING_SIGNIFICANT_DECLINE_PCT", 20))
        self.readers = {tool: self.enterContext(patch.object(listing, name, wraps=getattr(listing, name)))
                        for tool, name in READERS.items()}

    def check_sales(self, result):
        evidence = result["evidence"]
        sales = evidence["metrics"]
        sql = Path("scripts/task42_t1_listing.sql").read_text(encoding="utf-8")
        args = dict(zip(("baseline_start", "baseline_end", "current_start", "current_end"), evidence["windows"]))
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(sql, {**args, **evidence["target"]})]
        result["independent_sql"] = rows
        for key in ("baseline", "current"):
            window = [row for row in rows if row["window"] == key]
            self.assertEqual(sales[key]["days"], [{"date": row["day"], "units": row["units"]} for row in window])
            self.assertEqual((sales[key]["units"], sales[key]["covered_days"]), (window[0]["total"], window[0]["covered"]))
        self.assertEqual((sales["units_delta"], sales["units_change_pct"]), (rows[0]["delta"], rows[0]["relative_pct"]))
        return sales

    def check_pending(self, result, sku, version):
        evidence = result["evidence"]
        proposal = evidence["proposal"]
        self.assertEqual((proposal["target"]["sku"], evidence["listing"]["version"]), (sku, version))
        self.assertEqual(proposal["original_version"], version)
        self.assertEqual(len(proposal["bullets"]), 5)
        facts = {fact["fact_id"]: fact for fact in evidence["facts"]["facts"]}
        self.assertEqual(len({bullet["fact_id"] for bullet in proposal["bullets"]}), 5)
        for bullet in proposal["bullets"]:
            self.assertIn(bullet["text"], facts[bullet["fact_id"]]["options"])
        result["pending_readback"] = self.client.get(result["action_url"], headers=self.boss).json()["data"]
        self.assertEqual(result["pending_readback"]["state"], "pending")
        self.assertEqual(result["pending_readback"]["proposal"], proposal)
        self.assertIsNone(result["pending_readback"]["receipt"])
        self.assertEqual(result["pending_readback"]["listing"]["version"], version)

    def test_original_sales_question(self):
        with closing(database.get_db()) as conn, conn:
            owner = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            listing.seed_test_product(conn, owner)
        result = self.round("original", ORIGINAL, explicit=False)
        metrics = self.check_sales(result)
        self.assertEqual((metrics["baseline"]["units"], metrics["current"]["units"]), (70, 49))
        self.assertIsNone(result["action_url"])
        self.assertEqual(set(result["evidence"]["artifacts"]), {SALES})
        self.assertTrue(all(not self.readers[name].called for name in (CURRENT, FACTS)))

    def test_ambiguity_and_product_switch(self):
        with closing(database.get_db()) as conn, conn:
            owner = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            other = conn.execute("SELECT id FROM users WHERE username='lisi'").fetchone()[0]
            listing.seed_test_product(conn, other, sku="TASK42-HIDDEN", shop_id="other-shop")
            current_end = conn.execute("SELECT max(key) FROM listing_test_sales, json_each(series) "
                                       "WHERE sku=? AND shop_id=?", (listing.TEST_SKU, listing.TEST_SHOP)).fetchone()[0]
            listing.seed_test_product(conn, owner, sku=SECOND, variant="changed", current_end=current_end)
            conn.execute("UPDATE listing_test_products SET version=4 WHERE sku=?", (SECOND,))
        before = self.rows()
        result = self.round("ambiguous", QUESTION, explicit=False)
        group = result["conversation_id"]
        self.assertEqual(result["fixed_model_calls"], 0)
        self.assertTrue(all(not reader.called for reader in self.readers.values()))
        self.assertEqual([row["sku"] for row in result["evidence"]["candidates"]], [listing.TEST_SKU, SECOND])
        self.assertNotIn("TASK42-HIDDEN", result["assistant"]["content"])
        self.model.target = SECOND
        selected = self.round("choose", "第2款", explicit=False, group=group)
        self.check_pending(selected, SECOND, 4)
        self.assertTrue(all(call.args[4] == SECOND for reader in self.readers.values() for call in reader.call_args_list))
        self.assertIn("750 mL", selected["evidence"]["proposal"]["after_bullets"][0])
        repeated = self.round("context", PARAPHRASE, explicit=False, group=group)
        self.check_pending(repeated, SECOND, 4)
        self.model.target = listing.TEST_SKU
        explicit = self.round("explicit", QUESTION.replace("这款水杯", f"SKU {listing.TEST_SKU}"),
                              explicit=False, group=group)
        self.check_pending(explicit, listing.TEST_SKU, 1)
        self.assertIn("500 mL", explicit["evidence"]["proposal"]["after_bullets"][0])
        self.assertEqual(self.rows(), before)

    def test_missing_date_and_confirmed_zero(self):
        with closing(database.get_db()) as conn, conn:
            series = json.loads(conn.execute("SELECT series FROM listing_test_sales WHERE shop_id=?",
                                            (listing.TEST_SHOP,)).fetchone()[0])
            date = sorted(series)[9]
            del series[date]
            conn.execute("UPDATE listing_test_sales SET series=? WHERE shop_id=?", (json.dumps(series), listing.TEST_SHOP))
        gap = self.round("gap", QUESTION, explicit=False)
        sales = self.check_sales(gap)
        self.assertEqual((sales["current"]["covered_days"], sales["current"]["units"], sales["units_change_pct"]), (6, None, None))
        self.assertIn(date, gap["assistant"]["content"])
        self.assertIn("6/7", gap["assistant"]["content"])
        self.assertEqual(gap["evidence"]["decline_condition"]["status"], "sales_unknown")
        self.assertEqual(set(gap["evidence"]["artifacts"]), {SALES})
        self.assertTrue(all(not self.readers[name].called for name in (CURRENT, FACTS)))
        self.assertEqual(gap["fixed_model_calls"], 1)
        self.assertIsNone(gap["action_url"])
        with closing(database.get_db()) as conn, conn:
            series[date] = 0
            series.update(dict.fromkeys(sorted(series)[7:], 0))
            conn.execute("UPDATE listing_test_sales SET series=? WHERE shop_id=?", (json.dumps(series), listing.TEST_SHOP))
        zero = self.round("zero", QUESTION, explicit=False)
        sales = self.check_sales(zero)
        self.assertEqual((sales["current"]["covered_days"], sales["current"]["units"], sales["units_change_pct"]), (7, 0, -100))
        self.check_pending(zero, listing.TEST_SKU, 1)

    def test_missing_facts_retains_sales(self):
        self.seed("missing")
        result = self.round("missing_facts", QUESTION, explicit=False)
        self.assertEqual(self.check_sales(result)["units_change_pct"], -30)
        self.assertIn("还缺 1 项", result["assistant"]["content"])
        self.assertEqual(result["evidence"]["decline_condition"]["status"], "met")
        self.assertIsNone(result["action_url"])
        self.assertEqual(result["fixed_model_calls"], 2)

    def test_threshold_missing_and_strict_boundary(self):
        with patch.object(settings, "LISTING_SIGNIFICANT_DECLINE_PCT", None):
            unknown = self.round("unknown_threshold", QUESTION, explicit=False)
            self.assertEqual(unknown["evidence"]["decline_condition"]["status"], "threshold_unknown")
            self.assertIsNone(unknown["action_url"])
            self.assertTrue(all(not self.readers[name].called for name in (CURRENT, FACTS)))
            accepted = self.round("threshold_followup", "阈值20%", explicit=False, group=unknown["conversation_id"])
            self.check_pending(accepted, listing.TEST_SKU, 1)
        strict = self.round("strict", QUESTION.replace("下滑明显", "下滑超过30%"), explicit=False)
        self.assertEqual(strict["evidence"]["decline_condition"]["status"], "not_met")
        self.assertEqual(set(strict["evidence"]["artifacts"]), {SALES})
        self.assertIsNone(strict["action_url"])

    def test_worker_first_input_uses_verified_sales(self):
        result = self.round("delegate", QUESTION, explicit=False, mode="conditional_delegate")
        self.check_pending(result, listing.TEST_SKU, 1)
        evidence = result["evidence"]
        worker = next(row for row in result["model_inputs"] if "temporary worker" in row["messages"][0]["content"])
        handoff = json.loads(worker["messages"][1]["content"])
        result["first_worker_input"] = handoff
        self.assertEqual(handoff["scope"]["windows"], evidence["windows"])
        self.assertEqual(handoff["scope"]["target"], evidence["target"])
        receipt, = handoff["direct_inputs"]
        self.assertEqual(receipt["artifact_id"], evidence["artifacts"][SALES]["artifact_id"])
        self.assertEqual(receipt["result"], evidence["metrics"])
        self.assertEqual(receipt["arguments"]["sku"], listing.TEST_SKU)

    def test_altered_tool_arguments_stop_before_business_read(self):
        original = self.model.create
        for mode, scope_patch in (
            ("direct", {"current_end": "2026-01-01"}),
            ("direct", {"shop_id": "other-shop"}),
            ("conditional_delegate", {"marketplace_id": "CA"}),
        ):
            with self.subTest(mode=mode, scope_patch=scope_patch):
                for reader in self.readers.values():
                    reader.reset_mock()

                def alter(**payload):
                    response = original(**payload)
                    for call in response.choices[0].message.tool_calls:
                        function = call["function"]
                        if (mode == "direct" and function["name"] == SALES) or (
                            mode == "conditional_delegate" and function["name"] == CURRENT
                        ):
                            function["arguments"] = json.dumps({**json.loads(function["arguments"]), **scope_patch})
                    return response

                with patch.object(self.model.chat.completions, "create", alter):
                    result = self.round("altered_scope", QUESTION, explicit=False, mode=mode)
                self.assertFalse(self.readers[CURRENT].called)
                self.assertIsNone(result["action_url"])
                if mode == "direct":
                    self.assertTrue(all(not reader.called for reader in self.readers.values()))
                else:
                    self.assertTrue(self.readers[SALES].called)
                    self.assertNotIn(CURRENT, result["evidence"]["artifacts"])
                    self.assertTrue(any(step["stage"] == "tool_rejected" and step["action"] == CURRENT
                                        for step in result["trace"]["steps"]))
