"""Exercise the requested answer format at the ordinary-chat ASGI entry point."""

import json
from fractions import Fraction

from backend.agent.tests import test_task26_r4_ctr_reply as fixture
from scripts.run_task26_t1 import MISSING_DAY


AD037_SUFFIX = " 请只返回一个合法 JSON 对象，包含 baseline_ctr_pct、current_ctr_pct、relative_decline_pct 和 review_required。"
AD037_MODEL_TEXT = json.dumps(dict(baseline_ctr_pct=10.0, current_ctr_pct=8.0,
                                   relative_decline_pct=20.0, review_required=True))


class Task26R6CTRJSONTests(fixture.CTRReplyFixture):
    async def answer(self, model_text, suffix=AD037_SUFFIX, *, current_read=True):
        baseline, current = [fixture.metrics(name, *window)
                             for name, window in zip(("baseline", "current"), fixture.WINDOWS)]
        rule = fixture.tool_call("rule", name="search_knowledge", query="CTR 复核规则 v1")
        reads = [baseline, current, rule] if current_read else [baseline, rule]
        self.select(lambda receipts: fixture.fixture.reply(model_text) if receipts
                    else fixture.fixture.reply("", *reads))
        self.evidence["model_text"] = model_text
        await self.request(message=fixture.QUESTION + suffix)
        [text] = [event["content"] for event in self.events if event["type"] == "text"]
        steps = self.complete(text)
        [facts] = [step["evidence"] for step in steps if step["action"] == "ctr_reply"]
        return text, facts

    async def test_ad037_complete_windows_keep_requested_json(self):
        self.readings((1000, 100), (1000, 80))
        text, facts = await self.answer(AD037_MODEL_TEXT)
        reply = json.loads(text)
        self.assertEqual(reply, json.loads(AD037_MODEL_TEXT))
        self.assertIs(reply["review_required"], True)
        self.assertEqual(facts["format"], "json")
        base, current = [self.main_receipts[key]["result"] for key in ("baseline", "current")]
        base_ctr, current_ctr = (Fraction(row["clicks"], row["impressions"]) for row in (base, current))
        self.assertEqual(reply["baseline_ctr_pct"], float(100 * base_ctr))
        self.assertEqual(reply["current_ctr_pct"], float(100 * current_ctr))
        self.assertEqual(reply["relative_decline_pct"], float(100 * (base_ctr - current_ctr) / base_ctr))
        self.assertEqual(facts["policy"]["chunk_id"], fixture.RULE)

    async def test_current_gap_is_null_and_preserves_known_fields(self):
        self.readings((1000, 100), (1000, 80), missing=True)
        text, facts = await self.answer(AD037_MODEL_TEXT, AD037_SUFFIX + "另含 current_coverage 和 review_status。")
        reply = json.loads(text)
        self.assertEqual(reply["baseline_ctr_pct"], 10.0)
        for key in ("current_ctr_pct", "relative_decline_pct", "review_required"):
            self.assertIsNone(reply[key])
        self.assertEqual(reply["review_status"], "未知")
        self.assertEqual(reply["current_coverage"]["missing_dates"], [MISSING_DAY])
        self.assertEqual(facts["windows"][1]["threshold"], "未知")

    async def test_wrong_model_numbers_are_replaced_from_receipts(self):
        self.readings((1000, 100), (1000, 90))
        text, _ = await self.answer('```json\n{"baseline_ctr_pct": 99, "review_required": true, "cause": "素材疲劳"}\n```')
        self.assertEqual(json.loads(text), dict(baseline_ctr_pct=10.0, current_ctr_pct=9.0,
                                                relative_decline_pct=10.0, review_required=False))
        self.assertIs(json.loads(text)["review_required"], False)

    async def test_json_mention_with_markdown_request_keeps_markdown(self):
        self.readings((1000, 100), (1000, 80))
        text, facts = await self.answer(AD037_MODEL_TEXT, ' “请只返回 JSON”是格式示例。请用 Markdown 答复。')
        self.assertTrue(text.startswith("## 计划 #102"))
        self.assertEqual(facts["format"], "markdown")

    async def test_ordinary_markdown_uses_verified_receipts(self):
        self.readings((1000, 100), (1000, 90))
        text, facts = await self.answer("当前 CTR 为 0%，应标记为待复核。", "")
        self.assertIn("| 1,000 | 90 | 9.00% | 已满足 |", text)
        self.assertIn("未达到复核阈值。", text)
        self.assertNotIn("应标记为待复核", text)
        self.assertEqual(facts["format"], "markdown")

    async def test_unread_window_cannot_use_model_values(self):
        self.readings((1000, 100), (1000, 80))
        text, facts = await self.answer(AD037_MODEL_TEXT, current_read=False)
        self.assertEqual(json.loads(text), dict(baseline_ctr_pct=10.0, current_ctr_pct=None,
                                                relative_decline_pct=None, review_required=None))
        self.assertEqual(facts["comparison_gap"], "missing_window")

    async def test_unverified_rule_keeps_review_unknown(self):
        self.readings((1000, 100), (1000, 80), forged=True)
        text, facts = await self.answer(AD037_MODEL_TEXT, AD037_SUFFIX + "另含 rule_chunk_id。")
        reply = json.loads(text)
        self.assertEqual(reply["relative_decline_pct"], 20.0)
        self.assertIsNone(reply["review_required"])
        self.assertIsNone(reply["rule_chunk_id"])
        self.assertIsNone(facts["policy"])
