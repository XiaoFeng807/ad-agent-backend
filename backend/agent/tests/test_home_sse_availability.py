"""Pause individual chat stages while exercising the real homepage ASGI routes."""

import asyncio
import threading
from contextlib import closing, contextmanager
from unittest.mock import patch

import httpx
from anyio.to_thread import run_sync

from backend.agent import agent_pool
from backend.agent.llm_provider import ProviderFactory
from backend.agent.tests.test_home_sse_tracing import HomeSSEFixture, MESSAGE, REPLY
from backend.memory import memory_manager
from backend.tools import tools


class HomeSSEAvailabilityTests(HomeSSEFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pause_tool = None
        with closing(self.connect()) as conn, conn:
            conn.execute("""INSERT INTO ad_plans (id, plan_name, user_id, cost, sales, impressions, clicks)
                            VALUES (1, 'demo plan', 17, 25, 50, 100, 5)""")
        self.enterContext(patch.object(tools, "get_db", self.connect))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app), base_url="http://test",
            headers={"Authorization": f"Bearer {self.token}"},
        ))

    def tool_overrides(self):
        return {
            "get_dashboard_data": self.query_dashboard,
            "search_knowledge": lambda query: {"context": "fixed rule"},
        }

    def query_dashboard(self, user_id=None):
        if self.pause_tool is not None:
            self.pause_tool()
        return {"result": "fixed data"}

    @contextmanager
    def pause(self):
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        # A watchdog also releases the gate when a regression blocks the loop itself.
        watchdog = threading.Timer(3, release.set)

        def block():
            watchdog.start()
            loop.call_soon_threadsafe(entered.set)
            release.wait()

        try:
            yield block, entered, release
        finally:
            release.set()
            watchdog.cancel()

    async def assert_pages_ready(self, stage, release):
        paths = ("/api/health", "/api/dashboard", "/api/plans")
        responses = await asyncio.wait_for(
            asyncio.gather(*(self.client.get(path) for path in paths)), timeout=2,
        )
        status = [response.status_code for response in responses]
        before_release = not release.is_set()
        print(f"AD-016 {stage}: {dict(zip(paths, status))}, before_release={before_release}", flush=True)
        self.assertEqual(status, [200, 200, 200])
        self.assertTrue(before_release, f"Page requests waited for {stage}")
        self.assertEqual(responses[1].json()["data"]["total_cost"], 25)
        self.assertEqual(responses[2].json()["data"][0]["id"], 1)

    async def test_pages_respond_during_ai_tool(self):
        with self.pause() as (block, entered, release):
            self.pause_tool = block
            chat = asyncio.create_task(self.request())
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                await self.assert_pages_ready("AI tool", release)
            finally:
                release.set()
                await asyncio.wait_for(chat, timeout=5)
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])

    async def test_chat_completes_during_history_read_and_late_history_excludes_its_turn(self):
        view_id = "current-view"
        with closing(self.connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO conversations (user_id, role, content, priority, chat_view_id) VALUES (?, ?, ?, 1, ?)",
                [(17, "user", MESSAGE, None), (17, "assistant", REPLY, "other-view"),
                 (99, "user", "another user's message", view_id)],
            )
        read_history = self.server.agent.get_optimized_context
        with self.pause() as (block, entered, release):
            def read_late(*args, **kwargs):
                block()
                return read_history(*args, **kwargs)

            with patch.object(self.server.agent, "get_optimized_context", side_effect=read_late):
                pending = asyncio.create_task(self.client.get(
                    "/api/chat/history", params={"exclude_chat_view_id": view_id},
                ))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=2)
                    await asyncio.wait_for(self.request(body={"message": MESSAGE, "chat_view_id": view_id}), timeout=2)
                    self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
                    self.assertFalse(release.is_set())
                    self.assertFalse(pending.done())
                finally:
                    release.set()
                    history = await asyncio.wait_for(pending, timeout=5)

        self.assertEqual(history.status_code, 200)
        self.assertEqual([(row["role"], row["content"]) for row in history.json()["data"]],
                         [("user", MESSAGE), ("assistant", REPLY)])
        unfiltered = await self.client.get("/api/chat/history")
        self.assertEqual(unfiltered.status_code, 200)
        self.assertEqual(len(unfiltered.json()["data"]), 4)
        print("AD-016 history: SSE completed before GET release, filtered rows=2, unfiltered rows=4", flush=True)

    async def test_pages_and_pool_holder_progress_during_expansion(self):
        holder_ready, resume_holder = asyncio.Event(), asyncio.Event()

        async def pause_holder():
            holder_ready.set()
            await resume_holder.wait()

        self.pool._max_size = 2
        holder_events, new_events = [], []
        coordinator = agent_pool.PipelineCoordinator
        with self.pause() as (block, entered, release):
            def construct(orchestrator):
                block()
                return coordinator(orchestrator)

            with (
                patch.object(agent_pool, "PipelineCoordinator", side_effect=construct),
                patch.object(ProviderFactory, "create", return_value=self.model),
            ):
                holder = asyncio.create_task(self.request(events=holder_events, on_thinking=pause_holder))
                tasks = [holder]
                try:
                    await asyncio.wait_for(holder_ready.wait(), timeout=5)
                    tasks.append(asyncio.create_task(self.request(events=new_events)))
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    await self.assert_pages_ready("pool expansion", release)
                    resume_holder.set()
                    await asyncio.wait_for(asyncio.shield(holder), timeout=2)
                    self.assertEqual(holder_events[-1]["type"], "done")
                    self.assertFalse(release.is_set(), "The holder waited for the constructor")
                    self.assertEqual(self.pool.stats["available"], 1)
                finally:
                    resume_holder.set()
                    release.set()
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        self.assertEqual([event["type"] for event in new_events], ["thinking", "text", "done"])
        self.assertEqual(self.pool.stats["available"], 2)

    async def test_pages_respond_during_summary_write(self):
        save = memory_manager._save
        with self.pause() as (block, entered, release):
            def save_summary(user_id, memory):
                block()
                save(user_id, memory)

            with patch.object(memory_manager, "_save", side_effect=save_summary):
                chat = asyncio.create_task(self.request())
                try:
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    await self.assert_pages_ready("summary write", release)
                    self.assertEqual(self.assistant_replies(), [REPLY])
                    self.assertEqual([event["type"] for event in self.events], ["thinking", "text"])
                finally:
                    release.set()
                    await asyncio.wait_for(chat, timeout=5)
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        self.assertEqual(self.terminal_observations, [("completed", [REPLY])])

    async def test_disconnect_during_expansion_leaves_constructed_agent_available(self):
        pool = agent_pool.AgentPool(min_size=0, max_size=1)
        coordinator = agent_pool.PipelineCoordinator
        with self.pause() as (block, entered, release):
            def construct(orchestrator):
                block()
                return coordinator(orchestrator)

            with (
                patch.object(self.server, "agent_pool", pool),
                patch.object(agent_pool, "PipelineCoordinator", side_effect=construct),
                patch.object(ProviderFactory, "create", return_value=self.model),
            ):
                chat = asyncio.create_task(self.request())
                try:
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    self.disconnect.set()
                    await asyncio.wait_for(self.disconnect_seen.wait(), timeout=2)
                    self.assertFalse(release.is_set())
                    self.assertEqual(pool.stats["total"], 0)
                finally:
                    release.set()
                    await asyncio.wait_for(chat, timeout=5)
        self.assertEqual(self.events, [])
        self.assertEqual(self.assistant_replies(), [])
        self.assertEqual(pool.stats["available"], 1)

    async def test_same_user_memory_serializes_reads_and_updates(self):
        store = memory_manager.store_conversation_summary
        read = memory_manager.get_conversation_summaries
        store(17, "previous", "old reply")
        save = memory_manager._save
        tasks = []
        with self.pause() as (block, entered, release):
            def save_first(user_id, memory):
                if user_id == 17 and not release.is_set() and memory["short_term"][-1].endswith("first reply"):
                    block()
                save(user_id, memory)

            with patch.object(memory_manager, "_save", side_effect=save_first):
                tasks.append(asyncio.create_task(run_sync(store, 17, "A", "first reply")))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    read_task = asyncio.create_task(run_sync(read, 17))
                    tasks.extend([
                        read_task,
                        asyncio.create_task(run_sync(store, 17, "B", "second reply")),
                        asyncio.create_task(run_sync(memory_manager.update_profile, 17, "get_dashboard_data")),
                    ])
                    completed, _ = await asyncio.wait(tasks[1:], timeout=0.2)
                    self.assertFalse(completed)
                    await asyncio.wait_for(run_sync(store, 18, "other user", "independent"), timeout=2)
                    self.assertFalse(release.is_set())
                finally:
                    release.set()
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        self.assertIn("previous -> old reply", read_task.result())
        self.assertIn("A -> first reply", read_task.result())
        self.assertIn("B -> second reply", read(17))
        self.assertIn("get_dashboard_data(1次)", memory_manager.get_profile_text(17))
