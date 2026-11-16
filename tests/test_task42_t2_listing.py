"""Fixed-response tests of the Listing workgroup's scope, evidence and writes."""

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from zoneinfo import ZoneInfo

from backend.database import database, listing_operations as listing
from tests import test_listing_workgroup as fixture


EDIT = "查看 SKU TASK42-BOTTLE 销量，结合商品资料重写英文五点。"
FIXTURE_STATISTICS_DATE = datetime.fromisoformat(listing.TEST_CURRENT[1]).replace(
    tzinfo=ZoneInfo(listing.SALES_TIMEZONE),
) + timedelta(days=1)


class ScopeModel(fixture.ListingModel):
    def __init__(self):
        super().__init__()
        self.target = listing.TEST_SKU
        self.draft_override = None

    def create(self, **payload):
        if self.target == listing.TEST_SKU and self.draft_override is None:
            return super().create(**payload)
        response = super().create(**payload)
        message = response.choices[0].message
        for call in message.tool_calls:
            args = json.loads(call["function"]["arguments"])
            if "sku" in args:
                args["sku"] = self.target
                call["function"]["arguments"] = json.dumps(args)
        if self.draft_override and payload["messages"][0]["content"].startswith("Produce only a JSON object"):
            message.content = self.draft_override(json.loads(message.content))
        return response


class Task42T2ListingTests(unittest.TestCase):
    login = fixture.ListingWorkgroupTests.login
    seed = fixture.ListingWorkgroupTests.seed
    rows = fixture.ListingWorkgroupTests.rows

    def setUp(self):
        fixture.ListingWorkgroupTests.setUp(self)
        import fast_server
        self.model = ScopeModel()
        with patch.object(fixture.ProviderFactory, "create", return_value=self.model):
            pool = fixture.AgentPool(min_size=1, max_size=1)
        self.enterContext(patch.object(fast_server, "agent_pool", pool))
        self.report = []
        self.addCleanup(self.write_report)
        self.started = perf_counter()

    def write_report(self):
        if raw := os.environ.get("TASK42_T2_LISTING_EVIDENCE"):
            output = Path(raw)
            output.mkdir(parents=True, exist_ok=True)
            (output / (self._testMethodName + ".json")).write_text(
                json.dumps({"seconds": round(perf_counter() - self.started, 4), "observed": self.report},
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def round(self, name, text=EDIT, *, mode="direct", explicit=True, group=None):
        self.model.requests.clear()
        self.model.mode = mode
        if group is None:
            group_response = self.client.post("/api/chat/conversations", headers=self.boss, json={"title": name})
            self.assertEqual(group_response.status_code, 201)
            group = group_response.json()["data"]["conversation_id"]
        body = {"message": text, "conversation_id": group}
        if explicit:
            body["listing_operations"] = True
        stream = self.client.post("/api/chat/stream", headers=self.boss, json=body)
        self.assertEqual(stream.status_code, 200)
        events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        base = f"/api/chat/conversations/{group}"
        history = self.client.get(base + "/history", headers=self.boss)
        self.assertEqual(history.status_code, 200)
        entry = next(row for row in reversed(history.json()["data"]["entries"]) if row["kind"] == "assistant")
        run = base + f"/runs/{events[-1]['run_id']}"
        evidence = self.client.get(run + "/evidence", headers=self.boss)
        actions = entry.get("listing_actions", [])
        result = {"name": name, "request": text, "events": events, "assistant": entry, "conversation_id": group,
                  "evidence_status": evidence.status_code, "evidence": evidence.json().get("data"),
                  "fixed_model_calls": len(self.model.requests), "model_inputs": list(self.model.requests),
                  "action_url": run + "/listing-actions/" + actions[0]["action_id"] if actions else None}
        trace_id = events[-1].get("trace_id")
        trace_path = self.path.parent / "traces.jsonl"
        if trace_id and trace_path.exists():
            result["trace"] = next((json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
                                    if json.loads(line)["trace_id"] == trace_id), None)
        self.report.append(result)
        self.assertEqual([row["type"] for row in events], ["thinking", "text", "done"], name)
        self.assertEqual(entry["content"], events[1]["content"], name)
        return result

    def test_read_existing_bullets_never_creates_edit_proposal(self):
        cases = (
            "查看 SKU TASK42-BOTTLE 原 Listing 的英文五点。",
            "只看 SKU TASK42-BOTTLE 当前英文五点，不要修改。",
            "Show the current bullet points for SKU TASK42-BOTTLE.",
            "查看 SKU TASK42-BOTTLE 的五條商品描述。",
            "不要重写 SKU TASK42-BOTTLE 的英文五点，只显示当前内容。",
            "把 SKU TASK42-BOTTLE 的当前英文五点给我看看，先不修改。",
        )
        before = self.rows()
        for text in cases:
            with self.subTest(text=text):
                result = self.round("read_existing_bullets", text, explicit=False)
                self.assertEqual(result["evidence_status"], 200)
                self.assertEqual(result["evidence"]["listing"]["version"], 1)
                self.assertEqual(set(result["evidence"]["artifacts"]), {"get_listing_current"})
                self.assertEqual(result["fixed_model_calls"], 1)
                self.assertIsNone(result["action_url"])
                self.assertEqual(self.rows(), before)
        sales_only = self.round("sales_only",
                                "只查询 SKU TASK42-BOTTLE 的销量，不要改英文五点，也不要生成草案。",
                                explicit=False)
        self.assertEqual(set(sales_only["evidence"]["artifacts"]), {"get_listing_sales"})
        self.assertEqual(sales_only["fixed_model_calls"], 1)
        self.assertIsNone(sales_only["action_url"])
        natural_sales = self.round("sales_without_keyword",
                                   "SKU TASK42-BOTTLE 在 2026-10-30 到 2026-11-12 的前后两周分别卖了多少件？",
                                   explicit=False)
        self.assertEqual(set(natural_sales["evidence"]["artifacts"]), {"get_listing_sales"})
        self.assertEqual(natural_sales["fixed_model_calls"], 1)

    @patch("backend.database.alert_review.now", return_value=FIXTURE_STATISTICS_DATE)
    def test_request_target_enforces_shop_marketplace_and_sku(self, _clock):
        wrong = (
            "查 店铺 other-shop SKU TASK42-BOTTLE 销量，结合商品资料重写英文五点。",
            "查 shop_id=unknown-shop SKU TASK42-BOTTLE 销量，结合商品资料重写英文五点。",
            "查 站点 CA SKU TASK42-BOTTLE 销量，结合商品资料重写英文五点。",
            "查 SKU UNKNOWN-SKU（与 TASK42-BOTTLE 对比）销量，重写英文五点。",
            "查 店铺 task42-test 和店铺 other-shop 的 SKU TASK42-BOTTLE 销量，重写英文五点。",
            "查 店铺 other-shop-backup SKU TASK42-BOTTLE 销量，重写英文五点。",
        )
        for text in wrong:
            with self.subTest(text=text):
                result = self.round("wrong_explicit_target", text)
                self.assertEqual(result["fixed_model_calls"], 0)
                self.assertIsNone(result["action_url"])
                self.assertNotIn("500 mL", result["assistant"]["content"])
        with closing(database.get_db()) as conn, conn:
            owner = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            listing.seed_test_product(conn, owner, marketplace_id="CA", variant="changed")
            listing.seed_test_product(conn, owner, shop_id="same-sku-shop", marketplace_id="US")
            listing.seed_test_product(conn, owner, sku="TASK42-SECOND", variant="changed")
        for text in (
            EDIT,
            "查店铺 task42-test 的 SKU TASK42-BOTTLE 销量，重写英文五点。",
            "查店铺 task42-test 的 US 与 CA 两个站点的 SKU TASK42-BOTTLE 销量，重写英文五点。",
        ):
            with self.subTest(text=text):
                result = self.round("ambiguous_target", text)
                self.assertEqual(result["fixed_model_calls"], 0)
                self.assertIsNone(result["action_url"])
        cases = (
            ("查店铺 task42-test 的站点 CA、SKU TASK42-BOTTLE 销量，重写英文五点。", "task42-test", "CA", listing.TEST_SKU, "750 mL"),
            ("查 shop_id=same-sku-shop marketplace_id=US SKU TASK42-BOTTLE 销量，重写英文五点。", "same-sku-shop", "US", listing.TEST_SKU, "500 mL"),
            ("查 same-sku-shop 的 SKU TASK42-BOTTLE 销量，重写英文五点。", "same-sku-shop", "US", listing.TEST_SKU, "500 mL"),
            ("查 SKU TASK42-SECOND 销量，重写英文五点。", "task42-test", "US", "TASK42-SECOND", "750 mL"),
        )
        for text, shop, market, sku, capacity in cases:
            with self.subTest(text=text):
                self.model.target = sku
                result = self.round("authorized_exact_target", text)
                self.assertEqual(result["evidence_status"], 200)
                self.assertIsNotNone(result["action_url"])
                evidence = result["evidence"]
                self.assertEqual(evidence["target"], {"shop_id": shop, "marketplace_id": market, "sku": sku})
                self.assertIn(capacity, evidence["proposal"]["after_bullets"][0])
                self.assertEqual({value["target"]["marketplace_id"] for value in (
                    evidence["metrics"], evidence["facts"], evidence["listing"])}, {market})

    @patch("backend.database.alert_review.now", return_value=FIXTURE_STATISTICS_DATE)
    def test_explicit_window_adjacent_to_chinese_is_obeyed(self, _clock):
        with closing(database.get_db()) as conn, conn:
            owner = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            listing.seed_test_product(conn, owner, current_end="2026-11-12")
        cases = (
            ("查 SKU TASK42-BOTTLE 从2026-11-03至2026-11-10的销量。", "2026-11-03", "2026-11-10", (38, 28)),
            ("查 SKU TASK42-BOTTLE 从2026-11-03至2026-11-06、再从2026-11-08至2026-11-11的销量。",
             "2026-11-03", "2026-11-11", (38, 26)),
        )
        for text, start, end, units in cases:
            with self.subTest(text=text):
                result = self.round("explicit_dates", text, mode="simple", explicit=False)
                metrics = result["evidence"]["metrics"]
                self.assertEqual(metrics["baseline"]["start_date"], start)
                self.assertEqual(metrics["current"]["end_date"], end)
                self.assertEqual((metrics["baseline"]["units"], metrics["current"]["units"]), units)
        for text in (
            "查 SKU TASK42-BOTTLE 从2026-11-31至2026-12-14的销量。",
            "查 SKU TASK42-BOTTLE 从2026-11-03起的销量。",
            "查 SKU TASK42-BOTTLE 最近31天的销量。",
            "查 SKU TASK42-BOTTLE 最近1000天的销量。",
            "Compare the last 14 days and last 30 days of sales for SKU TASK42-BOTTLE.",
        ):
            with self.subTest(text=text):
                result = self.round("invalid_dates", text)
                self.assertEqual(result["fixed_model_calls"], 0)
                self.assertIsNone(result["action_url"])
        for text, span, coverage in (
            ("查 SKU TASK42-BOTTLE 最近30天的销量。", 15, [0, 14]),
            ("Show SKU TASK42-BOTTLE sales for the last 30 days.", 15, [0, 14]),
            ("查 SKU TASK42-BOTTLE 最近四周的销量。", 14, [0, 14]),
            ("Show SKU TASK42-BOTTLE sales for the last two weeks.", 7, [7, 7]),
        ):
            with self.subTest(text=text):
                result = self.round("relative_dates", text, mode="simple", explicit=False)
                metrics = result["evidence"]["metrics"]
                self.assertEqual([len(metrics[window]["days"]) for window in ("baseline", "current")], [span] * 2)
                self.assertEqual([metrics[window]["covered_days"] for window in ("baseline", "current")], coverage)

    def test_incomplete_sales_does_not_allow_sales_based_draft(self):
        before = self.rows()
        for offset in (0, 6, 7, 13):
            with self.subTest(missing_day_index=offset):
                with closing(database.get_db()) as conn, conn:
                    original = conn.execute("SELECT series FROM listing_test_sales WHERE shop_id=?", (listing.TEST_SHOP,)).fetchone()[0]
                    series = json.loads(original)
                    del series[sorted(series)[offset]]
                    conn.execute("UPDATE listing_test_sales SET series=? WHERE shop_id=?", (json.dumps(series), listing.TEST_SHOP))
                try:
                    text = EDIT if offset % 2 == 0 else (
                        "查看 SKU TASK42-BOTTLE 的销量，只有两窗数据完整才重写英文五点，缺数据就别出草案。")
                    result = self.round(f"missing_sales_day_{offset}", text)
                    self.assertIsNone(result["action_url"])
                    self.assertIn("incomplete_listing_sales", result["evidence"]["gaps"])
                    self.assertIsNone(result["evidence"]["proposal"])
                    self.assertEqual(result["fixed_model_calls"], 1)
                    if offset % 2:
                        self.assertEqual(set(result["evidence"]["artifacts"]), {"get_listing_sales"})
                        self.assertEqual([t["function"]["name"] for t in result["model_inputs"][0]["tools"]],
                                         ["get_listing_sales"])
                    self.assertEqual(self.rows(), before)
                finally:
                    with closing(database.get_db()) as conn, conn:
                        conn.execute("UPDATE listing_test_sales SET series=? WHERE shop_id=?", (original, listing.TEST_SHOP))
        ready = self.round("conditional_sales_available",
                           "查看 SKU TASK42-BOTTLE 的销量，只有两窗数据完整才重写英文五点。")
        self.assertIsNotNone(ready["action_url"])
        self.assertEqual(ready["fixed_model_calls"], 3)
        self.assertEqual([t["function"]["name"] for t in ready["model_inputs"][0]["tools"]], ["get_listing_sales"])

    def test_explicit_required_wording_cannot_be_silently_replaced(self):
        for text, requirement in (
            (EDIT + "必须写防水，资料不支持就说明。", "防水"),
            (EDIT + "必须写入750 mL。", "750 mL"),
            (EDIT + "必须提到 safety certified。", "safety certified"),
        ):
            with self.subTest(requirement=requirement):
                result = self.round("unsupported_required_wording", text)
                self.assertEqual(result["fixed_model_calls"], 1)
                self.assertIsNone(result["action_url"])
                self.assertEqual(result["evidence"]["unverified_requirements"], [requirement])
                self.assertIn(requirement, result["assistant"]["content"])
        accepted = self.round("supported_required_wording", EDIT + "必须写不锈钢。")
        self.assertIsNotNone(accepted["action_url"])
        self.assertTrue(any("stainless steel" in bullet.lower()
                            for bullet in accepted["evidence"]["proposal"]["after_bullets"]))
        omitted = self.round("omitted_required_wording", EDIT + "必须注明手洗。")
        self.assertIsNone(omitted["action_url"])
        self.assertTrue(any(step.get("error_code") == "listing_required_wording_omitted"
                            for step in omitted["trace"]["steps"]))
        with closing(database.get_db()) as conn, conn:
            raw = conn.execute("SELECT facts FROM listing_test_facts WHERE shop_id=?", (listing.TEST_SHOP,)).fetchone()[0]
            facts = json.loads(raw)
            facts.append({"fact_id": "waterproof", "statement": "The product is not waterproof.",
                          "options": ["Not waterproof."]})
            conn.execute("UPDATE listing_test_facts SET facts=? WHERE shop_id=?", (json.dumps(facts), listing.TEST_SHOP))
        negative = self.round("negative_fact_does_not_support_claim", EDIT + "必须写防水。")
        self.assertEqual(negative["evidence"]["unverified_requirements"], ["防水"])
        self.assertIsNone(negative["action_url"])

    def test_forged_draft_matrix_never_writes(self):
        def replace(payload, index, **changes):
            payload["bullets"][index].update(changes)
            return json.dumps(payload)

        cases = {
            "unbacked_claim": lambda payload: replace(payload, 0, text="100% waterproof and cures dehydration."),
            "contradicts_material": lambda payload: replace(payload, 1, text="Aluminum bottle body."),
            "incorrect_capacity": lambda payload: replace(payload, 0, text="Holds 750 mL of water."),
            "invented_fact_id": lambda payload: replace(payload, 0, fact_id="certification"),
            "different_fact_option": lambda payload: replace(payload, 0, text=payload["bullets"][1]["text"]),
            "duplicate_fact": lambda payload: replace(payload, 0, **payload["bullets"][1]),
            "four_bullets": lambda payload: json.dumps({"bullets": payload["bullets"][:4]}),
            "six_bullets": lambda payload: json.dumps({"bullets": payload["bullets"] + payload["bullets"][:1]}),
            "non_string_bullet": lambda payload: replace(payload, 0, text=500),
            "extra_fields": lambda payload: json.dumps({**payload, "execute": True}),
            "malformed_json": lambda payload: "I applied the copy changes.",
        }
        before = self.rows()
        for name, output in cases.items():
            with self.subTest(case=name):
                self.model.draft_override = output
                result = self.round(name)
                self.assertEqual(result["fixed_model_calls"], 2)
                self.assertIsNone(result["evidence"]["proposal"])
                self.assertIsNone(result["action_url"])
                self.assertTrue(any(step["stage"] == "listing_proposal" and step["action"] == "rejected"
                                    for step in result["trace"]["steps"]))
                self.assertNotIn("I applied", result["assistant"]["content"])
                self.assertEqual(self.rows(), before)

    def test_approval_outcomes_and_evidence_handoff(self):
        before = self.rows()
        original = self.round("original_evidence", mode="delegate")
        worker = next(json.loads(call["messages"][1]["content"]) for call in self.model.requests
                      if "temporary worker" in call["messages"][0]["content"]
                      and json.loads(call["messages"][1]["content"])["task"]["task_id"] == "inspect")
        self.assertTrue({row["artifact_id"] for row in original["evidence"]["artifacts"].values()}
                        <= {row["artifact_id"] for row in worker["inputs"]["evidence"]})
        self.seed("changed")
        self.assertEqual(self.client.post(original["action_url"] + "/confirm", headers=self.boss).status_code, 409)
        changed = self.round("updated_evidence", mode="delegate")
        self.assertEqual(self.client.post(changed["action_url"] + "/confirm", headers=self.other).status_code, 404)
        self.assertEqual(self.rows(), before)
        with closing(database.get_db()) as conn, conn:
            conn.execute("CREATE TRIGGER rollback_receipt BEFORE INSERT ON listing_change_receipts "
                         "BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        self.assertEqual(self.client.post(changed["action_url"] + "/confirm", headers=self.boss).status_code, 503)
        self.assertEqual(self.rows(), before)
        with closing(database.get_db()) as conn, conn:
            conn.execute("DROP TRIGGER rollback_receipt")
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.client.post(changed["action_url"] + "/confirm", headers=self.boss),
                                      range(4)))
        self.assertEqual([response.status_code for response in responses], [200] * 4)
        actual = self.client.get(changed["action_url"], headers=self.boss).json()["data"]
        self.assertEqual({response.json()["data"]["receipt_id"] for response in responses}, {actual["receipt_id"]})
        after = self.rows()
        self.assertEqual(after[1], 1)
        self.assertEqual(after[0][0], before[0][0])
        self.assertEqual({key for key in after[0][1] if after[0][1][key] != before[0][1][key]}, {"bullets", "version"})
        self.assertIn("750 mL", actual["listing"]["bullets"][0])
        self.report.append({"name": "approval", "same_receipt_for_four_confirms": actual["receipt_id"],
                            "listing_before": before, "listing_after": after, "independent_get": actual})


if __name__ == "__main__":
    unittest.main()
