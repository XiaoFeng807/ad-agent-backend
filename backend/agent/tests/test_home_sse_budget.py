"""Capture homepage model calls at the SDK transport and audit the budget independently."""

from collections import Counter
from concurrent.futures import Future
from contextlib import ExitStack, closing
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from backend.agent import orchestrator as orchestration
from backend.agent.agent_pool import AgentPool
from backend.agent.diagnosis import DIAGNOSIS_REQUEST
from backend.agent.llm_provider import ProviderFactory
from backend.agent.request_budget import RequestBudget
from backend.agent.tests.test_ad_diagnosis_eval import SEVEN_DAYS
from backend.agent.tests.test_home_sse_tracing import FixedModel, HomeSSEFixture, tool_call
from backend.agent.tests.test_plan_metrics import synthetic_artifact
from backend.agent.tests.test_provider_contract import SDKCapture
from backend.config.config import settings
from backend.database.diagnosis_tasks import accept_diagnosis
from backend.memory import memory_manager
from backend.rag.review_rule_demo import DEMO_RULES
from backend.tools import tools


QUESTION = "budget test question"
REPLY = "固定预算测试回复"
OVERSIZED = "长" * 12000
DIAGNOSIS = {"plan_id": 101, "rule_id": "fixture-rule", **SEVEN_DAYS}


def response(text=REPLY, *calls):
    return FixedModel.response(text, calls)


class HomeSSEBudgetTests(HomeSSEFixture):
    def tool_overrides(self):
        return {"get_dashboard_data": lambda user_id=None: self.receipt,
                "get_plan_detail": lambda plan_id, user_id=None: self.receipt}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enterContext(patch.object(tools, "get_db", self.connect))
        self.receipt = {"value": "完整回执"}
        self.responder = lambda **_: response()

        def complete(**body):
            if "任务规划专家" in body["messages"][0]["content"]:
                return response('{"steps": []}')
            return self.responder(**body)

        self.capture = SDKCapture(SimpleNamespace(create=complete))
        self.capture.install(self.enterContext(ExitStack()), "deepseek")
        self.pool = AgentPool(min_size=1, max_size=1)
        self.server.agent_pool = self.pool
        self.orchestrator = self.pool._pool[0].orchestrator
        if archive := getattr(self, "archive_model_inputs", None):
            self.addCleanup(lambda: archive(self.id(), self.capture.requests))

    def audit(self, expected_actions, *, events=None, requests=None, limit=32000):
        events = self.events if events is None else events
        requests = self.capture.requests if requests is None else requests
        trace = next(json.loads(line) for line in self.trace_file.read_text(encoding="utf-8").splitlines()
                     if json.loads(line)["trace_id"] == events[-1]["trace_id"])
        steps = [step for step in trace["steps"] if step["stage"] == "context_budget"]
        allowed = [step for step in steps if step["status"] == "ok"]
        self.assertCountEqual([step["action"] for step in allowed], expected_actions)
        sdk_counts = Counter()
        for request in requests:
            body = request["body"]
            # Count actual serialized SDK body bytes without the production estimator.
            count = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            output = body["max_tokens"]
            sdk_counts[count, output] += 1
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertLessEqual(count + output + 512, limit)
            self.assertLessEqual(output, 3000)
            pending = []
            for message in body["messages"]:
                self.assertNotIn("priority", message)
                if message["role"] == "tool":
                    self.assertIn(message["tool_call_id"], pending)
                    pending.remove(message["tool_call_id"])
                else:
                    self.assertEqual(pending, [])
                    pending = [call["id"] for call in message.get("tool_calls", [])]
            self.assertEqual(pending, [])
        self.assertEqual(sdk_counts, Counter((s["budget"]["input_tokens"], s["budget"]["output_reserved"])
                                             for s in allowed))
        for step in steps:
            stats = step["budget"]
            self.assertEqual(stats["estimator"], "utf8_json_bytes_v1")
            self.assertEqual((stats["limit"], stats["safety_margin"]), (limit, 512))
            self.assertEqual(stats["total"], stats["input_tokens"] + stats["output_reserved"] + 512)
            self.assertEqual(stats["total"] <= limit, step["status"] == "ok")
            self.assertLessEqual(stats["input_tokens"], stats["input_before_trim"])
        self.assertEqual(self.pool.stats["available"], 1)
        self.assertEqual(trace["status"], "error" if events[-1]["type"] == "error" else "completed")
        return steps, trace["steps"]

    def assert_done(self):
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        self.assertEqual(self.terminal_observations, [("completed", [self.events[1]["content"]])])
        self.memory_store.assert_called_once()

    def assert_rejected(self, step, allowed=(), *, prior_fallback=None):
        self.assertEqual([event["type"] for event in self.events], ["thinking", "error"])
        self.assertEqual(self.events[-1]["error_code"], "context_budget_exceeded")
        self.assertEqual(self.terminal_observations, [("error", [])])
        self.memory_store.assert_not_called()
        steps, trace = self.audit(allowed)
        self.assertEqual([(s["action"], s["error_code"]) for s in steps if s["status"] == "error"],
                         [(step, "context_budget_exceeded")])
        self.assertEqual([s["action"] for s in trace if s["stage"] == "fallback"],
                         [] if prior_fallback is None else [prior_fallback])

    def diagnose(self, message=QUESTION):
        return self.request(body={"message": message, "diagnosis": DIAGNOSIS})

    async def test_oversized_current_question_zero_sdk_calls_and_pool_reuse(self):
        await self.request(message=OVERSIZED)
        self.assert_rejected("main")
        retry_events = []
        await self.request(message=QUESTION, events=retry_events)
        self.assertEqual(retry_events[-1]["type"], "done")
        self.assertEqual(self.assistant_replies(), [REPLY])
        self.audit(["plan", "main"], events=retry_events)

    async def test_oversized_diagnosis_question_leaves_turn_uncommitted(self):
        await self.diagnose(OVERSIZED)
        self.assert_rejected("main")
        with closing(self.connect()) as conn:
            self.assertEqual([tuple(row) for row in conn.execute("SELECT reply FROM diagnosis_turns")], [(None,)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM diagnosis_review_proposals").fetchone()[0], 0)

    async def test_oversized_required_system_prompt_zero_sdk_calls(self):
        with patch.object(orchestration, "HOME_REACT_PROMPT", OVERSIZED + "{memory}"):
            await self.request(message=QUESTION)
        self.assert_rejected("main")

    async def test_oversized_required_tool_definition_zero_sdk_calls(self):
        definitions = orchestration.tool_definitions()
        definitions[0]["function"]["description"] = OVERSIZED
        with patch.object(orchestration, "tool_definitions", return_value=definitions):
            await self.request(message=QUESTION)
        self.assert_rejected("main")

    async def test_old_complete_turn_and_optional_memory_removed(self):
        old_call = {"id": "old-pair", "type": "function",
                    "function": {"name": "old-tool", "arguments": "{}"}}
        history = [{"role": "user", "content": "old question"},
                   {"role": "assistant", "content": None, "tool_calls": [old_call]},
                   {"role": "tool", "content": OVERSIZED, "tool_call_id": "old-pair"},
                   {"role": "assistant", "content": "old answer"},
                   {"role": "user", "content": "recent question"},
                   {"role": "assistant", "content": "recent answer"},
                   {"role": "user", "content": QUESTION}]
        with patch.object(self.pool._pool[0], "get_optimized_context", return_value=(history, "")), \
             patch.object(memory_manager, "short_term_from_history", return_value=OVERSIZED):
            await self.request(message=QUESTION)
        self.assert_done()
        steps, _ = self.audit(["plan", "main"])
        self.assertEqual(steps[-1]["budget"]["dropped_turns"], 1)
        self.assertTrue(steps[-1]["budget"]["memory_removed"])
        messages = self.capture.requests[-1]["body"]["messages"]
        self.assertEqual([item["content"] for item in messages[1:]],
                         ["recent question", "recent answer", QUESTION])
        self.assertNotIn(OVERSIZED, messages[0]["content"])

    async def test_main_receipt_and_dictionary_tool_call_reach_actual_sdk(self):
        def respond(**body):
            if body["max_tokens"] == 3000 and body["messages"][-1]["role"] != "tool":
                return response(None, tool_call("lookup", name="search_conversation_memory", query=QUESTION))
            return response()

        self.responder = respond
        completions = self.orchestrator.client._client.chat.completions
        original = completions.create

        def return_dict_calls(**kwargs):
            result = original(**kwargs)
            message = result.choices[0].message
            if message.tool_calls:
                message.tool_calls = [call.model_dump() for call in message.tool_calls]
            return result

        with patch.object(completions, "create", side_effect=return_dict_calls), \
             patch.object(orchestration, "search_conversation_tool", return_value=self.receipt):
            await self.request(message=QUESTION)
        self.assert_done()
        self.audit(["plan", "main", "main"])
        call, receipt = self.capture.requests[-1]["body"]["messages"][-2:]
        self.assertEqual(json.loads(call["tool_calls"][0]["function"]["arguments"]), {"query": QUESTION})
        self.assertEqual(json.loads(receipt["content"]), {"result": self.receipt})

    async def test_oversized_tool_arguments_block_main_continuation(self):
        self.responder = lambda **body: (response() if body["max_tokens"] == 500 else
            response(None, tool_call("lookup", name="search_conversation_memory", query=QUESTION, unused=OVERSIZED)))
        with patch.object(orchestration, "search_conversation_tool", return_value=self.receipt):
            await self.request(message=QUESTION)
        self.assert_rejected("main", ["plan", "main"])

    async def test_diagnosis_large_required_receipt_stops_before_continuation_and_fallback(self):
        self.receipt = {"value": OVERSIZED}

        def respond(**body):
            names = {tool["function"]["name"] for tool in body.get("tools") or []}
            if "query_data" in names:
                return response(None, tool_call("dispatch", name="query_data", query="lookup"))
            if "get_plan_detail" in names:
                return response(None, tool_call("plan-detail", name="get_plan_detail", plan_id="101"))
            return response()

        self.responder = respond
        await self.diagnose()
        self.assert_rejected("sub_agent_summary", ["plan", "main", "sub_agent"])
        with closing(self.connect()) as conn:
            self.assertEqual(conn.execute("SELECT reply FROM diagnosis_turns").fetchone()[0], None)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM diagnosis_review_proposals").fetchone()[0], 0)

    async def test_diagnosis_peer_budget_rejection_propagates_to_sse(self):
        def respond(**body):
            names = {tool["function"]["name"] for tool in body.get("tools") or []}
            if "query_data" in names:
                return response(None, tool_call("dispatch", name="query_data", query="lookup"))
            if "call_peer" in names:
                return response(None, tool_call("peer", name="call_peer", peer_name="query_knowledge_base",
                                                question=OVERSIZED))
            return response()

        self.responder = respond
        await self.diagnose()
        self.assert_rejected("sub_agent", ["plan", "main", "sub_agent"])

    async def test_diagnosis_commit_keeps_full_metric_and_rule_evidence(self):
        _, artifact, manifest = synthetic_artifact(self.database.parent)
        rule_id = "ctr-review-v1#low-ctr"
        baseline = {"start_date": "2026-10-01", "end_date": "2026-10-01"}
        current = {"start_date": "2026-10-02", "end_date": "2026-10-02"}
        chunk, identity, version, title, original = DEMO_RULES[0]
        rule_evidence = {"chunk_id": chunk, "rule_id": identity, "version": version,
                         "title": title, "content": original}
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO ad_plans(id,plan_name,user_id) VALUES (101,'fixture',17)")
            conn.execute("INSERT INTO plan_metric_bindings VALUES (101,?,?, 'fixture-campaign')",
                         (manifest["source_id"], manifest["data_version"]))
        knowledge_registry = self.orchestrator.knowledge_agent.tool_registry
        self.enterContext(patch.dict(knowledge_registry.function_map,
                                     {"search_knowledge": lambda query: {"evidence": [rule_evidence]}}))

        def respond(**body):
            names = {tool["function"]["name"] for tool in body.get("tools") or []}
            if body.get("tool_choice") == "none":
                return response("模型固定文案")
            if "query_data" in names:
                return response(None, tool_call("dispatch", name="query_data", query="metrics"),
                                tool_call("rule-dispatch", name="query_knowledge_base", question=rule_id))
            if "get_plan_metrics_by_date" in names:
                return response(None, tool_call("plan", name="get_plan_detail", plan_id="101"),
                                tool_call("base", name="get_plan_metrics_by_date", plan_id="101", **baseline),
                                tool_call("current", name="get_plan_metrics_by_date", plan_id="101", **current))
            if "search_knowledge" in names:
                return response(None, tool_call("rule", name="search_knowledge", query=rule_id))
            return response()

        self.responder = respond
        params = {"plan_id": 101, "rule_id": rule_id, "baseline": baseline, "current": current}
        old_turn = accept_diagnosis(17, DIAGNOSIS_REQUEST.validate_python(params), OVERSIZED)
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE diagnosis_turns SET reply='旧轮回复' WHERE id=?", (old_turn.turn_id,))
        self.enterContext(patch.object(settings, "PLAN_METRICS_DB_PATH", str(artifact)))
        await self.request(body={"message": QUESTION, "diagnosis": {
            "task_id": old_turn.snapshot.task_id, "baseline": baseline, "current": current,
        }})
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        steps, _ = self.audit(["plan", "main", "sub_agent", "sub_agent", "sub_agent_summary",
                              "sub_agent_summary", "diagnosis_summary"])
        self.assertEqual(next(s for s in steps if s["action"] == "main")["budget"]["dropped_turns"], 1)
        proposal = self.events[-1]["review_proposal"]
        self.assertEqual(proposal["evidence_gaps"], [])
        self.assertEqual(proposal["calculation"]["relative_decline_pct"], 20)
        self.assertEqual(proposal["rule_evaluation"]["original"], original)
        self.assertEqual(proposal["metrics"]["current"]["daily"][0]["clicks"], 80)
        self.assertEqual(proposal["citations"][0]["chunk_id"], chunk)
        with closing(self.connect()) as conn:
            snapshot = json.loads(conn.execute("SELECT snapshot FROM diagnosis_review_proposals").fetchone()[0])
            self.assertEqual(snapshot, {key: value for key, value in proposal.items()
                                        if key not in {"action_id", "turn_id"}})
            self.assertEqual(conn.execute("SELECT reply FROM diagnosis_turns WHERE id=?", (proposal["turn_id"],)).fetchone()[0],
                             self.events[1]["content"])
        handoffs = [json.loads(item["content"]) for item in self.capture.requests[-1]["body"]["messages"]
                    if item["role"] == "tool"]
        metrics_receipt = next(r["result"] for h in handoffs for r in h["receipts"]
                               if r["tool"] == "get_plan_metrics_by_date" and r["arguments"]["start_date"] == "2026-10-02")
        self.assertEqual(metrics_receipt, proposal["metrics"]["current"])

    async def test_sync_fallback_has_its_own_final_payload_check(self):
        self.capture.fail_dispatch = True
        self.orchestrator.data_agent.system_prompt = OVERSIZED + "{memory}"
        await self.request(message=QUESTION)
        self.assert_rejected("sub_agent", ["plan", "main"], prior_fallback="data_agent_reply")

    async def test_parallel_budget_error_takes_precedence_over_sdk_failure(self):
        sdk_failure_observed = Event()

        def respond(**body):
            if any(tool["function"]["name"] == "query_data" for tool in body.get("tools") or []):
                return response(
                    None,
                    tool_call("failed-sdk", name="query_data", query="lookup"),
                    tool_call("over-budget", name="analyze_data", query=OVERSIZED),
                )
            raise RuntimeError("fixture parallel SDK failure")

        dispatch = self.orchestrator._execute_sub_agent
        future_result = Future.result
        first_failure = None

        def dispatch_after_sdk_failure(name, *args, **kwargs):
            nonlocal first_failure
            if name == "analyze_data":
                self.assertTrue(sdk_failure_observed.wait(5), "SDK failure was not collected")
            try:
                return dispatch(name, *args, **kwargs)
            except Exception as error:
                if name == "query_data":
                    first_failure = error
                raise

        def observe_result(future, *args, **kwargs):
            try:
                return future_result(future, *args, **kwargs)
            except Exception as error:
                if error is first_failure:
                    sdk_failure_observed.set()
                raise

        self.responder = respond
        with (patch.object(self.orchestrator, "_execute_sub_agent", dispatch_after_sdk_failure),
              patch.object(Future, "result", observe_result)):
            await self.request(message=QUESTION)
        self.assertTrue(sdk_failure_observed.is_set())
        self.assert_rejected("sub_agent", ["plan", "main", "sub_agent"])

    async def test_sync_fallback_checks_sdk_tool_continuation_and_commits(self):
        self.capture.fail_dispatch = True
        self.responder = lambda **body: (response(None, tool_call("dashboard", name="get_dashboard_data"))
                                         if body.get("tools") else response())
        await self.request(message=QUESTION)
        self.assert_done()
        self.audit(["plan", "main", "sub_agent", "sub_agent_summary"])
        self.assertEqual(json.loads(self.capture.requests[-1]["body"]["messages"][-1]["content"]), self.receipt)

    async def test_stream_fallback_checks_retry_and_streaming_sdk_request(self):
        def respond(**body):
            self.assertIn("get_dashboard_data", [tool["function"]["name"] for tool in body["tools"]])
            if len(self.capture.requests) == 1:
                raise RuntimeError("fixture transient stream preparation")
            return response(None, tool_call("dashboard", name="get_dashboard_data"))

        self.responder = respond
        with patch.object(self.orchestrator, "_create_plan", side_effect=RuntimeError("fixture plan failure")):
            await self.request(message=QUESTION)
        self.assert_done()
        self.audit(["stream_prepare", "stream_retry", "stream_reply"])
        final = self.capture.requests[-1]["body"]
        self.assertTrue(final["stream"])
        self.assertEqual(json.loads(final["messages"][-1]["content"]), self.receipt)

    async def test_stream_budget_error_bypasses_retry(self):
        with patch.object(self.orchestrator, "_create_plan", side_effect=RuntimeError("fixture plan failure")), \
             patch("backend.agent.agent.SYSTEM_PROMPT_TPL", OVERSIZED + "{memory}"):
            await self.request(message=QUESTION)
        self.assert_rejected("stream_prepare", prior_fallback="data_agent_stream")

    async def test_optional_planning_can_skip_and_main_still_commits(self):
        create_plan = self.orchestrator._create_plan
        with patch.object(self.orchestrator, "_create_plan",
                          side_effect=lambda user_msg, **kw: create_plan(user_msg + OVERSIZED, **kw)):
            await self.request(message=QUESTION)
        self.assert_done()
        steps, _ = self.audit(["main"])
        self.assertEqual([(s["action"], s["status"]) for s in steps], [("plan", "skipped"), ("main", "ok")])

    async def test_reflection_and_score_keep_smaller_output_caps(self):
        await self.check_optional_review(skip=False)

    async def test_optional_reflection_and_score_skip_without_losing_reply(self):
        await self.check_optional_review(skip=True)

    async def check_optional_review(self, *, skip):
        def respond(**body):
            names = {tool["function"]["name"] for tool in body.get("tools") or []}
            if "query_data" in names:
                return response(None, tool_call(f"lookup-{len(self.capture.requests)}",
                                                name="search_conversation_memory", query=QUESTION))
            return response()

        self.responder = respond
        with ExitStack() as stack:
            stack.enter_context(patch.object(orchestration, "search_conversation_tool", return_value=self.receipt))
            if skip:
                stack.enter_context(patch.object(orchestration, "REFLECTION_PROMPT", OVERSIZED))
                stack.enter_context(patch("backend.prompts.quality.QUALITY_SCORE_PROMPT", OVERSIZED))
            await self.request(message=QUESTION)
        self.assert_done()
        expected = ["plan", "main", "main", "main_summary"]
        if not skip:
            expected += ["reflection", "reflection", "quality_score"]
        steps, _ = self.audit(expected)
        trace = json.loads(self.trace_file.read_text(encoding="utf-8").splitlines()[-1])
        stops = [step for step in trace["steps"] if step["stage"] == "react_decision" and step["action"] == "stop"]
        self.assertEqual([step["evidence"]["reason"] for step in stops], ["no_progress"])
        self.assertEqual([(s["action"], s["budget"]["output_reserved"]) for s in steps
                          if s["action"] in {"plan", "reflection", "quality_score"}],
                         [("plan", 500)] + [("reflection", 2000)] * (1 if skip else 2) + [("quality_score", 200)])
        self.assertEqual([s["action"] for s in steps if s["status"] == "skipped"],
                         ["reflection", "quality_score"] if skip else [])
