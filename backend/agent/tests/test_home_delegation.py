"""ASGI regressions for the shared ordinary-chat ReAct execution."""

import asyncio
import json
import os
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

from backend.agent import home_react
from backend.agent.team_runtime import DelegationRun
from backend.agent.team_tools import TeamBudget
from backend.agent.tests.test_home_sse_tracing import FixedModel, HomeSSEFixture, tool_call
from backend.agent.tests.test_team_runtime import DAILY, METRICS, PLAN, WINDOW, task
from backend.tools import tools


def dispatch(call_id, *tasks):
    return tool_call(call_id, name="delegate_tasks", reason="Fill the remaining evidence gaps", tasks=tasks)


def reply(text="", *calls):
    return FixedModel.response(text or None, list(calls))


def worker_summary(output, status="succeeded"):
    return reply(json.dumps({"status": status, "output": output}))


class HomeDelegationFixture(HomeSSEFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enterContext(patch.object(tools, "get_db", self.connect))
        self.enterContext(patch("backend.config.config.settings.PLAN_METRICS_DB_PATH", ""))
        with closing(self.connect()) as conn, conn:
            conn.executemany("INSERT INTO ad_plans (id, user_id, plan_name, impressions, clicks) "
                             "VALUES (?, ?, ?, 1500, 120)", [
                                 (101, 17, "owned-plan-a"), (102, 17, "owned-plan-b"), (201, 18, "foreign-plan"),
                             ])
        self.worker_inputs = []
        self.worker_receipts = []
        self.main_inputs = []
        self.main_receipts = {}
        self.evidence = {}
        self.worker = lambda _, __: worker_summary("handoff complete")

    def archive_traces(self, _, source):
        if (directory := os.environ.get("AD_TEAM_TEST_EVIDENCE")) and source.exists():
            evidence = {"traces": [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()],
                        "main_receipts": self.main_receipts, "worker_inputs": self.worker_inputs,
                        "worker_receipts": self.worker_receipts,
                        "main_inputs": self.main_inputs, "events": self.events,
                        "terminal_observations": self.terminal_observations,
                        "replies": self.assistant_replies(), **self.evidence}
            (Path(directory) / f"{self._testMethodName}.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
            )

    def select(self, main):
        def create(messages, tools=None, response_format=None, **_):
            receipts = {msg["tool_call_id"]: json.loads(msg["content"]) for msg in messages if msg["role"] == "tool"}
            if response_format:
                inputs = json.loads(messages[1]["content"])
                self.worker_inputs.append(inputs)
                self.worker_receipts.append(receipts)
                return self.worker(inputs, receipts)
            if not tools and messages[0]["content"] == "你是一个任务规划专家, 输出JSON格式计划。":
                return reply('{"steps":[]}')
            self.main_inputs.append(messages)
            self.main_receipts.update(receipts)
            return main(receipts)
        self.model.create = create

    def complete(self, text):
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        self.assertEqual(self.assistant_replies(), [text])
        self.assertEqual(self.terminal_observations, [("completed", [text])])
        return self.assert_trace("completed", expected_calls=None)


class HomeDelegationTests(HomeDelegationFixture):
    async def test_pure_answer_and_explicit_multi_read_create_no_workers(self):
        self.select(lambda _: reply("CTR 是点击数除以曝光数。"))
        await self.request(message="CTR 是什么？")
        steps = self.complete("CTR 是点击数除以曝光数。")
        self.assertFalse(any(s["stage"] in {"worker", "tool_call"} for s in steps))

        self.events = []
        self.select(lambda receipts: reply("两条计划各有 120 次点击。") if receipts else reply(
            "", tool_call("a", name=PLAN, plan_id=101), tool_call("b", name=PLAN, plan_id=102),
        ))
        await self.request(message="查 101 和 102 的点击量")
        self.assertEqual(self.assistant_replies()[-1], "两条计划各有 120 次点击。")
        self.assertEqual(self.events[-1]["type"], "done")
        trace = self.trace_file.read_text(encoding="utf-8").splitlines()[-1]
        self.assertNotIn('"worker"', trace)
        self.assertEqual([row["result"]["id"] for row in self.main_receipts.values()], [101, 102])
        continuation = self.main_inputs[-1][-3:]
        self.assertEqual([call["id"] for call in continuation[0]["tool_calls"]], ["a", "b"])
        self.assertEqual([message["tool_call_id"] for message in continuation[1:]], ["a", "b"])

    async def test_append_reuses_direct_and_worker_artifacts_after_reclaim(self):
        def choose(receipts):
            if "direct" not in receipts:
                return reply("", tool_call("direct", name=PLAN, plan_id=101))
            if "batch-1" not in receipts:
                return reply("", dispatch("batch-1", {**task("lookup"),
                    "direct_refs": [receipts["direct"]["artifact_id"]]}))
            if "batch-2" not in receipts:
                artifact = receipts["batch-1"]["evidence"][0]
                return reply("", dispatch("batch-2", task("join", tools=(), deps=("lookup",),
                    artifacts=({"task_id": "lookup", "artifact_id": artifact["artifact_id"]},))))
            return reply("两批证据已核对")

        self.worker = lambda inputs, receipts: (
            reply("", tool_call("worker-plan", name=PLAN, plan_id=102))
            if inputs["task"]["task_id"] == "lookup" and not receipts else worker_summary("已核对")
        )
        self.select(choose)
        await self.request()
        steps = self.complete("两批证据已核对")
        starts = [(i, step) for i, step in enumerate(steps) if step["stage"] == "batch" and step["action"] == "started"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0][1]["detail"], starts[1][1]["detail"])
        reclaimed = next(i for i, s in enumerate(steps) if s["stage"] == "worker" and s["action"] == "reclaimed")
        self.assertLess(reclaimed, starts[1][0])
        self.assertEqual(self.worker_inputs[0]["direct_inputs"][0]["result"]["id"], 101)
        join = self.worker_inputs[-1]
        self.assertEqual(set(join["inputs"]), {"lookup"})
        [upstream] = join["inputs"]["lookup"]
        self.assertEqual((upstream["source"], upstream["result"]["id"]), (PLAN, 102))
        self.assertNotIn("direct_inputs", join)
        self.assertEqual(self.main_receipts["batch-2"]["evidence"], [])
        self.assertEqual(self.main_receipts["batch-2"]["summaries"][0]["output"], "已核对")
        self.assertEqual(self.main_receipts["batch-2"]["remaining"]["tool"], 10)
        first_remaining = self.main_receipts["batch-1"]["remaining"]
        second_remaining = self.main_receipts["batch-2"]["remaining"]
        self.assertEqual(second_remaining["model"], first_remaining["model"] - 2)
        self.assertEqual(second_remaining["task"], first_remaining["task"] - 1)
        first_read = next(i for i, s in enumerate(steps) if s["stage"] == "tool_result" and s["tool_call_id"] == "direct")
        self.assertLess(first_read, starts[0][0])

    async def test_single_failed_item_retried_with_original_parameters(self):
        actual = []
        self.evidence["actual_calls"] = actual
        original = tools.get_plan_detail

        def read(plan_id, user_id):
            actual.append((plan_id, user_id))
            if plan_id == 102 and actual.count((102, 17)) == 1:
                raise TimeoutError
            return original(plan_id, user_id)

        self.worker = lambda _, receipts: worker_summary("查询完成") if receipts else reply(
            "", tool_call("one", name=PLAN, plan_id=101), tool_call("two", name=PLAN, plan_id=102),
        )
        self.select(lambda receipts: reply("两项已读回") if receipts else reply("", dispatch("batch", task("read"))))
        with patch.object(tools, PLAN, read):
            await self.request()
        steps = self.complete("两项已读回")
        self.assertEqual(actual, [(101, 17), (102, 17), (102, 17)])
        self.assertEqual(len(self.main_receipts["batch"]["evidence"]), 2)
        self.assertEqual(self.main_receipts["batch"]["gaps"], [])
        [retry] = [step for step in steps if step["stage"] == "tool_call" and "retry_of" in step]
        self.assertTrue(any(s.get("runtime_call_id") == retry["retry_of"] and
                            s.get("error_code") == "temporary_tool_failure" for s in steps))

    async def test_rejects_cross_request_reference_foreign_plan_and_injected_owner(self):
        self.select(lambda receipts: reply("首轮完成") if receipts else reply("", tool_call("a", name=PLAN, plan_id=101)))
        await self.request()
        ref = self.main_receipts["a"]["artifact_id"]
        self.events = []

        def choose(receipts):
            if not receipts:
                return reply("", dispatch("foreign-ref", {**task("read"), "direct_refs": [ref]}),
                             tool_call("foreign-plan", name=PLAN, plan_id=201),
                             tool_call("injected", name=PLAN, plan_id=201, user_id=18))
            return reply("无法读取指定计划")

        self.select(choose)
        await self.request(body={"message": "尝试读引用和计划", "user_id": 18})
        self.assertEqual(self.events[-1]["type"], "done")
        self.assertEqual(self.main_receipts["foreign-ref"]["error_code"], "invalid_input_reference")
        self.assertEqual(self.main_receipts["foreign-plan"]["error_code"], "plan_not_found")
        self.assertEqual(self.main_receipts["injected"]["error_code"], "invalid_tool_arguments")
        self.assertEqual(self.worker_inputs, [])
        self.assertNotIn("foreign-plan", json.dumps(self.main_receipts["foreign-plan"]))

        other_token = self.sign_token(user_id=18)
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO sessions (user_id, token) VALUES (?, ?)", (18, other_token))
        self.events = []
        self.select(lambda receipts: reply("只读取了当前用户的计划") if receipts else reply(
            "", dispatch("other-ref", {**task("read"), "direct_refs": [ref]}),
            tool_call("other-plan", name=PLAN, plan_id=101), tool_call("own-plan", name=PLAN, plan_id=201),
        ))
        await self.request(token=other_token)
        self.assertEqual(self.events[-1]["type"], "done")
        self.assertEqual(self.main_receipts["other-ref"]["error_code"], "invalid_input_reference")
        self.assertEqual(self.main_receipts["other-plan"]["error_code"], "plan_not_found")
        self.assertEqual(self.main_receipts["own-plan"]["result"]["id"], 201)
        self.assertEqual(self.worker_inputs, [])

    async def test_reports_missing_capabilities_and_real_missing_receipts(self):
        def choose(receipts):
            if not receipts:
                return reply("", dispatch("denied", task("write", tools=("update_daily_budget",))),
                             dispatch("batch", task("history", tools=(METRICS, DAILY))))
            return reply("没有计划历史，且所查日期没有日报，无法判断原因。")

        self.worker = lambda _, receipts: worker_summary("历史与日报缺失", "missing") if receipts else reply(
            "", tool_call("metrics", name=METRICS, plan_id=101, **WINDOW), tool_call("daily", name=DAILY, **WINDOW),
        )
        self.select(choose)
        await self.request()
        self.complete("没有计划历史，且所查日期没有日报，无法判断原因。")
        self.assertEqual(self.main_receipts["denied"]["error_code"], "unauthorized_capability")
        batch = self.main_receipts["batch"]
        self.assertEqual(batch["evidence"], [])
        self.assertEqual(batch["summaries"][0]["status"], "missing")
        gaps = {row["source"]: row["error_code"] for row in batch["gaps"]}
        self.assertEqual(gaps[METRICS], "plan_history_not_mapped")
        self.assertEqual(gaps[DAILY], "daily_reports_missing")

    async def test_tool_budget_shared_by_direct_and_worker_with_single_final_reply(self):
        actual = []
        self.evidence["actual_calls"] = actual
        original = tools.get_plan_detail

        def read(plan_id, user_id):
            actual.append(plan_id)
            return original(plan_id, user_id)

        def choose(receipts):
            if not receipts:
                return reply("", tool_call("direct", name=PLAN, plan_id=101))
            if "batch" not in receipts:
                return reply("", dispatch("batch", task("lookup")))
            return reply("已查两项，第三项因预算未执行")

        self.worker = lambda _, receipts: worker_summary("第三项缺失", "missing") if receipts else reply(
            "", tool_call("second", name=PLAN, plan_id=102), tool_call("third", name=PLAN, plan_id=103),
        )
        self.select(choose)
        with patch.object(self.server, "TeamBudget", return_value=TeamBudget(tool_limit=2)), patch.object(tools, PLAN, read):
            await self.request()
        self.complete("已查两项，第三项因预算未执行")
        self.assertEqual(actual, [101, 102])
        batch = self.main_receipts["batch"]
        self.assertEqual(batch["remaining"]["tool"], 0)
        self.assertTrue(any(gap["error_code"] == "budget_exhausted_or_cancelled" for gap in batch["gaps"]))

    async def test_model_budget_reserves_final_main_call(self):
        self.select(lambda receipts: reply("只完成部分证据") if receipts else reply("", dispatch("batch", task("lookup"))))
        self.worker = lambda *_: reply("", tool_call("worker-read", name=PLAN, plan_id=101))
        with patch.object(self.server, "TeamBudget", return_value=TeamBudget(model_limit=4)):
            await self.request()
        steps = self.complete("只完成部分证据")
        self.assertEqual(sum(s["stage"] == "model_call" for s in steps), 4)
        self.assertEqual(self.main_receipts["batch"]["gaps"][0]["error_code"], "model_budget_exhausted")
        self.assertTrue(any(s["stage"] == "model_call" and s["action"] == "main_summary" for s in steps))

    async def test_deadline_rejects_direct_receipt_and_stops_the_model_loop(self):
        now = 100
        original = tools.get_plan_detail

        def read(plan_id, user_id):
            nonlocal now
            result = original(plan_id, user_id)
            self.evidence["late_tool_arguments"] = {"plan_id": plan_id, "user_id": user_id}
            now += 91
            return result

        self.select(lambda receipts: reply("", dispatch("batch", task("lookup"))) if receipts else reply(
            "", tool_call("direct", name=PLAN, plan_id=101),
        ))
        with (patch("backend.agent.team_tools.monotonic", side_effect=lambda: now),
              patch.object(tools, PLAN, read)):
            await self.request()
        self.assertEqual([event["type"] for event in self.events], ["thinking", "error"])
        self.assertEqual(self.events[-1]["error_code"], "deadline_exceeded")
        self.assertEqual(self.assistant_replies(), [])
        steps = self.assert_trace("error", expected_calls=None)
        self.assertEqual([s["action"] for s in steps if s["stage"] == "tool_call"], [PLAN])
        self.assertTrue(any(s["stage"] == "tool_result" and s.get("error_code") == "expired_receipt" for s in steps))
        self.assertFalse(any(s["stage"] in {"worker", "receipt"} for s in steps))

    async def test_disconnect_stops_queue_and_drains_worker_before_agent_release(self):
        entered, resume = asyncio.Event(), Event()
        loop = asyncio.get_running_loop()
        original = tools.get_plan_detail
        execution = TeamBudget()

        def read(plan_id, user_id):
            self.evidence["late_tool_arguments"] = {"plan_id": plan_id, "user_id": user_id}
            loop.call_soon_threadsafe(entered.set)
            if not resume.wait(5):
                raise TimeoutError
            return original(plan_id, user_id)

        self.worker = lambda *_: reply("", tool_call("worker-plan", name=PLAN, plan_id=101))
        self.select(lambda _: reply("", dispatch("batch", task("running"), task("queued"))))
        with (patch.object(self.server, "TeamBudget", return_value=execution), patch.object(tools, PLAN, read),
              patch.object(home_react, "DelegationRun", side_effect=lambda **kw: DelegationRun(concurrency=1, **kw))):
            request = asyncio.create_task(self.request())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.disconnect.set()
                await asyncio.wait_for(self.disconnect_seen.wait(), 5)
                self.assertEqual(execution.stop_code(), "run_cancelled")
                self.assertEqual(self.pool.stats["in_use"], 1)
            finally:
                resume.set()
                await asyncio.wait_for(request, 5)
        self.assertEqual([event["type"] for event in self.events], ["thinking"])
        self.assertEqual(self.assistant_replies(), [])
        steps = self.assert_trace("interrupted", expected_calls=None)
        self.assertEqual([(s["action"], s["detail"]) for s in steps if s["stage"] == "worker"],
                         [("created", "running"), ("reclaimed", "running")])
        self.assertTrue(any(s["stage"] == "receipt" and s["action"] == "rejected" and
                            s.get("error_code") == "expired_receipt" for s in steps))
