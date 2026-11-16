"""Exercise diagnosis guardrails through the authenticated ASGI SSE route."""

import asyncio
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from backend import observability
from backend.agent.agent_pool import AgentPool
from backend.agent.diagnosis import DIAGNOSIS_CALL_LIMIT
from backend.agent.llm_provider import ProviderFactory
from backend.agent.orchestrator import OrchestratorAgent
from backend.agent.tests.test_ad_diagnosis_eval import SEVEN_DAYS
from backend.agent.tests.test_home_sse_tracing import FixedModel, HomeSSEFixture, tool_call
from backend.database import database
from backend.tools import tools


MESSAGE = "复盘固定诊断样本"
RULE = "fixture-rule"
VERSIONED_RULE = "fixture-v1#ctr"
MODEL_REPLY = "固定模型声称单计划 CTR 为 99%"


def plan(call_id, plan_id=101, **args):
    return tool_call(call_id, name="get_plan_detail", plan_id=plan_id, **args)


def dispatch(call_id, query):
    return tool_call(call_id, name="query_data", query=query)


def peer(call_id, peer_name, question, **args):
    return tool_call(call_id, name="call_peer", peer_name=peer_name, question=question, **args)


def raw_call(call_id, name, arguments):
    call = tool_call(call_id, name=name)
    call.function.arguments = arguments
    return call


class GuardrailsModel(FixedModel):
    def __init__(self):
        super().__init__()
        self.dispatches = [dispatch("call_data", "data"), tool_call("call_knowledge", question="knowledge")]
        self.agent_calls = {"knowledge": [tool_call("call_search", query=RULE)]}
        self.agent_inputs = []
        self.protocol = []
        self.handoffs = {}
        self.leaf_inputs = {}
        self.after_handoff = []
        self.fail_after_dispatch = False

    def create(self, messages, tools=None, **kwargs):
        if kwargs.get("max_tokens") == 500:
            return self.response('{"steps": []}', [])
        names = {tool["function"]["name"] for tool in tools or []}
        receipts = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        self.receipts.update(receipts)
        if receipts:
            call_ids = [tc.id for message in messages for tc in message.get("tool_calls", [])]
            self.protocol.append((call_ids, list(receipts)))
        if "query_data" in names:
            if receipts:
                self.handoffs[messages[1]["content"]] = (kwargs.get("tool_choice"), receipts)
            if receipts and self.fail_after_dispatch:
                raise RuntimeError("private-model-failure")
            calls = self.after_handoff if receipts else self.dispatches
        elif "call_peer" in names:
            query = messages[-1]["content"]
            self.agent_inputs.append(query)
            calls = self.agent_calls.get(query, [])
        else:
            self.leaf_inputs[messages[1]["content"]] = receipts
            calls = []
        return self.response(None if calls else MODEL_REPLY, calls)


class DiagnosisGuardrailsTests(HomeSSEFixture):
    model_class = GuardrailsModel

    async def asyncSetUp(self):
        self.business_calls = []
        self.rule_id = RULE
        self.rule_evidence = []
        await super().asyncSetUp()
        database.init_db()
        self.enterContext(patch.object(tools, "get_db", self.connect))
        with closing(self.connect()) as conn, conn:
            conn.executemany("INSERT INTO ad_accounts (id, account_name, platform, user_id) VALUES (?, ?, 'test', ?)",
                             [(1, "private-owned-account", 17), (2, "private-other-account", 18)])
            conn.executemany("""INSERT INTO ad_plans (id, plan_name, account_id, user_id, impressions, clicks)
                VALUES (?, ?, ?, ?, 100, 5)""", [
                (101, "private-owned-plan", 1, 17), (102, "private-other-plan", 2, 18),
                (103, "private-cross-owner-plan", 2, 17),
            ])
            conn.executemany("""INSERT INTO daily_reports (report_date, user_id, impressions, clicks)
                VALUES (?, ?, ?, ?)""", [(window["start_date"], user, 200, clicks)
                                           for window in SEVEN_DAYS.values()
                                           for user, clicks in ((17, 10), (18, 90))])

    def tool_overrides(self):
        def query_plan(plan_id, user_id):
            self.business_calls.append(("plan", plan_id, user_id))
            return tools.get_plan_detail(plan_id, user_id)

        def query_report(start_date, end_date, user_id):
            self.business_calls.append(("report", start_date, end_date, user_id))
            return tools.get_daily_report_by_date(start_date, end_date, user_id)

        def query_rule(query):
            self.business_calls.append(("rule", query))
            return json.dumps({"context": "fixture rule", "sources": ["fixture-source"],
                               "evidence": self.rule_evidence})

        def dashboard():
            self.business_calls.append(("unexpected",))

        return {"get_plan_detail": query_plan, "get_daily_report_by_date": query_report,
                "search_knowledge": query_rule, "get_dashboard_data": dashboard}

    def assistant_replies(self):
        with closing(self.connect()) as conn:
            return [row["reply"] for row in conn.execute("SELECT reply FROM diagnosis_turns ORDER BY id")]

    async def diagnose(self, plan_id=101):
        await asyncio.wait_for(self.request(body={"message": MESSAGE, "diagnosis": {
            "plan_id": plan_id, "rule_id": self.rule_id, **SEVEN_DAYS,
        }}), timeout=10)
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        reply = self.events[1]["content"]
        self.assertIn("证据不足", reply)
        self.assertIn("历史 CTR", reply)
        self.assertNotIn(MODEL_REPLY, reply)
        self.assertEqual(self.terminal_observations, [("completed", [reply])])
        self.assertEqual(self.events[0]["task_id"], self.events[-1]["task_id"])
        for calls, receipts in self.model.protocol:
            self.assertEqual(calls, receipts)
        return reply

    def check_trace(self, expected, errors=None):
        errors = errors or {}
        self.assert_trace("completed", expected_calls=expected, failed_calls=errors)
        trace = json.loads(self.trace_file.read_text(encoding="utf-8"))
        actual = {step["tool_call_id"]: step["error_code"] for step in trace["steps"]
                  if step["stage"] == "tool_result" and "error_code" in step}
        self.assertEqual(actual, errors)
        self.assertEqual([(step["stage"], step.get("error_code")) for step in trace["steps"][-2:]],
                         [("guardrail", "insufficient_evidence"), ("persistence", None)])
        return trace["steps"]

    async def check_plan_scope(self, plan_id, account_name, *, denied=False):
        self.model.agent_calls["data"] = [plan("call_plan", str(plan_id))] + [
            tool_call(key, name="get_daily_report_by_date", **window) for key, window in SEVEN_DAYS.items()
        ]
        reply = await self.diagnose(plan_id)
        receipt = self.model.receipts["call_plan"]
        if denied:
            self.assertEqual(receipt, {"success": False, "message": "未找到"})
            self.assertIn("缺少成功回执（计划详情）", reply)
        else:
            self.assertEqual((receipt["id"], receipt["account_name"], receipt["ctr"]), (plan_id, account_name, 5))
            self.assertNotIn("缺少成功回执", reply)
        for key in SEVEN_DAYS:
            [row] = self.model.receipts[key]["reports"]
            self.assertEqual((row["user_id"], row["impressions"], row["clicks"]), (17, 200, 10))
            self.assertNotIn("plan_id", row)
        self.assertCountEqual(self.business_calls, [
            ("plan", str(plan_id), 17), ("rule", RULE),
            *(("report", window["start_date"], window["end_date"], 17) for window in SEVEN_DAYS.values()),
        ])
        self.check_trace({
            "call_data": ("query_data", "数据助手"), "call_plan": ("get_plan_detail", "数据助手"),
            "call_knowledge": ("query_knowledge_base", "知识助手"), "call_search": ("search_knowledge", "知识助手"),
            **{key: ("get_daily_report_by_date", "数据助手") for key in SEVEN_DAYS},
        }, {"call_plan": "tool_reported_failure"} if denied else {})

    async def test_real_tools_scope_plan_and_user_reports(self):
        await self.check_plan_scope(101, "private-owned-account")

    async def test_real_plan_tool_rejects_other_users_plan(self):
        await self.check_plan_scope(102, None, denied=True)

    async def test_real_plan_tool_redacts_cross_owner_account(self):
        await self.check_plan_scope(103, None)

    async def test_rejects_untrusted_arguments_before_executing(self):
        top = [
            (raw_call("top_json", "query_data", "{"), "invalid_tool_arguments"),
            (raw_call("top_array", "query_data", "[]"), "invalid_tool_arguments"),
            (tool_call("top_identity", name="query_data", query="data", user_id=18), "forbidden_user_id"),
            (tool_call("top_unknown", name="search_conversation_memory", query="private-query"), "unknown_tool"),
        ]
        data = [
            (plan("bad_identity", user_id=18), "forbidden_user_id"),
            (plan("wrong_plan", 102), "diagnosis_parameters_mismatch"),
            (plan("bad_type", True), "diagnosis_parameters_mismatch"),
            (tool_call("wrong_dates", name="get_daily_report_by_date", start_date="2026-01-01", end_date="2026-01-01"),
             "diagnosis_parameters_mismatch"),
            (raw_call("dup_identity", "get_plan_detail", '{"plan_id":102,"plan_id":101}'), "invalid_tool_arguments"),
            (tool_call("unlisted", name="get_dashboard_data"), "unknown_tool"),
            (peer("bad_peer", "private-missing-peer", "lookup"), "unknown_peer"),
            (peer("peer_identity", "query_knowledge_base", "lookup", user_id=18), "forbidden_user_id"),
        ]
        knowledge = [(tool_call("wrong_rule", name="search_knowledge", query="private-wrong-rule"),
                      "diagnosis_parameters_mismatch")]
        self.model.dispatches += [call for call, _ in top]
        self.model.agent_calls["data"] = [call for call, _ in data] + [plan("valid_plan")]
        self.model.agent_calls["knowledge"] = [call for call, _ in knowledge]
        reply = await self.diagnose()
        self.assertEqual(self.business_calls, [("plan", 101, 17)])
        self.assertCountEqual(self.model.agent_inputs, ["data", "knowledge"])
        self.assertIn("缺少成功回执（基准窗口用户日报、当前窗口用户日报、指定规则查询）", reply)
        expected = {"call_data": ("query_data", "数据助手"), "call_knowledge": ("query_knowledge_base", "知识助手"),
                    "valid_plan": ("get_plan_detail", "数据助手")}
        for rejected, role in ((top, "数据助手"), (data, "数据助手"), (knowledge, "知识助手")):
            for call, code in rejected:
                action = "unknown_tool" if code == "unknown_tool" else call.function.name
                expected[call.id] = (action, "orchestrator" if call.id == "top_unknown" else role)
                receipt = self.model.receipts[call.id]
                self.assertEqual(receipt.get("result", receipt), {"success": False, "error_code": code})
        self.check_trace(expected, {call.id: code for call, code in top + data + knowledge})

    async def test_oversized_dispatch_batch_is_limited_before_enqueue(self):
        count = DIAGNOSIS_CALL_LIMIT + 3
        self.model.dispatches = [dispatch(f"dispatch_{i}", f"data_{i}") for i in range(count)]
        self.model.agent_calls = {f"data_{i}": [plan(f"plan_{i}")] for i in range(count)}
        submitted = []
        submit = ThreadPoolExecutor.submit

        def record_submit(executor, fn, *args, **kwargs):
            submitted.append(kwargs["tool_call_id"])
            return submit(executor, fn, *args, **kwargs)

        with patch.object(ThreadPoolExecutor, "submit", record_submit):
            await self.diagnose()
        self.assertEqual(submitted, [f"dispatch_{i}" for i in range(DIAGNOSIS_CALL_LIMIT)])
        self.assertCountEqual(self.model.agent_inputs, [f"data_{i}" for i in range(DIAGNOSIS_CALL_LIMIT)])
        self.assertEqual(self.business_calls, [])
        self.check_trace({
            **{f"dispatch_{i}": ("query_data", "数据助手") for i in range(count)},
            **{f"plan_{i}": ("get_plan_detail", "数据助手") for i in range(DIAGNOSIS_CALL_LIMIT)},
        }, {key: "diagnosis_call_limit" for key in [
            *[f"dispatch_{i}" for i in range(DIAGNOSIS_CALL_LIMIT, count)],
            *[f"plan_{i}" for i in range(DIAGNOSIS_CALL_LIMIT)],
        ]})

    async def test_parallel_sub_agents_share_business_call_limit(self):
        self.model.dispatches = [dispatch("left", "left"), dispatch("right", "right")]
        self.model.agent_calls = {side: [plan(f"{side}_{i}") for i in range(DIAGNOSIS_CALL_LIMIT)]
                                  for side in ("left", "right")}
        await self.diagnose()
        self.assertEqual(Counter(self.business_calls), {("plan", 101, 17): DIAGNOSIS_CALL_LIMIT - 2})
        expected = {side: ("query_data", "数据助手") for side in ("left", "right")}
        expected.update({call.id: ("get_plan_detail", "数据助手")
                         for calls in self.model.agent_calls.values() for call in calls})
        self.check_trace(expected, {key: receipt["error_code"] for key, receipt in self.model.receipts.items()
                                    if receipt.get("error_code") == "diagnosis_call_limit"})

    async def test_peer_cycle_spends_the_same_limit(self):
        self.model.dispatches = [dispatch("dispatch", "cycle_0")]
        self.model.agent_calls = {f"cycle_{i}": [peer(f"peer_{i}",
                                                   "query_data" if i % 2 else "query_knowledge_base", f"cycle_{i+1}")]
                                  for i in range(DIAGNOSIS_CALL_LIMIT)}
        self.model.agent_calls["cycle_0"].insert(0, plan("first_plan"))
        await self.diagnose()
        self.assertEqual(self.business_calls, [("plan", 101, 17)])
        self.assertEqual(self.model.agent_inputs, [f"cycle_{i}" for i in range(DIAGNOSIS_CALL_LIMIT - 1)])
        self.check_trace({
            "dispatch": ("query_data", "数据助手"), "first_plan": ("get_plan_detail", "数据助手"),
            **{f"peer_{i}": ("call_peer", "知识助手" if i % 2 else "数据助手")
               for i in range(DIAGNOSIS_CALL_LIMIT - 1)},
        }, {f"peer_{DIAGNOSIS_CALL_LIMIT - 2}": "diagnosis_call_limit"})

    async def check_fallback(self, remaining, *, stream_failure=False):
        self.model.dispatches = [dispatch("dispatch", "data")]
        count = 0 if stream_failure else DIAGNOSIS_CALL_LIMIT - remaining - 1
        self.model.agent_calls["data"] = [plan(f"main_{i}") for i in range(count)]
        self.model.agent_calls[MESSAGE] = [plan(f"fallback_{i}") for i in range(remaining + 2)]
        self.model.fail_after_dispatch = True
        if stream_failure:
            self.enterContext(patch.object(OrchestratorAgent, "_create_plan", side_effect=RuntimeError))
        await self.diagnose()
        self.assertEqual(self.business_calls, [("plan", 101, 17)] * (count + max(remaining - 1, 0)))
        self.assertEqual(self.model.agent_inputs, ([] if stream_failure else ["data"]) + ([MESSAGE] if remaining else []))
        expected = {} if stream_failure else {"dispatch": ("query_data", "数据助手")}
        expected.update({f"main_{i}": ("get_plan_detail", "数据助手") for i in range(count)})
        if remaining:
            expected.update({f"fallback_{i}": ("get_plan_detail", "数据助手") for i in range(remaining + 2)})
        errors = {f"fallback_{i}": "diagnosis_call_limit" for i in range(remaining - 1, remaining + 2)} if remaining else {}
        [fallback] = [step for step in observability.get_trace(self.events[-1]["trace_id"])["steps"]
                      if step["stage"] == "fallback"]
        if remaining:
            fallback_id = fallback["runtime_call_id"]
            expected[fallback_id] = ("data_agent_reply", "orchestrator")
        steps = self.check_trace(expected, errors)
        self.assertEqual(fallback["error_code"] if not remaining else fallback["status"],
                         "diagnosis_call_limit" if not remaining else "ok")
        if remaining:
            self.assertTrue(all(step["parent_call_id"] == fallback_id for step in steps
                                if step.get("tool_call_id", "").startswith("fallback_")))

    async def test_reply_fallback_uses_remaining_budget(self):
        await self.check_fallback(3)

    async def test_exhausted_budget_blocks_fallback_entry(self):
        await self.check_fallback(0)

    async def test_early_stream_fallback_also_costs_a_call(self):
        await self.check_fallback(DIAGNOSIS_CALL_LIMIT, stream_failure=True)

    async def test_model_claim_cannot_replace_success_receipts(self):
        self.model.dispatches = []
        reply = await self.diagnose()
        self.assertIn("缺少成功回执（计划详情、基准窗口用户日报、当前窗口用户日报、指定规则查询）", reply)
        self.assertEqual(self.business_calls, [])
        self.check_trace({})

    def prepare_handoffs(self):
        self.rule_id = VERSIONED_RULE
        self.rule_evidence = [{"chunk_id": "fixture-ctr-v1", "rule_id": "fixture#ctr",
                               "version": "v1", "title": "CTR rule"}]
        self.model.dispatches = [
            dispatch("left", "left"), dispatch("right", "right"),
            tool_call("call_knowledge", question="knowledge"),
        ]
        self.model.agent_calls = {
            "left": [plan("call_plan"), tool_call("baseline", name="get_daily_report_by_date",
                                                 **SEVEN_DAYS["baseline"])],
            "right": [tool_call("current", name="get_daily_report_by_date", **SEVEN_DAYS["current"])],
            "knowledge": [tool_call("call_search", query=self.rule_id)],
        }
        self.model.after_handoff = [dispatch("unexpected_repeat", "left")]
        return [
            ("plan", 101, 17), ("rule", self.rule_id),
            *(("report", window["start_date"], window["end_date"], 17) for window in SEVEN_DAYS.values()),
        ]

    def handoffs(self, message=MESSAGE):
        tool_choice, receipts = self.model.handoffs[message]
        self.assertEqual(tool_choice, "none")
        return {call_id: receipt["receipts"] for call_id, receipt in receipts.items() if "receipts" in receipt}

    def fail_current_report(self, failures, on_recovery=None):
        real_report = tools.get_daily_report_by_date
        attempts = 0

        def report(start_date, end_date, user_id):
            nonlocal attempts
            if start_date == SEVEN_DAYS["current"]["start_date"]:
                attempts += 1
                if attempts <= failures:
                    raise TimeoutError("private-report-connection")
                if on_recovery and attempts == failures + 1:
                    on_recovery()
            return real_report(start_date, end_date, user_id)

        self.enterContext(patch.object(tools, "get_daily_report_by_date", report))

    def evidence_steps(self, events):
        trace = observability.get_trace(events[-1]["trace_id"])
        return [step for step in trace["steps"] if step["stage"].startswith("evidence_")]

    async def test_complete_first_batch_preserves_parallel_receipts_by_id(self):
        expected = self.prepare_handoffs()
        reply = await self.diagnose()
        self.assertCountEqual(self.business_calls, expected)
        handoffs = self.handoffs()
        self.assertEqual({key: [item["tool_call_id"] for item in items] for key, items in handoffs.items()},
                         {"left": ["call_plan", "baseline"], "right": ["current"], "call_knowledge": ["call_search"]})
        for key, window in (("left", "baseline"), ("right", "current")):
            report = handoffs[key][-1]
            [root] = [s for s in observability.get_trace(self.events[-1]["trace_id"])["steps"]
                      if s["stage"] == "tool_call" and s.get("tool_call_id") == key]
            self.assertEqual((report["scope"], report["arguments"], report["status"], report["parent_call_id"]),
                             ("user", SEVEN_DAYS[window], "ok", root["runtime_call_id"]))
        self.assertEqual(handoffs["call_knowledge"][0]["chunk_ids"], ["fixture-ctr-v1"])
        self.assertIn("日期报表按用户和日期汇总", reply)
        self.assertNotIn("本轮缺项", reply)
        self.assertEqual(self.evidence_steps(self.events)[-1]["action"], "receipts_complete")

    async def test_retries_only_temporary_failure_and_consumes_fresh_receipt(self):
        expected = self.prepare_handoffs()
        self.fail_current_report(1)
        reply = await self.diagnose()
        self.assertCountEqual(self.business_calls, [*expected, expected[-1]])
        failure, recovered = self.handoffs()["right"]
        self.assertEqual((failure["tool_call_id"], failure["status"], failure["error_code"]),
                         ("current", "temporary_failure", "temporary_tool_failure"))
        self.assertEqual((recovered["retry_of"], recovered["status"], recovered["arguments"]),
                         (failure["runtime_call_id"], "ok", SEVEN_DAYS["current"]))
        self.assertEqual(recovered["result"]["reports"][0]["clicks"], 10)
        self.assertNotIn("缺少成功回执", reply)
        failure_id, retry_id = failure["runtime_call_id"], recovered["runtime_call_id"]
        trace = json.loads(self.trace_file.read_text(encoding="utf-8"))
        self.assertNotIn("private-", json.dumps(trace))
        right_steps = [(s["stage"], s["runtime_call_id"], s.get("retry_of"), s["status"]) for s in trace["steps"]
                       if s.get("parent_call_id") == failure["parent_call_id"]]
        self.assertEqual(right_steps, [
            ("tool_call", failure_id, None, "ok"), ("tool_result", failure_id, None, "error"),
            ("evidence_received", failure_id, None, "error"),
            ("tool_call", retry_id, failure_id, "ok"), ("tool_result", retry_id, failure_id, "ok"),
            ("evidence_received", retry_id, failure_id, "ok"),
        ])
        self.assertEqual(self.evidence_steps(self.events)[-1]["action"], "receipts_complete")

    async def test_second_temporary_failure_stops_after_one_retry(self):
        expected = self.prepare_handoffs()
        self.fail_current_report(2)
        reply = await self.diagnose()
        self.assertCountEqual(self.business_calls, [*expected, expected[-1]])
        self.assertEqual([r["status"] for r in self.handoffs()["right"]], ["temporary_failure"] * 2)
        self.assertIn("当前窗口用户日报", reply)
        self.assertIn("补查后仍遇到临时故障，已停止查询", reply)

    async def test_empty_data_and_missing_exact_rule_stop_without_retry(self):
        expected = self.prepare_handoffs()
        self.rule_evidence[0]["version"] = "v2"
        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM daily_reports WHERE user_id=17 AND report_date=?",
                         (SEVEN_DAYS["current"]["start_date"],))
        reply = await self.diagnose()
        self.assertCountEqual(self.business_calls, expected)
        self.assertIn("当前窗口用户日报无数据", reply)
        self.assertIn("未找到指定规则的精确版本证据", reply)
        self.assertIn("已停止查询", reply)
        self.assertEqual(self.handoffs()["right"][0]["status"], "empty")
        self.assertEqual(self.handoffs()["call_knowledge"][0]["status"], "rule_not_found")
        self.assertEqual(self.events[1]["citations"], [])

    async def test_exhausted_budget_prevents_retry(self):
        expected = self.prepare_handoffs()
        repeat = DIAGNOSIS_CALL_LIMIT - len(self.model.dispatches) - len(expected)
        self.model.agent_calls["left"].extend(plan(f"extra_{i}") for i in range(repeat))
        self.fail_current_report(1)
        reply = await self.diagnose()
        self.assertCountEqual(self.business_calls, expected + [expected[0]] * repeat)
        self.assertIn("当前窗口用户日报", reply)
        self.assertIn("本轮工具执行额度已耗尽", reply)
        self.assertEqual([r["status"] for r in self.handoffs()["right"]], ["temporary_failure"])
        self.assertEqual([r["tool_call_id"] for r in self.handoffs()["left"] if r["scope"] == "plan"],
                         ["call_plan", *(f"extra_{i}" for i in range(repeat))])
        self.assertEqual(self.evidence_steps(self.events)[-1]["action"], "diagnosis_call_limit")

    async def test_diagnosis_peer_result_reaches_caller_and_root_dispatch(self):
        expected = self.prepare_handoffs()
        self.model.dispatches.pop()
        self.model.agent_calls["left"].append(peer("peer_rule", "query_knowledge_base", "knowledge"))
        await self.diagnose()
        self.assertCountEqual(self.business_calls, expected)
        self.assertEqual(self.model.leaf_inputs["left"]["peer_rule"], MODEL_REPLY)
        rule_receipt = self.handoffs()["left"][-1]
        [peer_step] = [s for s in observability.get_trace(self.events[-1]["trace_id"])["steps"]
                       if s["stage"] == "tool_call" and s.get("tool_call_id") == "peer_rule"]
        self.assertEqual((rule_receipt["tool_call_id"], rule_receipt["parent_call_id"], rule_receipt["chunk_ids"]),
                         ("call_search", peer_step["runtime_call_id"], ["fixture-ctr-v1"]))

    async def test_parallel_nested_reused_ids_trace_receipts_and_retry(self):
        expected = self.prepare_handoffs()
        data_calls = self.model.agent_calls.copy()
        self.model.dispatches = [tool_call(side, name="analyze_data", query=side) for side in ("left", "right")]
        self.model.agent_calls.update({
            "left": [peer("same-peer", "query_data", "left-data"),
                     peer("peer_rule", "query_knowledge_base", "knowledge")],
            "right": [peer("same-peer", "query_data", "right-data")],
            "left-data": [data_calls["left"][0], tool_call("same-leaf", name="get_daily_report_by_date",
                                                         **SEVEN_DAYS["baseline"])],
            "right-data": [tool_call("same-leaf", name="get_daily_report_by_date", **SEVEN_DAYS["current"])],
        })
        self.fail_current_report(1)
        report = tools.get_daily_report_by_date
        both_entered = threading.Event()
        initial_reports = threading.Barrier(2, action=both_entered.set)

        def parallel_report(start_date, end_date, user_id):
            if not both_entered.is_set():
                initial_reports.wait(timeout=5)
            return report(start_date, end_date, user_id)

        with patch.object(tools, "get_daily_report_by_date", parallel_report):
            await self.diagnose()
        self.assertTrue(both_entered.is_set())
        self.assertCountEqual(self.business_calls, [*expected, expected[-1]])
        for side in ("left", "right"):
            self.assertEqual(self.model.leaf_inputs[side]["same-peer"], MODEL_REPLY)
            self.assertIn("same-leaf", self.model.leaf_inputs[f"{side}-data"])

        steps = self.assert_trace("completed", expected_calls=None)
        calls = {s["runtime_call_id"]: s for s in steps if s["stage"] == "tool_call"}
        self.assertEqual(len(calls), 10)
        results = [s for s in steps if s["stage"] == "tool_result"]
        evidence = [s for s in steps if s["stage"] == "evidence_received"]
        for stage in ("tool_call", "tool_result"):
            self.assertCountEqual([s["runtime_call_id"] for s in steps if s["stage"] == stage], calls.keys())
        for result in results:
            call = calls[result["runtime_call_id"]]
            self.assertLess(steps.index(call), steps.index(result))
            for field in ("tool_call_id", "parent_call_id", "retry_of", "action", "role"):
                self.assertEqual(result.get(field), call.get(field))

        handoffs = self.handoffs()
        self.assertEqual(handoffs.keys(), {"left", "right"})
        plan_receipt, baseline, rule = handoffs["left"]
        failed, retried = handoffs["right"]
        for receipt, side, peer_id in ((plan_receipt, "left", "same-peer"), (baseline, "left", "same-peer"),
                                       (rule, "left", "peer_rule"), (failed, "right", "same-peer"),
                                       (retried, "right", "same-peer")):
            call = calls[receipt["runtime_call_id"]]
            parent = calls[call["parent_call_id"]]
            root = calls[parent["parent_call_id"]]
            self.assertEqual((parent["action"], parent["tool_call_id"]), ("call_peer", peer_id))
            self.assertEqual((root["action"], root["tool_call_id"]), ("analyze_data", side))
            [received] = [s for s in evidence if s["runtime_call_id"] == receipt["runtime_call_id"]]
            [result] = [s for s in results if s["runtime_call_id"] == receipt["runtime_call_id"]]
            self.assertLess(steps.index(result), steps.index(received))
            self.assertEqual((received["action"], received["detail"], received["status"]),
                             (receipt["tool"], receipt["status"], result["status"]))
            self.assertEqual(result["status"], "error" if receipt is failed else "ok")
            for field in ("tool_call_id", "parent_call_id", "retry_of", "error_code"):
                self.assertEqual(received.get(field), receipt[field])
                self.assertEqual(result.get(field), receipt[field])

        self.assertEqual(len(evidence), 5)
        self.assertEqual([r["tool_call_id"] for r in (baseline, failed, retried)], ["same-leaf", "same-leaf", None])
        self.assertEqual((baseline["status"], failed["status"], retried["status"]), ("ok", "temporary_failure", "ok"))
        self.assertEqual((failed["arguments"], retried["arguments"]), (SEVEN_DAYS["current"], SEVEN_DAYS["current"]))
        self.assertEqual(retried["retry_of"], failed["runtime_call_id"])
        self.assertEqual(retried["parent_call_id"], failed["parent_call_id"])
        self.assertEqual(failed["error_code"], "temporary_tool_failure")
        self.assertEqual(self.evidence_steps(self.events)[-1]["action"], "receipts_complete")

    async def test_concurrent_tasks_with_reused_call_ids_isolate_receipts(self):
        expected = self.prepare_handoffs()
        retry_started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def pause_retry():
            loop.call_soon_threadsafe(retry_started.set)
            if not release.wait(timeout=8):
                raise RuntimeError("test gate timed out")

        self.fail_current_report(1, pause_retry)
        with patch.object(ProviderFactory, "create", return_value=self.model):
            self.pool = AgentPool(min_size=2, max_size=2)
        self.enterContext(patch.object(self.server, "agent_pool", self.pool))

        async def run(message, events):
            await self.request(body={"message": message, "diagnosis": {
                "plan_id": 101, "rule_id": self.rule_id, **SEVEN_DAYS,
            }}, events=events)
            self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])

        a, b = [], []
        first = asyncio.create_task(run("复盘任务 A", a))
        try:
            await asyncio.wait_for(retry_started.wait(), timeout=5)
            await asyncio.wait_for(run("复盘任务 B", b), timeout=5)
        finally:
            release.set()
            await asyncio.wait_for(first, timeout=10)
        self.assertNotEqual(a[-1]["task_id"], b[-1]["task_id"])
        self.assertNotEqual(a[-1]["trace_id"], b[-1]["trace_id"])
        self.assertCountEqual(self.business_calls, expected * 2 + [expected[-1]])
        self.assertEqual([r["status"] for r in self.handoffs("复盘任务 A")["right"]], ["temporary_failure", "ok"])
        self.assertEqual([r["status"] for r in self.handoffs("复盘任务 B")["right"]], ["ok"])
        failed, _ = self.handoffs("复盘任务 A")["right"]
        self.assertEqual([s["retry_of"] for s in self.evidence_steps(a) if "retry_of" in s],
                         [failed["runtime_call_id"]])
        self.assertFalse([s for s in self.evidence_steps(b) if "retry_of" in s])
