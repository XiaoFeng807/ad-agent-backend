"""Offline regression for the homepage SSE route and its actual Agent loop."""

import asyncio
import importlib
import json
import sqlite3
import threading
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from anyio.to_thread import current_default_thread_limiter

from backend import observability
from backend.agent.agent_pool import AgentPool
from backend.agent.llm_provider import ProviderFactory
from backend.agent.orchestrator import OrchestratorAgent
from backend.agent.pipeline import PipelineCoordinator
from backend.auth import auth
from backend.database import database
from backend.di import DatabaseProvider
from backend.memory import memory_manager
from backend.tools import tools


MESSAGE = "复盘投放 private-user-message"
DATA_QUERY = "private-data-query"
KNOWLEDGE_QUERY = "private-knowledge-query"
REPLY = "固定复盘回复"
FALLBACK = "固定降级回复"
TOOL_CALLS = {
    "call_data": ("query_data", "数据助手"),
    "call_knowledge": ("query_knowledge_base", "知识助手"),
    "call_dashboard": ("get_dashboard_data", "数据助手"),
    "call_search": ("search_knowledge", "知识助手"),
}


def tool_call(call_id, *, name=None, **arguments):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(
        name=name or TOOL_CALLS[call_id][0], arguments=json.dumps(arguments),
    ))


class FixedModel:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)
        self.receipts = {}

    def create(self, messages, tools=None, **kwargs):
        if kwargs.get("stream"):
            chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=FALLBACK))])
            return iter([chunk])
        names = {tool["function"]["name"] for tool in tools or []}
        receipts = {m["tool_call_id"]: json.loads(m["content"])
                    for m in messages if m["role"] == "tool"}
        self.receipts.update(receipts)
        calls = []
        if "query_data" in names:
            if not receipts:
                calls = [tool_call("call_data", query=DATA_QUERY),
                         tool_call("call_knowledge", question=KNOWLEDGE_QUERY)]
        elif "get_dashboard_data" in names:
            if messages[-1]["content"] == DATA_QUERY:
                calls = [tool_call("call_dashboard")]
            else:
                return self.response(FALLBACK, [])
        elif "search_knowledge" in names:
            calls = [tool_call("call_search", query=KNOWLEDGE_QUERY)]

        if calls:
            return self.response(None, calls)
        if receipts.keys() == {"call_data", "call_knowledge"}:
            return self.response(REPLY, [])
        if "call_dashboard" in receipts:
            return self.response("private-data-subagent-reply", [])
        if "call_search" in receipts:
            return self.response("private-knowledge-subagent-reply", [])
        return self.response('{"steps": []}', [])

    @staticmethod
    def response(content, calls):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=calls),
        )])


class HomeSSEFixture(unittest.IsolatedAsyncioTestCase):
    model_class = FixedModel

    @classmethod
    def setUpClass(cls):
        with patch.object(ProviderFactory, "create", return_value=cls.model_class()):
            cls.server = importlib.import_module("fast_server")

    def tool_overrides(self):
        return {}

    async def asyncSetUp(self):
        tmp = Path(self.enterContext(TemporaryDirectory()))
        self.database = tmp / "chat.sqlite3"
        self.trace_file = tmp / "traces.jsonl"
        if archive := getattr(self, "archive_traces", None):
            self.addCleanup(archive, self.id(), self.trace_file)
        self.signing_key = token_urlsafe(32)
        self.enterContext(patch.object(auth.settings, "JWT_SECRET", self.signing_key))
        self.token = self.sign_token(user_id=17)
        self.model = self.model_class()
        self.enterContext(patch.object(database, "get_db", self.connect))
        database.init_db()
        self.enterContext(patch.object(self.server.auth_instance, "db", DatabaseProvider()))
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO sessions (user_id, token) VALUES (?, ?)", (17, self.token))
        self.enterContext(patch.object(memory_manager, "MEMORY_DIR", str(tmp / "memory")))
        self.enterContext(patch.object(memory_manager, "add_fact"))
        self.memory_store = self.enterContext(patch("backend.memory.conversation_memory.store_conversation"))
        self.enterContext(patch.object(observability, "TRACE_LOG_FILE", self.trace_file))
        self.enterContext(patch.object(observability, "_traces", {}))
        self.enterContext(patch.dict(tools.function_map, self.tool_overrides()))
        with patch.object(ProviderFactory, "create", return_value=self.model):
            self.pool = AgentPool(min_size=1, max_size=1)
        self.enterContext(patch.object(self.server, "agent_pool", self.pool))
        self.events = []
        self.status_code = None
        self.terminal_observations = []
        self.disconnect = asyncio.Event()
        self.disconnect_seen = asyncio.Event()

    def sign_token(self, **claims):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        return jwt.encode({"exp": expiry, **claims}, self.signing_key, algorithm="HS256")

    def connect(self):
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        return conn

    def assistant_replies(self):
        with closing(self.connect()) as conn:
            return [row["content"] for row in conn.execute(
                "SELECT content FROM conversations WHERE role = 'assistant' ORDER BY id"
            )]

    async def request(self, *, message=MESSAGE, events=None, on_thinking=None, body=None, token=None,
                      asgi_version="2.3", fail_text_send=False, authenticated=True):
        if events is None:
            events = self.events
        if body is None:
            body = {"message": message}
        incoming = [{"type": "http.request", "body": json.dumps(body).encode()}]
        self.response_json = None

        async def receive():
            if incoming:
                return incoming.pop()
            await self.disconnect.wait()
            self.disconnect_seen.set()
            return {"type": "http.disconnect"}

        async def send(message):
            await asyncio.sleep(0)
            if message["type"] == "http.response.start":
                self.status_code = message["status"]
            body = message.get("body", b"")
            if not body.startswith(b"data: "):
                if body and self.status_code != 200:
                    self.response_json = json.loads(body)
                return
            event = json.loads(body[6:])
            if fail_text_send and event["type"] == "text":
                raise OSError("offline connection closed")
            events.append(event)
            if event["type"] == "thinking" and on_thinking is not None:
                await on_thinking()
            if event["type"] in {"done", "error"}:
                trace = observability.get_trace(event["trace_id"])
                self.terminal_observations.append((trace["status"], self.assistant_replies()))

        headers = [(b"authorization", f"Bearer {token or self.token}".encode())] if authenticated else []
        await self.server.app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": asgi_version},
            "method": "POST", "scheme": "http", "path": "/api/chat/stream", "query_string": b"",
            "headers": headers,
        }, receive, send)

    def assert_trace(self, status, *, expected_calls=TOOL_CALLS, failed_calls=(), trace_count=1):
        self.assertEqual(self.status_code, 200)
        trace_id = self.events[0]["trace_id"]
        self.assertTrue(all(event.get("trace_id", trace_id) == trace_id for event in self.events))
        raw_trace = self.trace_file.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in raw_trace.splitlines()]
        trace = observability.get_trace(trace_id)
        self.assertEqual(len(rows), trace_count)
        self.assertEqual(rows[-1]["trace_id"], trace_id)
        self.assertEqual(rows[-1], trace)
        self.assertEqual(trace["status"], status)
        self.assertEqual(self.pool.stats["available"], 1)
        self.assertNotIn("private-", raw_trace)
        self.assertNotIn(self.token, raw_trace)
        self.assertNotIn("message", trace)
        self.assertNotIn("user_id", trace)

        steps = trace["steps"]
        if expected_calls is None:
            return steps

        def call_key(step):
            return step.get("tool_call_id", step.get("runtime_call_id"))

        for stage in ("tool_call", "tool_result"):
            self.assertCountEqual([call_key(step) for step in steps if step["stage"] == stage], expected_calls.keys())
        for call_id, (tool, role) in expected_calls.items():
            indexed = [(i, step) for i, step in enumerate(steps) if call_key(step) == call_id
                       and step["stage"] in ("tool_call", "tool_result")]
            [(start, call), (end, result)] = indexed
            self.assertLess(start, end)
            for step in (call, result):
                self.assertEqual((step["action"], step["role"]), (tool, role))
            self.assertEqual((call["stage"], result["stage"]), ("tool_call", "tool_result"))
            self.assertGreaterEqual(result["duration"], 0)
            self.assertEqual(result["status"], "error" if call_id in failed_calls else "ok")
        return steps


class HomeSSETracingTests(HomeSSEFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fail_tool = False
        self.data_result = {"result": "private-data-result"}
        self.block_tool = False
        self.tool_entered = asyncio.Event()
        self.resume_tool = threading.Event()
        self.loop = asyncio.get_running_loop()

    def tool_overrides(self):
        return {"get_dashboard_data": self.query_data, "search_knowledge": self.query_knowledge}

    def query_data(self, user_id=None):
        if user_id != 17:
            raise ValueError("unexpected user scope")
        if self.block_tool:
            self.loop.call_soon_threadsafe(self.tool_entered.set)
            if not self.resume_tool.wait(timeout=5):
                raise TimeoutError("offline tool gate timed out")
        if self.fail_tool:
            raise RuntimeError("private-tool-exception")
        return self.data_result

    @staticmethod
    def query_knowledge(query):
        if query != KNOWLEDGE_QUERY:
            raise ValueError("unexpected knowledge query")
        return json.dumps({"context": "private-knowledge-result", "sources": ["private-source-label"]})

    async def test_done_is_sent_once_after_commit(self):
        await self.request()
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        self.assertEqual(self.terminal_observations, [("completed", [REPLY])])
        self.assertEqual(self.model.receipts["call_data"], {"result": "private-data-subagent-reply"})
        self.assertEqual(self.model.receipts["call_knowledge"], {"result": "private-knowledge-subagent-reply"})
        self.assertEqual(self.assert_trace("completed")[-1]["action"], "save_reply")

    async def test_save_failure_emits_only_error(self):
        with closing(self.connect()) as conn:
            conn.execute("""CREATE TRIGGER fail_reply BEFORE INSERT ON conversations
                WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'private-save-error'); END""")
        await self.request()
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "error"])
        self.assertEqual(self.terminal_observations, [("error", [])])
        save = self.assert_trace("error")[-1]
        self.assertEqual((save["stage"], save["status"], save["error_code"]),
                         ("persistence", "error", "save_reply_failed"))
        self.assertNotIn("private-", self.events[-1]["content"])

    async def test_tool_failure_remains_visible_after_fallback(self):
        self.fail_tool = True
        await self.request()
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        self.assertEqual(self.terminal_observations, [("completed", [FALLBACK])])
        steps = self.assert_trace("completed", failed_calls={"call_data", "call_dashboard"})
        non_model_steps = [step for step in steps if step["stage"] not in {"model_call", "model_result"}]
        self.assertEqual([(step["stage"], step["action"]) for step in non_model_steps[-3:]], [
            ("fallback", "data_agent_reply"), ("context_budget", "sub_agent"), ("persistence", "save_reply"),
        ])

    async def test_concurrent_requests_read_history_through_their_own_message(self):
        first, second = "private-request-A", "private-request-B"
        model_inputs = []

        def echo(messages, tools=None, **kwargs):
            if not tools:
                return FixedModel.response('{"steps": []}', [])
            turns = [(m["role"], m["content"]) for m in messages if m["role"] != "system"]
            model_inputs.append(turns)
            last_user = next(content for role, content in reversed(turns) if role == "user")
            return FixedModel.response("echo " + last_user, [])

        with closing(self.connect()) as conn:
            conn.execute("""INSERT INTO conversations (user_id, role, content, priority)
                VALUES (99, 'user', 'private-other-user', 1)""")
            conn.commit()
        with patch.object(ProviderFactory, "create", return_value=self.model):
            pool = AgentPool(min_size=2, max_size=2)
        saved = {message: asyncio.Event() for message in (first, second)}
        resume = {message: threading.Event() for message in (first, second)}
        save_conversation = PipelineCoordinator.save_conversation

        def save_and_wait(agent, user_id, role, content, **kwargs):
            result = save_conversation(agent, user_id, role, content, **kwargs)
            if role == "user" and content in saved:
                self.loop.call_soon_threadsafe(saved[content].set)
                if not resume[content].wait(timeout=10):
                    raise TimeoutError("offline message save gate timed out")
            return result

        events_a, events_b = [], []
        tasks = []
        with (
            patch.object(self.server, "agent_pool", pool),
            patch.object(self.model, "create", side_effect=echo),
            patch.object(PipelineCoordinator, "save_conversation", save_and_wait),
        ):
            try:
                tasks.append(asyncio.create_task(self.request(message=first, events=events_a)))
                await asyncio.wait_for(saved[first].wait(), timeout=5)
                tasks.append(asyncio.create_task(self.request(message=second, events=events_b)))
                await asyncio.wait_for(saved[second].wait(), timeout=5)
                resume[first].set()
                await asyncio.wait_for(asyncio.shield(tasks[0]), timeout=5)
            finally:
                for gate in resume.values():
                    gate.set()
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

        with closing(self.connect()) as conn:
            saved_turns = [tuple(row) for row in conn.execute(
                "SELECT role, content FROM conversations WHERE user_id = 17 ORDER BY id"
            )]
        with self.subTest(check="model history"):
            self.assertEqual(model_inputs, [[("user", first)], [("user", first), ("user", second)]])
        for events, message in ((events_a, first), (events_b, second)):
            with self.subTest(check="SSE", message=message):
                self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])
                self.assertEqual(events[1]["content"], "echo " + message)
        with self.subTest(check="persistence"):
            self.assertEqual(saved_turns, [
                ("user", first), ("user", second),
                ("assistant", "echo " + first), ("assistant", "echo " + second),
            ])
        self.assertEqual(pool.stats["available"], 2)

    async def assert_short_term_isolation(self, path, expected_calls, fallback=None):
        first, second = "private-question-A", "private-question-B"
        reply_a, reply_b = "private-answer-A", "private-answer-B"
        legacy = "private-legacy-summary"
        memory_manager.store_conversation_summary(17, legacy, "legacy answer")
        model_calls = []

        def respond(messages, tools=None, **kwargs):
            names = {tool["function"]["name"] for tool in tools or []}
            last_user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            if kwargs.get("max_tokens") == 500:
                stage = "plan"
            elif "query_data" in names:
                stage = "orchestrator"
            elif "search_knowledge" in names:
                stage = "peer"
            elif last_user == DATA_QUERY:
                stage = "sub_agent" if tools else "sub_agent_final"
            elif kwargs.get("stream"):
                stage = "stream_reply"
            else:
                stage = "data_agent"
            model_calls.append((stage, deepcopy(messages)))

            if stage == "plan":
                return FixedModel.response('{"steps": []}', [])
            if last_user == second:
                return FixedModel.response(reply_b, [])
            if stage == "orchestrator":
                if path == "sub_agent" and not any(m["role"] == "tool" for m in messages):
                    return FixedModel.response(None, [tool_call("call_data", query=DATA_QUERY)])
                if path == "reply_fallback":
                    raise RuntimeError("offline orchestrator failure")
            if stage == "sub_agent":
                return FixedModel.response(None, [SimpleNamespace(id="call_peer", function=SimpleNamespace(
                    name="call_peer",
                    arguments=json.dumps({"peer_name": "query_knowledge_base", "question": KNOWLEDGE_QUERY}),
                ))])
            if stage == "stream_reply":
                chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=reply_a))])
                return iter([chunk])
            return FixedModel.response(reply_a, [])

        with patch.object(ProviderFactory, "create", return_value=self.model):
            pool = AgentPool(min_size=2, max_size=2)
        saved, resume = asyncio.Event(), threading.Event()
        save_conversation = PipelineCoordinator.save_conversation
        create_plan = OrchestratorAgent._create_plan

        def save_and_wait(agent, user_id, role, content, **kwargs):
            result = save_conversation(agent, user_id, role, content, **kwargs)
            if role == "user" and content == first:
                self.loop.call_soon_threadsafe(saved.set)
                if not resume.wait(timeout=10):
                    raise TimeoutError("offline message save gate timed out")
            return result

        def plan(agent, user_msg, **kwargs):
            if path == "stream_fallback" and user_msg == first:
                raise RuntimeError("offline planning failure")
            return create_plan(agent, user_msg, **kwargs)

        events_a, events_b = [], []
        with (
            patch.object(self.server, "agent_pool", pool),
            patch.object(self.model, "create", side_effect=respond),
            patch.object(PipelineCoordinator, "save_conversation", save_and_wait),
            patch.object(OrchestratorAgent, "_create_plan", plan),
        ):
            request_a = asyncio.create_task(self.request(message=first, events=events_a))
            try:
                await asyncio.wait_for(saved.wait(), timeout=5)
                await asyncio.wait_for(self.request(message=second, events=events_b), timeout=5)
                self.assertEqual(self.assistant_replies(), [reply_b])
                shared_summary = memory_manager.get_conversation_summaries(17)
                self.assertIn(second, shared_summary)
                self.assertIn(reply_b, shared_summary)
                b_calls = len(model_calls)
            finally:
                resume.set()
                await asyncio.wait_for(request_a, timeout=10)

        a_calls = model_calls[b_calls:]
        self.assertEqual([stage for stage, _ in a_calls], expected_calls)
        for stage, messages in a_calls:
            last_user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            if stage == "plan":
                self.assertTrue(last_user.endswith(first))
            else:
                self.assertEqual(last_user, {
                    "sub_agent": DATA_QUERY, "sub_agent_final": DATA_QUERY, "peer": KNOWLEDGE_QUERY,
                }.get(stage, first))
        leaks = [
            (stage, message["role"], label)
            for stage, messages in a_calls
            for message in messages
            for label, marker in (("B question", second), ("B answer", reply_b), ("legacy summary", legacy))
            if marker in (message.get("content") or "")
        ]
        with self.subTest(check="all A model messages including system"):
            self.assertFalse(leaks, f"Unexpected markers in A model inputs: {leaks}")
        if path == "sub_agent":
            final = next(messages for stage, messages in a_calls if stage == "sub_agent_final")
            self.assertEqual([tc.id for m in final for tc in m.get("tool_calls", [])], ["call_peer"])
            self.assertEqual([(m["tool_call_id"], json.loads(m["content"])) for m in final if m["role"] == "tool"],
                             [("call_peer", reply_a)])

        for events, expected in ((events_a, reply_a), (events_b, reply_b)):
            self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])
            self.assertEqual(events[1]["content"], expected)
        with closing(self.connect()) as conn:
            saved_turns = [tuple(row) for row in conn.execute(
                "SELECT role, content FROM conversations WHERE user_id = 17 ORDER BY id"
            )]
        self.assertEqual(saved_turns, [
            ("user", first), ("user", second), ("assistant", reply_b), ("assistant", reply_a),
        ])
        self.assertEqual(self.terminal_observations, [("completed", [reply_b]), ("completed", [reply_b, reply_a])])
        trace = observability.get_trace(events_a[-1]["trace_id"])
        self.assertEqual([s["action"] for s in trace["steps"] if s["stage"] == "fallback"],
                         [fallback] if fallback else [])
        self.assertEqual(pool.stats["available"], 2)

    async def test_short_term_isolation_in_orchestrator(self):
        await self.assert_short_term_isolation("main", ["plan", "orchestrator"])

    async def test_short_term_isolation_in_sub_agent_and_peer(self):
        await self.assert_short_term_isolation(
            "sub_agent", ["plan", "orchestrator", "sub_agent", "peer", "sub_agent_final", "orchestrator"],
        )

    async def test_short_term_isolation_in_reply_fallback(self):
        await self.assert_short_term_isolation(
            "reply_fallback", ["plan", "orchestrator", "data_agent"], fallback="data_agent_reply",
        )

    async def test_short_term_isolation_in_stream_fallback(self):
        await self.assert_short_term_isolation(
            "stream_fallback", ["data_agent", "stream_reply"], fallback="data_agent_stream",
        )

    async def test_follow_up_uses_previous_sqlite_turn_without_shared_summary(self):
        with patch.object(memory_manager, "store_conversation_summary"):
            await self.request()
        self.assertEqual(self.assistant_replies(), [REPLY])
        self.assertEqual(memory_manager.get_conversation_summaries(17), "")

        model_inputs = []
        create = self.model.create

        def capture(messages, tools=None, **kwargs):
            if tools:
                model_inputs.append(deepcopy(messages))
            return create(messages, tools, **kwargs)

        follow_up = "private-follow-up"
        events = []
        with patch.object(self.model, "create", side_effect=capture):
            await self.request(message=follow_up, events=events)

        self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])
        self.assertEqual(len(model_inputs), 4)  # Orchestrator twice, data and knowledge once each.
        for messages in model_inputs:
            self.assertEqual(messages[0]["role"], "system")
            self.assertIn(MESSAGE, messages[0]["content"])
            self.assertIn(REPLY, messages[0]["content"])
        self.assertEqual([(m["role"], m["content"]) for m in model_inputs[0][1:]], [
            ("user", MESSAGE), ("assistant", REPLY), ("user", follow_up),
        ])

    async def test_waiting_for_full_pool_leaves_worker_available_to_holder(self):
        holder_thinking, resume_holder, waiter_checked = (asyncio.Event() for _ in range(3))

        class ObservedInUse(set):
            def __contains__(in_use, agent_id):
                occupied = super().__contains__(agent_id)
                if occupied:
                    self.loop.call_soon_threadsafe(waiter_checked.set)
                return occupied

        async def pause_holder():
            holder_thinking.set()
            await resume_holder.wait()

        limiter = current_default_thread_limiter()
        old_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        tasks = []
        holder_events, waiter_events = [], []
        with patch.object(self.pool, "_in_use", ObservedInUse()):
            try:
                tasks.append(asyncio.create_task(self.request(events=holder_events, on_thinking=pause_holder)))
                await asyncio.wait_for(holder_thinking.wait(), timeout=5)
                tasks.append(asyncio.create_task(self.request(events=waiter_events)))
                await asyncio.wait_for(waiter_checked.wait(), timeout=5)
                self.assertEqual(self.pool.stats["in_use"], 1)
                self.assertEqual(waiter_events, [])
                resume_holder.set()
                await asyncio.wait_for(asyncio.shield(tasks[0]), timeout=5)
                await asyncio.wait_for(asyncio.shield(tasks[1]), timeout=5)
            finally:
                resume_holder.set()
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
                finally:
                    limiter.total_tokens = old_tokens

        for events in (holder_events, waiter_events):
            self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])
        self.assertEqual(self.assistant_replies(), [REPLY, REPLY])
        self.assertEqual(self.pool.stats["available"], 1)

    async def test_main_and_fallback_failure_emit_only_error(self):
        create = self.model.create

        def fail_replies(messages, tools=None, **kwargs):
            if tools or kwargs.get("stream"):
                raise RuntimeError("private-model-failure")
            return create(messages, tools, **kwargs)

        for trace_count, action in enumerate(("data_agent_reply", "data_agent_stream"), start=1):
            with self.subTest(fallback=action):
                self.events.clear()
                self.terminal_observations.clear()
                with patch.object(self.model, "create", side_effect=fail_replies):
                    if action == "data_agent_stream":
                        with patch.object(OrchestratorAgent, "_create_plan",
                                          side_effect=RuntimeError("private-plan-error")):
                            await self.request()
                    else:
                        await self.request()
                with self.subTest(check="terminal"):
                    self.assertEqual([event["type"] for event in self.events], ["thinking", "error"])
                with self.subTest(check="persistence and trace"):
                    self.assertEqual(self.terminal_observations, [("error", [])])
                steps = self.assert_trace("error", expected_calls={}, trace_count=trace_count)
                if action == "data_agent_reply":
                    expected = [("context_budget", "plan"), ("context_budget", "main"),
                                ("fallback", action), ("context_budget", "sub_agent")]
                else:
                    expected = [("fallback", action), ("context_budget", "stream_prepare"),
                                ("context_budget", "stream_retry")]
                self.assertEqual([(step["stage"], step["action"]) for step in steps
                                  if step["stage"] not in {"model_call", "model_result"}],
                                 [*expected, ("execution", "generate_reply")])
                self.assertEqual(steps[-1]["error_code"], "generate_reply_failed")

    async def test_stream_fallback_pairs_tool_business_failure_with_its_call(self):
        result = {"success": False, "message": "private-business-failure"}
        self.data_result = result
        create = self.model.create
        receipts = []

        def call_dashboard(messages, tools=None, **kwargs):
            if tools:
                return FixedModel.response(None, [tool_call("call_dashboard")])
            receipts.extend((m["tool_call_id"], json.loads(m["content"]))
                            for m in messages if m["role"] == "tool")
            return create(messages, tools, **kwargs)

        with (
            patch.object(OrchestratorAgent, "_create_plan", side_effect=RuntimeError("private-plan-error")),
            patch.object(self.model, "create", side_effect=call_dashboard),
        ):
            await self.request()
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        self.assertEqual(self.terminal_observations, [("completed", [FALLBACK])])
        self.assertEqual(receipts, [("call_dashboard", result)])
        steps = self.assert_trace(
            "completed", expected_calls={"call_dashboard": TOOL_CALLS["call_dashboard"]},
            failed_calls={"call_dashboard"},
        )
        self.assertEqual(steps[0]["action"], "data_agent_stream")
        result_step = next(step for step in steps if step["stage"] == "tool_result")
        self.assertEqual(result_step["error_code"], "tool_reported_failure")

    async def test_disconnect_waits_for_active_tool_and_releases_agent(self):
        self.block_tool = True
        task = asyncio.create_task(self.request())
        try:
            await asyncio.wait_for(self.tool_entered.wait(), timeout=5)
            self.disconnect.set()
            await asyncio.wait_for(self.disconnect_seen.wait(), timeout=5)
            self.assertEqual(self.pool.stats["in_use"], 1)
        finally:
            self.resume_tool.set()
            await asyncio.wait_for(task, timeout=5)
        self.assertEqual([event["type"] for event in self.events], ["thinking"])
        self.assertEqual(self.assistant_replies(), [])
        steps = self.assert_trace("interrupted", expected_calls=None)
        self.assertTrue(any(step["stage"] == "tool_result" and step.get("tool_call_id") == "call_dashboard"
                            and step.get("error_code") == "expired_receipt" for step in steps))

    async def test_transport_send_failure_closes_suspended_generator(self):
        await self.request(asgi_version="2.4", fail_text_send=True)
        self.assertEqual([event["type"] for event in self.events], ["thinking"])
        self.assertEqual(self.assistant_replies(), [])
        self.assert_trace("interrupted")

    async def test_transport_failure_also_closes_fallback_stream(self):
        with patch.object(OrchestratorAgent, "_create_plan", side_effect=RuntimeError("private-plan-error")):
            await self.request(asgi_version="2.4", fail_text_send=True)
        self.assertEqual([event["type"] for event in self.events], ["thinking"])
        self.assertEqual(self.assistant_replies(), [])
        steps = self.assert_trace("interrupted", expected_calls={})
        self.assertEqual([(step["stage"], step["action"]) for step in steps
                          if step["stage"] not in {"model_call", "model_result"}], [
            ("fallback", "data_agent_stream"), ("context_budget", "stream_prepare"),
            ("context_budget", "stream_reply"),
        ])

    async def test_cancelled_request_does_not_release_agent_while_tool_runs(self):
        self.block_tool = True
        task = asyncio.create_task(self.request(asgi_version="2.4"))
        try:
            await asyncio.wait_for(self.tool_entered.wait(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            self.assertEqual(self.pool.stats["in_use"], 1)
        finally:
            self.resume_tool.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        self.assertEqual(self.assistant_replies(), [])
        steps = self.assert_trace("interrupted", expected_calls=None)
        self.assertTrue(any(step["stage"] == "tool_result" and step.get("tool_call_id") == "call_dashboard"
                            and step.get("error_code") == "expired_receipt" for step in steps))

    async def test_unauthorized_request_does_not_start_trace(self):
        await self.request(authenticated=False)
        self.assertEqual(self.status_code, 401)
        self.assertEqual(observability.list_recent_traces(), [])
        self.assertFalse(self.trace_file.exists())
