"""Focused homepage SSE regressions for plan-window follow-up."""

from contextlib import closing
from unittest.mock import patch

from backend.agent.tests import test_home_delegation as fixture
from backend.agent.tests.test_home_sse_tracing import tool_call
from backend.agent.tests.test_team_runtime import METRICS, task
from backend.agent.team_tools import TeamBudget
from backend.config.config import settings
from backend.tools import tools
from scripts.run_task26_t1 import SOURCE_PLAN, WINDOWS, metrics_fixture


def metrics(call_id, start, end):
    return tool_call(call_id, name=METRICS, plan_id=102, start_date=start, end_date=end)


WHOLE = WINDOWS[0][0], WINDOWS[1][1]
SINGLE = "查 #102 在 2026-10-17 至 2026-10-30 整个窗口的合计 CTR，再查知识库如何解释日期覆盖。只需要这个窗口的合计。"
COMPARE = "检查 #102 在 2026-10-17 至 2026-10-30 的 CTR 是否下降，比较两个可比窗口。"


def compare_dispatch(*tasks, exact=False):
    goal = {"plan_id": 102, "start_date": WHOLE[0], "end_date": WHOLE[1]}
    if exact:
        goal["windows"] = {slot: dict(zip(("start_date", "end_date"), window))
                           for slot, window in zip(("baseline", "current"), WINDOWS)}
    return tool_call("first", name="delegate_tasks", reason="核对历史 CTR 比较证据", tasks=tasks, comparison_goal=goal)


class Task26R3SSETests(fixture.HomeDelegationFixture):
    def history(self, *, missing=False):
        artifact, sql = metrics_fixture(self.database.parent, missing)
        self.evidence["sql"] = sql
        self.enterContext(patch.object(settings, "PLAN_METRICS_DB_PATH", str(artifact)))
        with closing(self.connect()) as conn, conn:
            manifest = sql["manifest"]
            conn.execute("INSERT INTO plan_metric_bindings VALUES (102, ?, ?, ?)",
                         (manifest["source_id"], manifest["data_version"], SOURCE_PLAN))
        actual = self.evidence["actual_calls"] = []
        original = tools.get_plan_metrics_by_date

        def read(plan_id, start_date, end_date, user_id):
            actual.append((plan_id, start_date, end_date, user_id))
            return original(plan_id, start_date, end_date, user_id)

        self.enterContext(patch.object(tools, METRICS, read))
        return actual

    def verify_completion(self, answer, workers):
        steps = self.complete(answer)
        created = [s["runtime_call_id"] for s in steps if (s["stage"], s["action"]) == ("worker", "created")]
        reclaimed = [s["runtime_call_id"] for s in steps if (s["stage"], s["action"]) == ("worker", "reclaimed")]
        self.assertEqual(len(created), workers)
        self.assertCountEqual(created, reclaimed)
        return steps

    async def test_whole_window_followup_can_redelegate_and_reuse_rule(self):
        actual = self.history()
        answer = "2026-10-17 至 2026-10-23 对比 2026-10-24 至 2026-10-30，引用 rule-example"
        rule_reads = self.enterContext(patch.object(tools, "search_knowledge", return_value={
            "context": "rule", "sources": [], "evidence": [{"chunk_id": "rule-example", "content": "rule"}],
        }))

        def worker(inputs, receipts):
            name = inputs["task"]["task_id"]
            if receipts:
                return fixture.worker_summary("窗口合计")
            if name == "whole":
                return fixture.reply("", metrics("whole-read", *WHOLE))
            if name == "rule":
                return fixture.reply("", tool_call("rule-read", name="search_knowledge", query="private-rule-query"))
            self.assertEqual(inputs["inputs"]["rule"][0]["result"]["evidence"][0]["chunk_id"], "rule-example")
            return fixture.reply("", *(metrics(f"part-{i}", *window) for i, window in enumerate(WINDOWS)))

        def main(receipts):
            if "first" not in receipts:
                return fixture.reply("", compare_dispatch(task("whole", tools=(METRICS,)),
                                                          task("rule", tools=("search_knowledge",))))
            if "second" not in receipts:
                rule = next(row for row in receipts["first"]["evidence"] if row["task_id"] == "rule")
                return fixture.reply("", fixture.dispatch("second", task(
                    "split", tools=(METRICS,), deps=("rule",),
                    artifacts=({"task_id": "rule", "artifact_id": rule["artifact_id"]},),
                )))
            return fixture.reply(answer)

        self.worker = worker
        self.select(main)
        await self.request(message=COMPARE + "同时核对知识库规则。")
        steps = self.verify_completion(answer, 3)
        self.assertEqual(actual, [(102, *window, 17) for window in (WHOLE, *WINDOWS)])
        rule_reads.assert_called_once()
        [followup] = [s for s in steps if s["stage"] == "evidence_followup"]
        self.assertEqual(followup["action"], "opened")
        [gap] = followup["evidence"]
        self.assertEqual(gap["gap_type"], "window_granularity")
        self.assertEqual(gap["missing_evidence"], "two_comparable_windows")
        self.assertEqual(gap["comparison_goal"]["plan_id"], 102)
        whole = next(row for row in self.main_receipts["first"]["evidence"] if row["task_id"] == "whole")
        self.assertEqual(whole["result"]["aggregation"], "window_total")
        [comparison] = self.main_receipts["second"]["ctr_comparisons"]
        self.assertAlmostEqual(comparison["relative_decline_pct"], 33.75778631909133)
        self.assertCountEqual(
            [comparison["baseline_artifact_id"], comparison["current_artifact_id"]],
            [row["artifact_id"] for row in self.main_receipts["second"]["evidence"]],
        )
        [final] = [s["evidence"] for s in steps if s["action"] == "final_evidence"]
        self.assertEqual(sum(item.get("dates_or_artifact_cited", False) for item in final), 2)
        self.assertTrue(any(item.get("cited_chunk_ids") == ["rule-example"] for item in final))

    async def test_confirmed_gap_reused_by_main_and_another_worker(self):
        actual = self.history(missing=True)
        answer = "2026-10-27 缺报，补齐后再比较"
        self.worker = lambda inputs, receipts: (
            fixture.worker_summary("缺报", "missing") if receipts else
            fixture.reply("", metrics(inputs["task"]["task_id"] + "-read", *WHOLE))
        )

        def main(receipts):
            if not receipts:
                return fixture.reply("", compare_dispatch(task("initial", tools=(METRICS,))))
            if "duplicate" not in receipts:
                return fixture.reply("", metrics("duplicate", *WHOLE),
                                     fixture.dispatch("second", task("repeat", tools=(METRICS,))))
            return fixture.reply(answer)

        self.select(main)
        await self.request(message=COMPARE)
        steps = self.verify_completion(
            "任务未完成。连续两轮没有新增证据，已停止继续查询。缺报日期 2026-10-27。\n\n" + answer, 2)
        self.assertEqual(actual, [(102, *WHOLE, 17)])
        [original, _] = self.main_receipts["first"]["gaps"]
        direct = self.main_receipts["duplicate"]
        [worker, _] = self.main_receipts["second"]["gaps"]
        for receipt in (original, direct, worker):
            self.assertEqual(receipt["gap_type"], "permanent")
            self.assertEqual(receipt["runtime_call_id"], original["runtime_call_id"])
            self.assertEqual(receipt["result"]["coverage"]["missing_dates"], ["2026-10-27"])
        self.assertCountEqual([s["role"] for s in steps if s["stage"] == "tool_reuse"],
                              ["orchestrator", "team_worker"])
        self.assertFalse(any(s["stage"] == "evidence_followup" for s in steps))
        decisions = [s for s in steps if s["stage"] == "comparison_evidence"]
        self.assertTrue(decisions)
        for step in decisions:
            self.assertEqual(step["action"], "confirmed_gap")
            [gap] = step["evidence"]["missing_evidence"]
            self.assertEqual(gap["missing_dates"], ["2026-10-27"])
            self.assertEqual(gap["runtime_call_id"], original["runtime_call_id"])
        self.assertEqual(self.main_receipts["second"]["remaining"]["tool"],
                         self.main_receipts["first"]["remaining"]["tool"])

    async def test_followup_consumes_remaining_tool_budget_and_finalizes(self):
        actual = self.history()
        answer = "只读到一窗，额度用尽，无法确认变化"
        self.worker = lambda _, receipts: (
            fixture.worker_summary("只有整窗合计", "missing") if receipts else
            fixture.reply("", metrics("whole", *WHOLE))
        )

        def main(receipts):
            if not receipts:
                return fixture.reply("", compare_dispatch(task("initial", tools=(METRICS,))))
            if "first-part" not in receipts:
                return fixture.reply("", metrics("first-part", *WINDOWS[0]), metrics("second-part", *WINDOWS[1]))
            return fixture.reply(answer)

        self.select(main)
        with patch.object(self.server, "TeamBudget", return_value=TeamBudget(tool_limit=2)):
            await self.request(message=COMPARE)
        steps = self.verify_completion(answer, 1)
        self.assertEqual(actual, [(102, *WHOLE, 17), (102, *WINDOWS[0], 17)])
        self.assertEqual(self.main_receipts["second-part"]["error_code"], "budget_exhausted_or_cancelled")
        self.assertEqual([s["action"] for s in steps if s["stage"] == "model_call" and s.get("role") == "orchestrator"],
                         ["plan", "main", "evidence_followup", "main_summary"])

    async def test_single_total_allows_authorized_knowledge_read(self):
        actual = self.history()
        answer = "2026-10-17 至 2026-10-30 共 19,434 曝光、5,886 点击，CTR 30.29%。日期覆盖见 fixture-rule。"
        read_rule = self.enterContext(patch.object(tools, "search_knowledge", return_value={
            "context": "rule", "sources": [], "evidence": [{"chunk_id": "fixture-rule", "content": "rule"}],
        }))
        self.worker = lambda _, receipts: (fixture.worker_summary("整窗合计已核实") if receipts else
                                           fixture.reply("", metrics("whole", *WHOLE)))

        def main(receipts):
            if "first" not in receipts:
                return fixture.reply("", fixture.dispatch("first", task("whole", tools=(METRICS,))))
            if "read-rule" not in receipts:
                return fixture.reply("", tool_call("read-rule", name="search_knowledge", query="解释日期覆盖"))
            return fixture.reply(answer)

        self.select(main)
        await self.request(message=SINGLE)
        steps = self.verify_completion(answer, 1)
        self.assertEqual(actual, [(102, *WHOLE, 17)])
        self.assertFalse(any(step["stage"] == "evidence_followup" for step in steps))
        [decision] = [step for step in steps if step["stage"] == "comparison_evidence"]
        self.assertEqual(decision["action"], "not_requested")
        self.assertEqual(decision["evidence"]["missing_evidence"], [])
        read_rule.assert_called_once_with(query="解释日期覆盖")
        self.evidence["actual_knowledge_calls"] = [call.kwargs for call in read_rule.call_args_list]
        self.assertEqual(self.main_receipts["read-rule"]["result"]["evidence"][0]["chunk_id"], "fixture-rule")

    async def test_overlapping_whole_keeps_baseline_and_only_reads_current(self):
        actual = self.history()
        answer = "2026-10-17 至 2026-10-23 CTR 38.10%，2026-10-24 至 2026-10-30 CTR 25.24%，相对下降 33.76%。"
        for already_complete in (False, True):
            with self.subTest(already_complete=already_complete):
                self.events = []
                self.main_receipts.clear()
                actual.clear()
                initial = WINDOWS if already_complete else (WHOLE, WINDOWS[0])

                def worker(_, receipts):
                    if receipts:
                        return fixture.worker_summary("两窗已查" if already_complete else "整窗及基准窗已查，仍缺当前窗",
                                                      "succeeded" if already_complete else "missing")
                    return fixture.reply("", *(metrics(f"worker-{i}", *window) for i, window in enumerate(initial)))

                def main(receipts):
                    if "first" not in receipts:
                        return fixture.reply("", compare_dispatch(task("sample", tools=(METRICS,)), exact=already_complete))
                    if not already_complete and "current" not in receipts:
                        return fixture.reply("", metrics("current", *WINDOWS[1]))
                    return fixture.reply(answer)

                self.worker = worker
                self.select(main)
                await self.request(message=COMPARE if not already_complete else COMPARE +
                                   "基准窗为 2026-10-17 至 2026-10-23，当前窗为 2026-10-24 至 2026-10-30。")
                if already_complete:
                    self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
                    self.assertEqual(self.assistant_replies(), [answer, answer])
                    steps = self.assert_trace("completed", expected_calls=None, trace_count=2)
                    self.assertFalse(any(step["stage"] == "evidence_followup" for step in steps))
                    self.assertEqual(actual, [(102, *window, 17) for window in WINDOWS])
                    receipt = self.main_receipts["first"]
                else:
                    steps = self.verify_completion(answer, 1)
                    self.assertEqual(actual, [(102, *window, 17) for window in (WHOLE, *WINDOWS)])
                    [followup] = [step for step in steps if step["stage"] == "evidence_followup"]
                    self.assertEqual(followup["action"], "opened")
                    [gap] = followup["evidence"]
                    self.assertEqual(gap["missing_evidence"], "current_window")
                    [baseline] = gap["reusable_windows"]
                    self.assertEqual(baseline["artifact_id"], self.main_receipts["first"]["evidence"][1]["artifact_id"])
                    receipt = self.main_receipts["current"]
                    self.evidence["overlap_calls"] = list(actual)
                    self.evidence["overlap_receipts"] = dict(self.main_receipts)
                [comparison] = receipt["ctr_comparisons"]
                [complete] = [step for step in steps if step["stage"] == "comparison_evidence" and step["action"] == "complete"]
                self.assertEqual(complete["evidence"]["adopted_pairs"], [{key: comparison[key] for key in
                                  ("baseline_artifact_id", "current_artifact_id")}])
                sql = self.evidence["sql"]
                for field, expected in (("baseline_ctr_pct", sql["windows"][0]["ctr"]),
                                        ("current_ctr_pct", sql["windows"][1]["ctr"]),
                                        ("relative_decline_pct", sql["relative_drop"])):
                    self.assertAlmostEqual(comparison[field], 100 * expected)
