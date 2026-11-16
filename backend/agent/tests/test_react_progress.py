"""Exercise the progress decisions through the FastAPI SSE route."""

from contextlib import closing
import json
from threading import Barrier
from unittest.mock import patch

from backend.agent.team_tools import RunReadCache, TeamBudget
from backend.agent.tests.test_home_delegation import HomeDelegationFixture, dispatch, reply, worker_summary
from backend.agent.tests.test_home_sse_tracing import tool_call
from backend.agent.tests.test_team_runtime import METRICS, task
from backend.config.config import settings
from backend.database import chat_groups
from backend.tools import tools
from scripts.run_task26_t1 import SOURCE_PLAN, metrics_fixture
from scripts.verify_task40 import has_plan_status


DETAIL = "get_plan_detail"
PLANS = tuple(range(101, 107))
SIX = "请逐项查询本人广告计划 #101、#102、#103、#104、#105、#106 的投放状态，逐项查完再汇总。"
ONE = "请查一下计划 #101 当前投放状态。"
THREE = "请逐项查询本人广告计划 #101、#102、#103 的投放状态，逐项查完再汇总。"


class ReActProgressTests(HomeDelegationFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        with closing(self.connect()) as conn, conn:
            conn.executemany("INSERT INTO ad_plans (id,user_id,plan_name,status) VALUES (?,17,?,'active')",
                             [(plan, f"plan-{plan}") for plan in PLANS[2:]])
        self.reads = []
        self.decisions = []
        self.evidence.update(bottom_reads=self.reads, main_tool_decisions=self.decisions)
        original = tools.get_plan_detail

        def read(plan_id, user_id):
            self.reads.append(plan_id)
            return original(plan_id, user_id)

        self.enterContext(patch.object(tools, DETAIL, read))

    def sequential(self, *, premature=False):
        def create(messages, tools=None, **_):
            if messages[0]["content"] == "你是一个任务规划专家, 输出JSON格式计划。":
                return reply('{"steps":[]}')
            receipts = [json.loads(row["content"]) for row in messages if row["role"] == "tool"]
            if tools and len(receipts) < len(PLANS) and not (premature and not receipts and not self.decisions):
                plan = PLANS[len(receipts)]
                self.decisions.append(plan)
                return reply("", tool_call(f"read-{len(receipts)}", name=DETAIL, plan_id=plan))
            if tools and premature and not receipts:
                self.decisions.append("premature-text")
                return reply("已全部查询。")
            return reply("已核验 " + "、".join(f"#{row['result']['id']}" for row in receipts if row["success"]))

        self.model.create = create

    def check_reply(self, reason, *, remaining_model=None, remaining_tool=None, missing=()):
        reply_text = self.events[-2]["content"]
        steps = self.complete(reply_text)
        stop = [row for row in steps if row["stage"] == "react_decision" and row["action"] == "stop"][-1]
        self.assertEqual(stop["evidence"]["reason"], reason)
        self.assertEqual(stop["evidence"]["missing_plan_ids"], list(missing))
        self.assertEqual(len(stop["evidence"]["artifact_ids"]), len(set(self.reads)))
        if remaining_model is not None:
            self.assertEqual(stop["budget"]["model"], remaining_model)
        if remaining_tool is not None:
            self.assertEqual(stop["budget"]["tool"], remaining_tool)
        self.assertLess(next(i for i, row in enumerate(steps) if row["stage"] == "persistence"), len(steps))
        return reply_text, steps

    async def test_six_separate_main_tool_decisions(self):
        self.sequential()
        await self.request(message=SIX)
        self.assertEqual(self.reads, list(PLANS))
        self.assertEqual(self.decisions, list(PLANS))
        answer, steps = self.check_reply("goal_satisfied", remaining_model=5, remaining_tool=6)
        self.assertEqual(sum(row["stage"] == "model_call" and row["action"] == "main" for row in steps), 6)
        self.assertTrue(all(f"#{plan}" in answer for plan in PLANS))
        self.assertIn("#106 plan-106，投放中", answer)
        self.assertFalse(any(row["action"] == "main_summary" for row in steps))

    async def test_one_status_read_early_stop(self):
        self.sequential()
        await self.request(message=ONE)
        self.assertEqual(self.reads, [101])
        answer, steps = self.check_reply("goal_satisfied", remaining_model=11, remaining_tool=11)
        self.assertIn("#101", answer)
        self.assertFalse(any(row["stage"] == "worker" for row in steps))

    async def test_repeated_evidence_does_not_reset_stall_count(self):
        self.select(lambda receipts: reply("已核验 #101。") if len(receipts) == 3 else reply(
            "", tool_call(f"new-id-{len(receipts)}", name=DETAIL, plan_id=101)))
        await self.request(message=SIX)
        self.assertEqual(self.reads, [101])
        answer, steps = self.check_reply("no_progress", missing=PLANS[1:])
        self.assertIn("#106", answer)
        self.assertEqual([step["evidence"]["stalled_rounds"] for step in steps
                          if step["stage"] == "react_decision"], [0, 1, 2])
        reuse = [step for step in steps if step["stage"] == "tool_reuse"]
        self.assertEqual(len(reuse), 2)
        self.assertEqual(len({step["evidence"]["artifact_id"] for step in reuse}), 1)
        self.assertTrue(all(not step["evidence"]["executed"] for step in reuse))

    async def test_alternating_queries_stop_and_group_history_preserves_reason(self):
        group = chat_groups.create(17, "交替读取")["conversation_id"]
        other = chat_groups.create(17, "独立读取")["conversation_id"]
        self.select(lambda receipts: reply("", tool_call(f"alternate-{len(receipts)}", name=DETAIL,
                                                        plan_id=(101, 102)[len(receipts) % 2])))
        await self.request(body={"message": THREE, "conversation_id": group})
        self.assertEqual(self.reads, [101, 102])
        self.assertEqual(len(self.main_inputs), 4)
        terminal = self.events[-1]
        self.assertEqual([row["type"] for row in self.events], ["thinking", "text", "done"])
        history = chat_groups.history(17, group)
        self.evidence["group_history"] = history
        reply_entry, = [entry for entry in history["entries"] if entry["kind"] == "assistant"]
        self.assertEqual(reply_entry["content"], self.events[-2]["content"])
        self.assertIn("缺少 #103", reply_entry["content"])
        notices = [entry for entry in history["entries"] if entry.get("event", {}).get("kind") in
                   {"read_reused", "react_stopped"}]
        self.assertEqual([entry["event"]["kind"] for entry in notices], ["read_reused", "read_reused", "react_stopped"])
        self.assertIn("#101、#102 已核验。#103 仍缺详情。", notices[-1]["content"])
        self.assertTrue(all(entry["run_id"] == terminal["run_id"] for entry in notices))
        trace = self.assert_trace("completed", expected_calls=None)
        self.assertFalse(any(row["stage"] == "worker" for row in trace))
        stop = [row for row in trace if row["stage"] == "react_decision"][-1]
        self.assertEqual(stop["evidence"]["reason"], "no_progress")
        self.assertEqual(stop["evidence"]["run_id"], terminal["run_id"])

        for conversation in (group, other):
            self.events.clear()
            self.select(lambda _: reply("", tool_call("fresh", name=DETAIL, plan_id=101)))
            await self.request(body={"message": ONE, "conversation_id": conversation})
            self.assertEqual(self.events[-1]["type"], "done")
            self.assertNotEqual(self.events[-1]["run_id"], terminal["run_id"])
        self.assertEqual(self.reads, [101, 102, 101, 101])

    async def test_same_batch_duplicate_reads_share_the_first_receipt(self):
        self.select(lambda receipts: reply("已核验 #101") if receipts else reply(
            "", tool_call("first", name=DETAIL, plan_id=101), tool_call("second", name=DETAIL, plan_id=101)))
        await self.request(message="核验本人计划的名称。")
        self.assertEqual(self.reads, [101])
        self.assertEqual(self.main_receipts["first"]["artifact_id"], self.main_receipts["second"]["artifact_id"])
        self.assertTrue(self.main_receipts["second"]["reused"])
        self.complete("已核验 #101")

    async def test_reuse_rechecks_current_plan_owner(self):
        def choose(receipts):
            if len(receipts) == 1:
                with closing(self.connect()) as conn, conn:
                    conn.execute("UPDATE ad_plans SET user_id=18 WHERE id=101")
            return reply("", tool_call(f"owner-{len(receipts)}", name=DETAIL, plan_id=101))

        self.select(choose)
        await self.request(message=THREE)
        self.assertEqual(self.reads, [101])
        self.assertEqual(self.main_receipts["owner-1"]["error_code"], "read_scope_changed")
        self.assertNotIn("result", self.main_receipts["owner-1"])
        answer = self.events[-2]["content"]
        self.complete(answer)
        self.assertIn("缺少 #101", answer)
        self.assertNotIn("owned-plan-a", answer)

    async def test_handoff_rechecks_owner_for_direct_and_worker_artifacts(self):
        for reference in ("direct_refs", "artifact_refs", "summary_refs"):
            with self.subTest(reference=reference):
                await self.check_transferred_handoff(reference)

    async def test_worker_cannot_return_earlier_evidence_after_its_reuse_is_denied(self):
        def worker(_, receipts):
            if not receipts:
                return reply("", tool_call("before", name=DETAIL, plan_id=101))
            if "after" not in receipts:
                with closing(self.connect()) as conn, conn:
                    conn.execute("UPDATE ad_plans SET user_id=18 WHERE id=101")
                return reply("", tool_call("after", name=DETAIL, plan_id=101))
            return worker_summary("权限变更，无法继续取证", "missing")

        def choose(receipts):
            if not receipts:
                return reply("", dispatch("batch", task("owner-check")))
            if "read-102" not in receipts:
                return reply("", tool_call("read-102", name=DETAIL, plan_id=102),
                             tool_call("read-103", name=DETAIL, plan_id=103))
            return reply("其余计划已核验")

        self.worker = worker
        self.select(choose)
        await self.request(message=THREE)
        self.assertEqual(self.reads, [101, 102, 103])
        self.assertEqual(self.worker_receipts[-1]["after"]["error_code"], "read_scope_changed")
        self.assertEqual(self.main_receipts["batch"]["evidence"], [])
        answer = self.events[-2]["content"]
        self.complete(answer)
        self.assertIn("缺少 #101 的详情回执（计划读取权限已变更）", answer)
        self.assertNotIn("owned-plan-a", answer)

    async def check_transferred_handoff(self, reference):
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE ad_plans SET user_id=17 WHERE id=101")
        self.events.clear()
        self.main_receipts.clear()
        self.reads.clear()
        self.worker_inputs.clear()
        start = "source-" + reference

        def worker(inputs, receipts):
            if receipts:
                return worker_summary("已读取")
            plan = 101 if inputs["task"]["task_id"] == start else 102
            return reply("", tool_call("worker-read", name=DETAIL, plan_id=plan))

        def choose(receipts):
            if not receipts:
                if reference == "direct_refs":
                    return reply("", tool_call("source", name=DETAIL, plan_id=101))
                return reply("", dispatch("source", task(start)))
            if "consumer" not in receipts:
                with closing(self.connect()) as conn, conn:
                    conn.execute("UPDATE ad_plans SET user_id=18 WHERE id=101")
                if reference == "direct_refs":
                    consumer = {**task("consumer"), "direct_refs": [receipts["source"]["artifact_id"]]}
                else:
                    key = "summaries" if reference == "summary_refs" else "evidence"
                    source, = receipts["source"][key]
                    consumer = task("consumer", deps=(start,), artifacts=(
                        {"task_id": start, "artifact_id": source["artifact_id"]},))
                return reply("", dispatch("consumer", consumer))
            if "independent-102" not in receipts:
                return reply("", tool_call("independent-102", name=DETAIL, plan_id=102),
                             tool_call("independent-103", name=DETAIL, plan_id=103))
            return reply("其余计划已核验")

        self.worker = worker
        self.select(choose)
        await self.request(message=THREE)
        answer = self.events[-2]["content"]
        self.evidence.setdefault("transferred_handoffs", {})[reference] = {
            "worker_task_ids": [row["task"]["task_id"] for row in self.worker_inputs],
            "reads": self.reads.copy(), "events": self.events.copy(), "dispatch": self.main_receipts.get("consumer"),
        }
        self.assertEqual(self.events[-1]["type"], "done")
        self.assertEqual({row["task"]["task_id"] for row in self.worker_inputs},
                         set() if reference == "direct_refs" else {start})
        self.assertEqual(self.reads, [101, 102, 103])
        self.assertEqual(self.main_receipts["consumer"]["gaps"][0]["error_code"], "read_scope_changed")
        self.assertIn("缺少 #101", answer)
        self.assertIn("计划读取权限已变更", answer)
        self.assertNotIn("owned-plan-a", answer)
        self.assertEqual(self.assistant_replies()[-1], answer)

    async def test_parallel_workers_share_the_read_without_merging_free_text_tasks(self):
        together = Barrier(2, timeout=5)

        def worker(inputs, receipts):
            if receipts:
                return worker_summary("已读取")
            together.wait()
            return reply("", tool_call(inputs["task"]["task_id"], name=DETAIL, plan_id=101))

        self.worker = worker
        self.select(lambda receipts: reply("已核验 #101") if receipts else reply(
            "", dispatch("batch", task("left"), task("right"))))
        await self.request(message="分别核验本人计划的名称。")
        self.assertEqual(self.reads, [101])
        steps = self.complete("已核验 #101")
        self.assertEqual(sum(row["stage"] == "worker" and row["action"] == "created" for row in steps), 2)
        self.assertEqual(sum(row["stage"] == "tool_reuse" for row in steps), 1)
        evidence = self.main_receipts["batch"]["evidence"]
        self.assertEqual({row["task_id"] for row in evidence}, {"left", "right"})
        self.assertEqual(len({row["artifact_id"] for row in evidence}), 1)

    async def test_concurrent_workers_share_one_cold_read_and_its_retry(self):
        together = Barrier(2, timeout=5)
        attempts = []
        original_read = tools.get_plan_detail
        original_lock = RunReadCache.lock_for

        def contend(cache, key):
            lock = original_lock(cache, key)
            together.wait()
            return lock

        def read(plan_id, user_id):
            attempts.append((plan_id, user_id))
            if len(attempts) == 1:
                raise TimeoutError("first concurrent read")
            return original_read(plan_id, user_id)

        self.worker = lambda inputs, receipts: worker_summary("已读取") if receipts else reply(
            "", tool_call(inputs["task"]["task_id"], name=DETAIL, plan_id=101))
        self.select(lambda receipts: reply("已核验 #101") if receipts else reply(
            "", dispatch("batch", task("left"), task("right"))))
        with patch.object(RunReadCache, "lock_for", contend), patch.object(tools, DETAIL, read):
            await self.request(message="分别核验本人计划的名称。")
        steps = self.complete("已核验 #101")
        self.evidence["physical_attempts"] = attempts
        self.assertEqual(attempts, [(101, 17), (101, 17)])
        first, retry = [row for row in steps if row["stage"] == "business_read"]
        self.assertEqual(retry["retry_of"], first["runtime_call_id"])
        reused, = [row for row in steps if row["stage"] == "tool_reuse"]
        self.assertEqual(reused["runtime_call_id"], retry["runtime_call_id"])
        self.assertNotEqual(reused["parent_call_id"], retry["parent_call_id"])
        self.assertEqual(len({row["artifact_id"] for row in self.main_receipts["batch"]["evidence"]}), 1)

    async def test_same_batch_renamed_dispatch_is_blocked_before_worker_creation(self):
        scope = [{"tool": DETAIL, "arguments": {"plan_id": 101}}]
        self.worker = lambda _, receipts: worker_summary("已读取") if receipts else reply(
            "", tool_call("scoped", name=DETAIL, plan_id=101))
        self.select(lambda receipts: reply("已核验 #101") if receipts else reply(
            "", dispatch("batch", {**task("first"), "read_scope": scope}, {**task("renamed"), "read_scope": scope})))
        await self.request(message="核验本人计划的名称。")
        steps = self.complete("已核验 #101")
        self.assertEqual(self.reads, [101])
        created, = [row for row in steps if row["stage"] == "worker" and row["action"] == "created"]
        self.assertEqual(created["detail"], "first")
        self.assertEqual(self.main_receipts["batch"]["gaps"][0]["error_code"], "duplicate_dispatch")

    async def test_worker_repeating_same_read_stops_before_model_budget(self):
        self.worker = lambda _, receipts: reply(
            "", tool_call(f"worker-{len(receipts)}", name=DETAIL, plan_id=101))
        self.select(lambda receipts: reply("已核验 #101") if receipts else reply("", dispatch("batch", task("repeat"))))
        await self.request(message="核验本人计划的名称。")
        steps = self.complete("已核验 #101")
        self.assertEqual(self.reads, [101])
        self.assertEqual(len(self.worker_inputs), 3)
        stop, = [row for row in steps if row["stage"] == "react_decision" and row["role"] == "team_worker"]
        self.assertEqual(stop["evidence"]["reason"], "no_progress")

    async def test_verified_handoff_advances_once_even_when_its_read_is_reused(self):
        def worker(inputs, receipts):
            if receipts:
                return worker_summary("已核验")
            selected = 102 if inputs.get("direct_inputs") else 101
            return reply("", tool_call("cached", name=DETAIL, plan_id=selected))

        def choose(receipts):
            if not receipts:
                return reply("", tool_call("a", name=DETAIL, plan_id=101), tool_call("b", name=DETAIL, plan_id=102))
            if "previous-reuse" not in receipts:
                return reply("", tool_call("previous-reuse", name=DETAIL, plan_id=102))
            if "handoff" not in receipts:
                consumer = {**task("uses-input"), "direct_refs": [receipts["a"]["artifact_id"]]}
                return reply("", dispatch("handoff", consumer))
            return reply("已核验两条计划")

        self.worker = worker
        self.select(choose)
        await self.request(message="核验两个计划的状态。")
        steps = self.complete("已核验两条计划")
        self.assertEqual(self.reads, [101, 102])
        decisions = [row["evidence"] for row in steps if row["stage"] == "react_decision"]
        self.assertEqual([(row["reason"], row["stalled_rounds"]) for row in decisions],
                         [("new_evidence", 0), ("no_progress_yet", 1), ("dependency_advanced", 0), ("model_answer", 0)])
        handoff, = [row["evidence"] for row in steps if row["stage"] == "dependency_progress"]
        self.assertEqual(handoff["input_artifact_ids"], [self.main_receipts["a"]["artifact_id"]])
        self.assertEqual(handoff["output_artifact_ids"], [self.main_receipts["b"]["artifact_id"]])

    def test_browser_scoring_keeps_multiline_status_with_its_plan(self):
        answer = "**计划 #101**\n- 编号：101\n- 全名：北岸搜索（下一条=102）\n- 投放状态：投放中\n\n" \
                 "**计划 #102**\n- 编号：102\n- 全名：南桥展示（下一条=103）\n- 投放状态：已暂停"
        plan = {"id": 101, "plan_name": "北岸搜索（下一条=102）", "status": "active"}
        self.assertTrue(has_plan_status(answer, plan))
        self.assertFalse(has_plan_status(answer, {**plan, "status": "paused"}))
        table = "| 101 | 北岸搜索（下一条=102） | active |\n| 102 | 南桥展示（下一条=103） | paused |"
        self.assertTrue(has_plan_status(table, plan))
        self.assertFalse(has_plan_status(table, {**plan, "status": "paused"}))

    async def test_scoped_dispatch_blocks_rename_and_admits_new_window_and_input(self):
        metric_reads = []
        self.evidence["metric_reads"] = metric_reads
        first_date, other_date = "2026-10-19", "2026-10-20"
        artifact, sql = metrics_fixture(self.database.parent, False)
        manifest = sql["manifest"]
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO plan_metric_bindings VALUES (101, ?, ?, ?)",
                         (manifest["source_id"], manifest["data_version"], SOURCE_PLAN))
        self.enterContext(patch.object(settings, "PLAN_METRICS_DB_PATH", str(artifact)))
        original = tools.get_plan_metrics_by_date

        def metrics(plan_id, start_date, end_date, user_id):
            metric_reads.append([plan_id, start_date, end_date])
            return original(plan_id, start_date, end_date, user_id)

        def read(date):
            return {"tool": METRICS, "arguments": {"plan_id": 101, "start_date": date, "end_date": date}}

        def scoped(task_id, date, refs=()):
            return {**task(task_id, tools=(METRICS,)), "read_scope": [read(date)], "direct_refs": list(refs)}

        def worker(inputs, receipts):
            action, = inputs["task"]["read_scope"]
            return worker_summary("核验已结束") if receipts else reply(
                "", tool_call("window", name=action["tool"], **action["arguments"]))

        def main(receipts):
            if not receipts:
                return reply("", dispatch("original", scoped("a", first_date)))
            if "repeat" not in receipts:
                renamed = scoped("new-name-and-id", first_date)
                renamed["read_scope"][0]["arguments"] = dict(reversed(list(read(first_date)["arguments"].items())))
                return reply("", dispatch("repeat", renamed), tool_call("context", name=DETAIL, plan_id=102))
            if "new" not in receipts:
                return reply("", dispatch("new", scoped("new-window", other_date),
                                          scoped("new-input", first_date, [receipts["context"]["artifact_id"]])))
            return reply("两个时间窗口的读取与缺项已核验。")

        self.worker = worker
        self.select(main)
        with patch.object(tools, METRICS, metrics):
            await self.request(message="分别核验计划的历史指标，补齐可取的证据。")
        steps = self.complete(self.events[-2]["content"])
        self.assertEqual(metric_reads, [[101, first_date, first_date], [101, other_date, other_date]])
        self.assertEqual({row["detail"] for row in steps if row["stage"] == "worker" and row["action"] == "created"},
                         {"a", "new-window", "new-input"})
        blocked, = [row for row in steps if row["stage"] == "duplicate_dispatch"]
        self.assertEqual(blocked["evidence"]["original_task_id"], "a")
        self.assertEqual(self.main_receipts["repeat"]["gaps"][0]["error_code"], "duplicate_dispatch")
        self.assertEqual(len(self.main_receipts["new"]["evidence"]), 2)

    async def test_transient_retry_can_execute_once_before_reuse(self):
        original = tools.get_plan_detail
        attempts = []
        self.evidence["detail_attempts"] = attempts

        def transient(plan_id, user_id):
            attempts.append((plan_id, user_id))
            if len(attempts) == 1:
                raise TimeoutError("fixture transient")
            return original(plan_id, user_id)

        self.select(lambda receipts: reply("已核验 #101") if len(receipts) == 2 else reply(
            "", tool_call(f"retry-{len(receipts)}", name=DETAIL, plan_id=101)))
        with patch.object(tools, DETAIL, transient):
            await self.request(message="核验本人计划的名称。")
        steps = self.complete("已核验 #101")
        self.assertEqual(attempts, [(101, 17), (101, 17)])
        first, retry = [row for row in steps if row["stage"] == "business_read"]
        self.assertEqual(retry["retry_of"], first["runtime_call_id"])
        self.assertEqual(self.main_receipts["retry-0"]["artifact_id"], self.main_receipts["retry-1"]["artifact_id"])

    async def test_correctable_arguments_report_field_and_allow_read(self):
        def select(receipts):
            if not receipts:
                return reply("", tool_call("bad", name=DETAIL, plan_id="101"))
            self.assertEqual(receipts["bad"]["error_code"], "invalid_tool_arguments")
            self.assertEqual(receipts["bad"]["errors"][0]["field"], "plan_id")
            return reply("", tool_call("corrected", name=DETAIL, plan_id=101))

        self.select(select)
        await self.request(message=ONE)
        self.assertEqual(self.reads, [101])
        _, steps = self.check_reply("goal_satisfied")
        self.assertTrue(any(row["stage"] == "react_decision" and row["evidence"]["reason"] == "correction_available"
                            for row in steps))

    async def test_model_budget_reserves_last_call_and_preserves_specific_gaps(self):
        self.sequential()
        with patch.object(self.server, "TeamBudget", return_value=TeamBudget(model_limit=4)):
            await self.request(message=SIX)
        self.assertEqual(self.reads, [101, 102])
        answer, steps = self.check_reply("model_budget_reserved_for_reply", remaining_model=1, missing=PLANS[2:])
        self.assertEqual(sum(row["stage"] == "model_call" for row in steps), 3)
        self.assertFalse(any(row["action"] == "main_summary" for row in steps))
        self.assertIn("模型额度", answer)
        self.assertIn("缺少 #106", answer)

    async def test_tool_budget_stops_without_another_tool_decision(self):
        self.sequential()
        with patch.object(self.server, "TeamBudget", return_value=TeamBudget(tool_limit=2)):
            await self.request(message=SIX)
        self.assertEqual(self.reads, [101, 102])
        self.assertEqual(self.decisions, self.reads)
        answer, _ = self.check_reply("tool_budget_exhausted", remaining_tool=0, missing=PLANS[2:])
        self.assertIn("读取额度", answer)
        self.assertIn("缺少 #103", answer)

    async def test_model_text_does_not_complete_unverified_scope(self):
        self.sequential(premature=True)
        await self.request(message=SIX)
        self.assertEqual(self.reads, list(PLANS))
        self.assertEqual(self.decisions, ["premature-text", *PLANS])
        self.check_reply("goal_satisfied")

    async def test_permanent_gap_leaves_independent_plans_executable(self):
        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM ad_plans WHERE id=101")
        self.sequential()
        await self.request(message=SIX)
        self.assertEqual(self.reads, list(PLANS))
        reply_text = self.events[-2]["content"]
        steps = self.complete(reply_text)
        decisions = [step["evidence"] for step in steps if step["stage"] == "react_decision"]
        self.assertEqual([row["stalled_rounds"] for row in decisions], [1, 0, 0, 0, 0, 0])
        self.assertEqual(decisions[-1]["reason"], "permanent_gaps")
        self.assertEqual(decisions[-1]["missing_plan_ids"], [101])
        self.assertEqual(len(decisions[-1]["artifact_ids"]), 5)
        self.assertIn("缺少 #101 的详情回执（无法核验该计划）", reply_text)
        self.assertIn("已核验 #106", reply_text)
