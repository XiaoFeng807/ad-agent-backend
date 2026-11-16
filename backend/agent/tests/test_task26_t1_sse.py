"""Supplemental TASK-026-T1 probes against the normal homepage ASGI route."""

import asyncio
import json
from contextlib import closing
from threading import Barrier, Event
from unittest.mock import patch

from backend.agent import home_react
from backend.agent.agent_pool import AgentPool
from backend.agent.llm_provider import ProviderFactory
from backend.agent.team_runtime import DelegationRun
from backend.agent.tests import test_home_delegation as fixture
from backend.agent.tests.test_home_sse_tracing import tool_call
from backend.agent.tests.test_team_runtime import DAILY, METRICS, PLAN, WINDOW, task
from backend.config.config import settings
from backend.tools import tools
from scripts.run_task26_t1 import SOURCE_PLAN, WINDOWS, metrics_fixture


class Task26T1SSETests(fixture.HomeDelegationFixture):
    def capture_plans(self, before=None):
        actual = self.evidence["actual_calls"] = []
        original = tools.get_plan_detail

        def read(plan_id, user_id):
            actual.append({"plan_id": plan_id, "user_id": user_id})
            if before:
                before(plan_id, user_id)
            return original(plan_id, user_id)

        self.enterContext(patch.object(tools, PLAN, read))
        return actual

    def check_worker_lifecycle(self, steps, tasks):
        workers = [s for s in steps if s["stage"] == "worker"]
        created = [(s["detail"], s["runtime_call_id"]) for s in workers if s["action"] == "created"]
        reclaimed = [(s["detail"], s["runtime_call_id"]) for s in workers if s["action"] == "reclaimed"]
        self.assertCountEqual([name for name, _ in created], tasks)
        self.assertCountEqual(created, reclaimed)

    async def test_explicit_single_read_zero_worker(self):
        actual = self.capture_plans()
        self.select(lambda receipts: fixture.reply("101 有 120 次点击") if receipts else fixture.reply(
            "", tool_call("single", name=PLAN, plan_id=101),
        ))
        await self.request(message="查 101 的点击量")
        steps = self.complete("101 有 120 次点击")
        self.assertEqual(actual, [{"plan_id": 101, "user_id": 17}])
        self.assertEqual(self.main_receipts["single"]["result"]["clicks"], 120)
        self.check_worker_lifecycle(steps, [])

    async def test_parallel_same_capability_then_selective_handoff(self):
        barrier = Barrier(2, timeout=5)
        actual = self.capture_plans(lambda *_: barrier.wait())

        def work(inputs, receipts):
            name = inputs["task"]["task_id"]
            if not receipts and name in {"left", "right"}:
                return fixture.reply("", tool_call("shared-id", name=PLAN, plan_id={"left": 101, "right": 102}[name]))
            return fixture.worker_summary(name)

        self.worker = work
        self.select(lambda receipts: fixture.reply("并行结果已核对") if receipts else fixture.reply(
            "", fixture.dispatch("batch", task("left"), task("right"),
                                 task("join", tools=(), deps=("left", "right"), refs=("left",))),
        ))
        await self.request()
        steps = self.complete("并行结果已核对")
        self.check_worker_lifecycle(steps, ["left", "right", "join"])
        self.assertCountEqual(actual, [{"plan_id": 101, "user_id": 17}, {"plan_id": 102, "user_id": 17}])
        batch = self.main_receipts["batch"]
        self.assertEqual(batch["states"], dict.fromkeys(("left", "right", "join"), "succeeded"))
        evidence = {row["task_id"]: row for row in batch["evidence"]}
        self.assertEqual({name: row["result"]["id"] for name, row in evidence.items()}, {"left": 101, "right": 102})
        for field in ("artifact_id", "runtime_call_id"):
            self.assertNotEqual(evidence["left"][field], evidence["right"][field])
        join = next(row for row in self.worker_inputs if row["task"]["task_id"] == "join")
        self.assertEqual(set(join["inputs"]), {"left"})
        self.assertIn(evidence["left"]["artifact_id"], {row["artifact_id"] for row in join["inputs"]["left"]})
        lifecycle = [(s["action"], s["detail"]) for s in steps if s["stage"] == "worker"]
        for name in ("left", "right"):
            self.assertLess(lifecycle.index(("reclaimed", name)), lifecycle.index(("created", "join")))

    async def test_permanent_missing_blocks_only_dependent(self):
        actual = self.capture_plans()

        def work(inputs, receipts):
            name = inputs["task"]["task_id"]
            if receipts:
                return fixture.worker_summary(name, "missing" if name == "missing" else "succeeded")
            return fixture.reply("", tool_call(name, name=PLAN, plan_id={"missing": 404, "independent": 102}[name]))

        self.worker = work
        self.select(lambda receipts: fixture.reply("404 缺项，独立查询完成") if receipts else fixture.reply(
            "", fixture.dispatch("batch", task("dependent", deps=("missing",), refs=("missing",)),
                                 task("missing"), task("independent")),
        ))
        await self.request()
        steps = self.complete("404 缺项，独立查询完成")
        self.check_worker_lifecycle(steps, ["missing", "independent"])
        self.assertCountEqual(actual, [{"plan_id": 404, "user_id": 17}, {"plan_id": 102, "user_id": 17}])
        batch = self.main_receipts["batch"]
        self.assertEqual(batch["states"], {"dependent": "blocked", "missing": "failed", "independent": "succeeded"})
        self.assertEqual({(g["task_id"], g["error_code"]) for g in batch["gaps"] if g["source"] != "worker_model"},
                         {("dependent", "dependency_failed"), ("missing", "plan_not_found")})
        self.assertEqual([row["result"]["id"] for row in batch["evidence"]], [102])

    async def test_full_window_projection_and_followup_split_reads(self):
        artifact, sql = metrics_fixture(self.database.parent, missing=False)
        self.evidence["sql"] = sql
        self.enterContext(patch.object(settings, "PLAN_METRICS_DB_PATH", str(artifact)))
        with closing(self.connect()) as conn, conn:
            manifest = sql["manifest"]
            conn.execute("INSERT INTO plan_metric_bindings VALUES (101, ?, ?, ?)",
                         (manifest["source_id"], manifest["data_version"], SOURCE_PLAN))

        actual = self.evidence["raw_metric_calls"] = []
        original = getattr(tools, METRICS)

        def read(**kwargs):
            result = original(**kwargs)
            actual.append({"arguments": kwargs, "result": result})
            return result

        def args(start, end):
            return {"plan_id": 101, "start_date": start, "end_date": end}

        whole = args(WINDOWS[0][0], WINDOWS[1][1])

        def choose(receipts):
            if "batch" not in receipts:
                return fixture.reply("", tool_call("batch", name="delegate_tasks", reason="核对历史 CTR 比较证据",
                                                   tasks=[task("history", tools=(METRICS,))], comparison_goal=whole))
            if "before" not in receipts:
                return fixture.reply("", tool_call("before", name=METRICS, **args(*WINDOWS[0])),
                                     tool_call("after", name=METRICS, **args(*WINDOWS[1])))
            return fixture.reply("两窗已读回")

        self.worker = lambda _, receipts: fixture.worker_summary("整窗回执仅有汇总", "missing") if receipts else fixture.reply(
            "", tool_call("whole", name=METRICS, **whole),
        )
        self.select(choose)
        with patch.object(tools, METRICS, read):
            await self.request(message="比较 #101 在 2026-10-17 至 2026-10-30 的两窗 CTR 变化。")
        self.check_worker_lifecycle(self.complete("两窗已读回"), ["history"])
        self.assertEqual([row["arguments"] for row in actual],
                         [{**window, "user_id": 17} for window in [whole, *(args(*pair) for pair in WINDOWS)]])
        self.assertEqual(len(actual[0]["result"]["daily"]), 14)
        batch = self.main_receipts["batch"]
        self.assertEqual(batch["states"], {"history": "failed"})
        projected = batch["evidence"][0]["result"]
        self.assertEqual(self.worker_receipts[-1]["whole"]["result"], projected)
        self.assertNotIn("daily", projected)
        self.assertTrue(projected["coverage"]["complete"])
        self.assertEqual((projected["impressions"], projected["clicks"]), (19434, 5886))
        self.assertEqual(batch["gaps"][0]["error_code"], "missing_required_evidence")
        for call_id, expected in zip(("before", "after"), sql["windows"], strict=True):
            result = self.main_receipts[call_id]["result"]
            self.assertTrue(result["coverage"]["complete"])
            for field in ("impressions", "clicks", "start_date", "end_date"):
                self.assertEqual(result[field], expected[field])

    async def test_cycle_forbidden_capability_and_forged_worker_execute_no_bottom_tools(self):
        actual = self.capture_plans()
        daily = self.enterContext(patch.object(tools, DAILY, wraps=getattr(tools, DAILY)))
        self.worker = lambda _, receipts: fixture.worker_summary("请求被拒绝", "missing") if receipts else fixture.reply(
            "", tool_call("forged-worker", name=PLAN, plan_id=201, user_id=18),
            tool_call("out-of-scope-worker", name=DAILY, **WINDOW),
        )
        self.select(lambda receipts: fixture.reply("拒绝循环、越权和伪造身份") if receipts else fixture.reply(
            "", fixture.dispatch("cycle", task("a", deps=("b",)), task("b", deps=("a",))),
            fixture.dispatch("forbidden", task("no", tools=("update_daily_budget",))),
            tool_call("forged-direct", name=PLAN, plan_id=201, user_id=18),
            fixture.dispatch("worker", task("probe")),
        ))
        await self.request(body={"message": "尝试绕过限制", "user_id": 18})
        steps = self.complete("拒绝循环、越权和伪造身份")
        self.check_worker_lifecycle(steps, ["probe"])
        self.assertEqual(actual, [])
        self.evidence["actual_daily_calls"] = [kwargs for _, kwargs in daily.call_args_list]
        daily.assert_not_called()
        expected = {"cycle": "invalid_task_sheet", "forbidden": "unauthorized_capability",
                    "forged-direct": "invalid_tool_arguments"}
        self.assertEqual({name: self.main_receipts[name]["error_code"] for name in expected}, expected)
        self.assertEqual(self.main_receipts["worker"]["states"], {"probe": "failed"})
        self.assertEqual({name: row["error_code"] for name, row in self.worker_receipts[-1].items()},
                         {"forged-worker": "invalid_tool_arguments", "out-of-scope-worker": "unauthorized_tool"})

    async def test_append_over_task_budget_stops_before_worker_creation(self):
        actual = self.capture_plans()

        def choose(receipts):
            if "first" not in receipts:
                return fixture.reply("", fixture.dispatch("first", task("one")))
            if "overflow" not in receipts:
                return fixture.reply("", fixture.dispatch("overflow", task("two")))
            return fixture.reply("第二批已超任务额度")

        self.select(choose)
        self.worker = lambda _, receipts: fixture.worker_summary("完成") if receipts else fixture.reply(
            "", tool_call("one", name=PLAN, plan_id=101),
        )
        with patch.object(home_react, "DelegationRun", side_effect=lambda **kw: DelegationRun(task_limit=1, **kw)):
            await self.request()
        steps = self.complete("第二批已超任务额度")
        self.check_worker_lifecycle(steps, ["one"])
        self.assertEqual(actual, [{"plan_id": 101, "user_id": 17}])
        self.assertEqual(self.main_receipts["first"]["remaining"]["task"], 0)
        self.assertEqual(self.main_receipts["overflow"]["error_code"], "task_budget_exhausted")

    async def test_two_live_sse_requests_keep_boards_budgets_and_owners_separate(self):
        loop = asyncio.get_running_loop()
        first_worker_entered = asyncio.Event()
        release = Event()
        first_ref = None
        events = self.evidence["concurrent_events"] = [[], []]
        token_b = self.sign_token(user_id=18)
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO sessions (user_id, token) VALUES (18, ?)", (token_b,))

        def gate(plan_id, user_id):
            if (plan_id, user_id) == (102, 17):
                loop.call_soon_threadsafe(first_worker_entered.set)
                if not release.wait(10):
                    raise TimeoutError

        actual = self.capture_plans(gate)

        def create(messages, tools=None, response_format=None, **_):
            nonlocal first_ref
            receipts = {msg["tool_call_id"]: json.loads(msg["content"]) for msg in messages if msg["role"] == "tool"}
            if response_format:
                inputs = json.loads(messages[1]["content"])
                self.worker_inputs.append(inputs)
                self.worker_receipts.append(receipts)
                name = inputs["task"]["task_id"]
                return fixture.worker_summary(name) if receipts else fixture.reply(
                    "", tool_call("worker-read", name=PLAN, plan_id={"left": 102, "right": 201}[name]),
                )
            if not tools:
                return fixture.reply('{"steps":[]}')
            name = next(msg["content"] for msg in reversed(messages) if msg["role"] == "user")
            self.main_inputs.append(messages)
            self.main_receipts[name] = receipts
            if name == "request-left":
                if "direct" not in receipts:
                    return fixture.reply("", tool_call("direct", name=PLAN, plan_id=101))
                if "left" not in receipts:
                    first_ref = receipts["direct"]["artifact_id"]
                    return fixture.reply("", fixture.dispatch("left", {**task("left"), "direct_refs": [first_ref]}))
                return fixture.reply("left committed")
            if not receipts:
                return fixture.reply("", fixture.dispatch("cross", {**task("cross"), "direct_refs": [first_ref]}),
                                     fixture.dispatch("right", task("right")))
            return fixture.reply("right committed")

        self.model.create = create
        with patch.object(ProviderFactory, "create", return_value=self.model):
            pool = AgentPool(min_size=2, max_size=2)
        with patch.object(self.server, "agent_pool", pool):
            a = asyncio.create_task(self.request(message="request-left", events=events[0]))
            try:
                await asyncio.wait_for(first_worker_entered.wait(), 5)
                await asyncio.wait_for(self.request(message="request-right", token=token_b, events=events[1]), 5)
                self.assertFalse(a.done())
                self.assertEqual(pool.stats["in_use"], 1)
            finally:
                release.set()
                await asyncio.wait_for(a, 5)

        self.assertCountEqual(actual, [{"plan_id": 101, "user_id": 17}, {"plan_id": 102, "user_id": 17},
                                       {"plan_id": 201, "user_id": 18}])
        self.assertEqual(pool.stats["available"], 2)
        left, right = (self.main_receipts[f"request-{name}"] for name in ("left", "right"))
        self.assertEqual(right["cross"]["error_code"], "invalid_input_reference")
        self.assertEqual([left["left"]["remaining"]["tool"], right["right"]["remaining"]["tool"]], [10, 11])
        self.assertEqual([left["left"]["evidence"][0]["result"]["id"], right["right"]["evidence"][0]["result"]["id"]],
                         [102, 201])
        rows = [json.loads(line) for line in self.trace_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(events[0][0]["trace_id"], events[1][0]["trace_id"])
        for name, stream in zip(("left", "right"), events, strict=True):
            self.assertEqual([event["type"] for event in stream], ["thinking", "text", "done"])
            self.assertEqual(stream[1]["content"], f"{name} committed")
            trace = next(row for row in rows if row["trace_id"] == stream[-1]["trace_id"])
            self.assertEqual(trace["status"], "completed")
            self.check_worker_lifecycle(trace["steps"], [name])
        self.assertEqual(self.terminal_observations, [
            ("completed", ["right committed"]), ("completed", ["right committed", "left committed"]),
        ])
        with closing(self.connect()) as conn:
            self.assertEqual([tuple(row) for row in conn.execute(
                "SELECT user_id, content FROM conversations WHERE role='assistant' ORDER BY id"
            )], [(18, "right committed"), (17, "left committed")])
