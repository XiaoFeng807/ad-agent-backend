"""Fixed-model SSE coverage for CTR facts owned by verified receipts."""

from datetime import date, timedelta
from unittest.mock import patch

from backend.agent.tests import test_home_delegation as fixture
from backend.agent.tests.test_home_sse_tracing import tool_call
from backend.agent.tests.test_task26_r3_sse import metrics
from backend.rag.review_rule_demo import DEMO_RULES
from backend.tools import tools
from scripts.run_task26_t1 import MISSING_DAY, WINDOWS


QUESTION = ("请比较计划 #102 的 CTR。基准 2026-10-17 至 2026-10-23，当前 2026-10-24 至 2026-10-30。"
            "用 CTR 复核规则 v1 核对各窗覆盖和曝光门槛，报告相对变化和复核结论。")
RULE = "ctr-review-v1-low-ctr-01"


class CTRReplyFixture(fixture.HomeDelegationFixture):
    def readings(self, baseline, current, *, missing=False, forged=False):
        def read(plan_id, start_date, end_date, user_id):
            is_baseline = start_date == WINDOWS[0][0]
            impressions, clicks = baseline if is_baseline else current
            days = [(date.fromisoformat(start_date) + timedelta(days=i)).isoformat() for i in range(7)]
            missing_dates = [MISSING_DAY] if missing and not is_baseline else []
            rows = [dict(metric_date=day, impressions=impressions if i == 0 else 0,
                         clicks=clicks if i == 0 else 0)
                    for i, day in enumerate(days) if day not in missing_dates]
            return dict(plan_id=plan_id, start_date=start_date, end_date=end_date, scope="plan",
                        source_id="fixture", data_version="r4-test", source_plan_id="local-102",
                        source=dict(kind="synthetic", source_url="fixture", license="fixture",
                                    compressed_sha256="fixture", tsv_sha256="fixture", sampling="fixture",
                                    time_mapping={"anchor_date": WINDOWS[0][0]}),
                        coverage=dict(complete=not missing_dates, expected_days=7, observed_days=len(rows),
                                      missing_dates=missing_dates),
                        impressions=impressions, clicks=clicks, daily=rows)

        self.enterContext(patch.object(tools, "get_plan_metrics_by_date", side_effect=read))
        evidence = [dict(chunk_id=chunk, rule_id=rule_id, version=version,
                         content="曝光需超过 9,999 次" if forged and chunk == RULE else content)
                    for chunk, rule_id, version, _, content in DEMO_RULES[:3]]
        self.enterContext(patch.object(tools, "search_knowledge", return_value={
            "context": "rules", "sources": [], "evidence": evidence,
        }))


class Task26R4CTRReplyTests(CTRReplyFixture):
    async def answer(self, model_text, *, delegate=False):
        reads = [metrics("baseline", *WINDOWS[0]), metrics("current", *WINDOWS[1])]
        rule = tool_call("rule", name="search_knowledge", query="CTR 复核规则 v1")
        if delegate:
            worker_reads = reads
            dispatch = fixture.dispatch("batch", fixture.task("metrics", tools=("get_plan_metrics_by_date",)))
            self.worker = lambda _, receipts: (fixture.worker_summary("窗口已读取") if receipts
                                               else fixture.reply("", *worker_reads))
            reads = [dispatch]
        self.select(lambda receipts: fixture.reply(model_text) if receipts else fixture.reply("", *reads, rule))
        await self.request(message=QUESTION)
        text = next(event["content"] for event in self.events if event["type"] == "text")
        steps = self.complete(text)
        [evidence] = [step["evidence"] for step in steps if step["action"] == "ctr_reply"]
        self.assertNotIn(model_text, text)
        return text, evidence

    async def test_delegated_current_gap_preserves_verified_baseline(self):
        self.readings((1322, 345), (1200, 200), missing=True)
        text, evidence = await self.answer("分母窗口不完整，无法计算。", delegate=True)
        baseline, current = evidence["windows"]
        self.assertEqual((baseline["impressions"], baseline["clicks"], baseline["threshold"]), (1322, 345, "已满足"))
        self.assertEqual((current["coverage"]["observed_days"], current["coverage"]["missing_dates"]), (6, [MISSING_DAY]))
        self.assertIsNone(current["impressions"])
        self.assertIsNone(current["ctr_pct"])
        self.assertEqual(current["threshold"], "未知")
        self.assertEqual(evidence["combined_threshold"], "未知")
        self.assertIn("当前窗缺报，无法计算相对变化，暂缓复核判定。", text)

    async def test_unverified_requested_rule_leaves_threshold_unknown(self):
        self.readings((1400, 280), (1400, 140), forged=True)
        text, evidence = await self.answer("两窗未达到曝光门槛。")
        self.assertIsNone(evidence["policy"])
        self.assertEqual([row["threshold"] for row in evidence["windows"]], ["未知", "未知"])
        self.assertEqual(evidence["combined_threshold"], "未知")
        self.assertIn("相对下降 50.00%", text)
        self.assertIn("暂缓复核判定", text)
        self.assertNotIn("样本不足", text)
        self.assertNotIn("标记为待复核", text)

    async def test_zero_baseline_keeps_defined_window_rates_and_sample_status(self):
        self.readings((3, 0), (10, 5))
        text, evidence = await self.answer("无法计算两窗 CTR。")
        self.assertEqual([row["ctr_pct"] for row in evidence["windows"]], [0.0, 50.0])
        self.assertIsNone(evidence["relative_decline_pct"])
        self.assertEqual(evidence["combined_threshold"], "未达到")
        self.assertIn("已确认样本不足", text)
        self.assertIn("相对变化无定义", text)

    async def test_model_cannot_assert_noise_or_cause_from_metrics(self):
        self.readings((1000, 100), (1000, 80))
        text, evidence = await self.answer("下降由素材疲劳导致，完全在噪声范围内，没有统计显著性。")
        self.assertEqual(evidence["relative_decline_pct"], 20.0)
        self.assertIn("因此标记为待复核", text)
        for unsupported in ("素材疲劳", "噪声", "统计显著性"):
            self.assertNotIn(unsupported, text)
