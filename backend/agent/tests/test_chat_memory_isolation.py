"""Offline ownership/boundary regressions with real temporary Chroma and SQLite.

All model messages (including system, tool receipts and retries) are captured.
Only synthetic markers are used; no provider or embedding network calls.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend import observability
from backend.agent.agent_pool import AgentPool
from backend.agent.intent_detector import intent_from_history
from backend.agent.llm_provider import ProviderFactory
from backend.agent.orchestrator import OrchestratorAgent
from backend.agent.pipeline import PipelineCoordinator
from backend.agent.tests.test_home_sse_tracing import FixedModel, HomeSSEFixture, tool_call
from backend.database import database, chat_storage
from backend.memory import conversation_memory as memory, memory_manager
from scripts.eval_review_retrieval import FixedReviewEmbedding


STORE = memory.store_conversation
OWN_Q, OWN_A = "什么是本人历史标记 OWN_QUESTION", "本人已提交 OWN_ANSWER"
OTHER_Q, OTHER_A = "为什么他人标记 FOREIGN_QUESTION", "他人已提交 FOREIGN_ANSWER"
LATER_Q, LATER_A = "为什么下降 LATER_QUESTION", "后发先完成 LATER_ANSWER"
FIRST = "今天花费多少 EARLIER_QUESTION"
LEGACY = "UNBOUNDED_LEGACY_MARKER"


def raw(messages):
    return json.dumps(messages, ensure_ascii=False, default=lambda item: vars(item))


class StorageFixture:
    def setup_chroma(self, directory):
        self.enterContext(patch.object(memory, "DB_PATH", str(directory / "chroma")))
        self.enterContext(patch.object(memory, "BGEEmbedding", FixedReviewEmbedding))
        self.chroma = memory._get_client()
        self.addCleanup(self.chroma.close)
        self.collection = memory._get_collection(self.chroma)
        # One actual client/collection per test; faults wrap the real collection.
        self.enterContext(patch.object(memory, "_get_client", return_value=self.chroma))
        self.enterContext(patch.object(memory, "_get_collection", return_value=self.collection))

    def commit_turn(self, user_id=17, question=OWN_Q, answer=OWN_A, *, index=True):
        question_id = chat_storage.save_message(user_id, "user", question, 1, None)
        answer_id = chat_storage.save_message(user_id, "assistant", answer, 1, None,
                                              user_message_id=question_id)
        args = dict(user_message_id=question_id, assistant_message_id=answer_id)
        if index:
            STORE(user_id, question, answer, **args)
        return args

    def recall(self, boundary=1000, user_id=17):
        return memory.search_conversation("历史标记", user_id, through_message_id=boundary)

    def forged_results(self, documents, metadatas):
        return patch.object(self.collection, "query", return_value={
            "documents": [documents], "metadatas": [metadatas], "distances": [[0.0] * len(documents)],
        })

    def metadata(self, **ids):
        return {"user_id": "17", "schema_version": 2, "ttl": "9999-12-31", **ids}


class ConversationStorageTests(StorageFixture, unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.database = directory / "chat.sqlite3"
        self.enterContext(patch.object(database, "get_db", self.connect))
        database.init_db()
        self.setup_chroma(directory)

    def connect(self):
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        return conn

    def test_empty_get_inserts_and_recalls_committed_owner_turn(self):
        self.assertEqual(self.collection.get(ids=["absent"])["ids"], [])
        ids = self.commit_turn()
        self.assertEqual(self.collection.count(), 1)
        self.assertIn(OWN_A, raw(self.recall(ids["assistant_message_id"] + 1)))
        self.assertEqual(self.recall(user_id=18), [])

    def test_same_second_distinct_turns_and_concurrent_duplicate_are_idempotent(self):
        with patch.object(memory, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 7, 10, 0, 0)
            first = self.commit_turn()
            second = self.commit_turn(question="同秒第二轮", answer="同秒第二答")
            with ThreadPoolExecutor(max_workers=4) as executor:
                jobs = [executor.submit(STORE, 17, OWN_Q, OWN_A, **first) for _ in range(8)]
                for job in jobs:
                    job.result(timeout=5)
        records = self.collection.get()
        self.assertEqual(len(set(records["ids"])), 2)
        self.assertEqual({m["timestamp"] for m in records["metadatas"]}, {"2026-11-07 10:00:00"})
        self.assertEqual({m["user_message_id"] for m in records["metadatas"]},
                         {first["user_message_id"], second["user_message_id"]})
        first_id = next(did for did, meta in zip(records["ids"], records["metadatas"])
                        if meta["user_message_id"] == first["user_message_id"])
        self.collection.update(ids=[first_id], documents=["tampered turn"])
        with self.assertRaisesRegex(ValueError, "conversation_memory_turn_conflict"):
            STORE(17, OWN_Q, OWN_A, **first)

    def test_uncommitted_wrong_owner_and_changed_content_cannot_be_indexed(self):
        ids = self.commit_turn(index=False)
        for user_id, question, answer, metadata in (
            (18, OWN_Q, OWN_A, ids), (17, "伪造", OWN_A, ids),
            (17, OWN_Q, OWN_A, {**ids, "assistant_message_id": 999}),
        ):
            with self.subTest(user_id=user_id, question=question, metadata=metadata):
                with self.assertRaisesRegex(ValueError, "conversation_memory_uncommitted_turn"):
                    STORE(user_id, question, answer, **metadata)
        self.assertEqual(self.collection.count(), 0)

    def test_filter_exception_never_retries_without_where_and_has_fixed_error(self):
        self.commit_turn()
        with patch.object(self.collection, "query", side_effect=RuntimeError("PRIVATE_BACKEND_ERROR")) as query:
            with self.assertLogs(memory.logger, level="WARNING") as logs:
                with self.assertRaisesRegex(memory.MemoryIsolationError, "^conversation_memory_filter_failed$"):
                    self.recall()
        self.assertEqual(query.call_count, 1)
        self.assertIn({"user_id": "17"}, query.call_args.kwargs["where"]["$and"])
        self.assertIn("conversation_memory_filter_failed", raw(logs.output))
        self.assertNotIn("PRIVATE_BACKEND_ERROR", raw(logs.output))

    def test_missing_and_foreign_receipt_owners_are_rejected(self):
        ids = self.commit_turn()
        good = self.metadata(**ids)
        with self.forged_results([LEGACY, OTHER_A, OTHER_A, memory._document(OWN_Q, OWN_A)],
                                 [None, {}, {**good, "user_id": "18"}, good]):
            recalled = raw(self.recall())
        self.assertIn(OWN_A, recalled)
        self.assertNotIn(OTHER_A, recalled)
        self.assertNotIn(LEGACY, recalled)

    def test_forged_matching_owner_and_metadata_cannot_smuggle_foreign_content(self):
        own = self.commit_turn()
        other = self.commit_turn(18, OTHER_Q, OTHER_A)
        valid = self.metadata(**own)
        with self.forged_results(
            [memory._document(OTHER_Q, OTHER_A), OTHER_A, memory._document(OWN_Q, OWN_A)],
            [self.metadata(**other), valid, {**valid, "entities": OTHER_A, "metrics": OTHER_Q,
                                           "timestamp": LEGACY, "decisions": LEGACY}],
        ):
            recalled = raw(self.recall())
        self.assertIn(OWN_A, recalled)
        for marker in (OTHER_Q, OTHER_A, LEGACY):
            self.assertNotIn(marker, recalled)

    def test_boundary_is_rechecked_even_if_backend_ignores_where(self):
        own = self.commit_turn()
        later = self.commit_turn(question=LATER_Q, answer=LATER_A)
        with self.forged_results([memory._document(LATER_Q, LATER_A), memory._document(OWN_Q, OWN_A)],
                                 [self.metadata(**later), self.metadata(**own)]):
            recalled = raw(self.recall(later["user_message_id"]))
        self.assertIn(OWN_A, recalled)
        self.assertNotIn(LATER_A, recalled)
        # An early question whose answer commits after this request is also excluded.
        with self.forged_results([LATER_A], [self.metadata(
            user_message_id=own["user_message_id"], assistant_message_id=later["assistant_message_id"],
        )]):
            self.assertEqual(self.recall(later["user_message_id"]), [])

    def test_legacy_records_and_missing_request_boundary_are_conservatively_empty(self):
        self.collection.add(ids=["legacy"], documents=[LEGACY], metadatas=[{"user_id": "17"}])
        self.assertEqual(self.recall(), [])
        with patch.object(self.collection, "query", wraps=self.collection.query) as query:
            self.assertEqual(memory.search_conversation("历史", 17), [])
        query.assert_not_called()
        with self.forged_results([LEGACY], [{"user_id": "17"}]):
            self.assertEqual(self.recall(), [])

    def test_unavailable_sqlite_source_rejects_receipt(self):
        self.commit_turn()
        with patch.object(memory, "_committed_document", side_effect=sqlite3.OperationalError("PRIVATE_DB_ERROR")):
            with self.assertLogs(memory.logger, level="WARNING") as logs:
                self.assertEqual(self.recall(), [])
        self.assertIn("conversation_memory_source_unavailable", raw(logs.output))
        self.assertNotIn("PRIVATE_DB_ERROR", raw(logs.output))

    def test_intent_snapshot_is_local_immutable_and_uses_only_user_history(self):
        history = [{"role": "user", "content": "什么是广告知识"},
                   {"role": "assistant", "content": "为什么下降"},
                   {"role": "user", "content": FIRST}]
        snapshot = intent_from_history(history)
        intent_from_history([{"role": "user", "content": LATER_Q}])
        self.assertEqual((snapshot.current, snapshot.last), ("query_data", "query_knowledge"))
        self.assertEqual(intent_from_history([history[-1]]).last, None)
        self.assertEqual(intent_from_history(history), snapshot)
        with self.assertRaises(FrozenInstanceError):
            snapshot.current = "analyze"


class IsolationModel(FixedModel):
    def __init__(self):
        super().__init__()
        self.inputs = []
        self.mode = "main"
        self.lock = threading.Lock()

    def create(self, messages, tools=None, **kwargs):
        names = {tool["function"]["name"] for tool in tools or []}
        receipts = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
        last = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        stage = ("plan" if kwargs.get("max_tokens") == 500 else
                 "orchestrator" if "query_data" in names else
                 "knowledge" if "search_knowledge" in names else
                 "analysis" if "compare_plans" in names else
                 "stream_reply" if kwargs.get("stream") else "data")
        with self.lock:
            self.inputs.append((stage, deepcopy(messages)))
        if stage == "plan":
            return self.response('{"steps": []}', [])
        if self.mode == "fail_all":
            raise RuntimeError("PRIVATE_MODEL_ERROR")
        reply = LATER_A if last == LATER_Q else "固定本轮回复"
        if stage == "orchestrator":
            if "recall" not in receipts:
                return self.response(None, [tool_call(
                    "recall", name="search_conversation_memory", query="历史标记",
                    user_id=18, through_message_id=999999,  # model-supplied scope is ignored
                )])
            if self.mode == "reply_fallback":
                raise RuntimeError("offline orchestrator failure")
            if self.mode == "sub_agents" and "dispatch_data" not in receipts:
                return self.response(None, [
                    tool_call("dispatch_data", name="query_data", query="SUB_QUESTION"),
                    tool_call("dispatch_analysis", name="analyze_data", query="ANALYSIS_QUESTION"),
                    tool_call("dispatch_knowledge", name="query_knowledge_base", question="KNOWLEDGE_QUESTION"),
                ])
        if stage == "data" and last == "SUB_QUESTION" and not receipts:
            return self.response(None, [tool_call(
                "peer", name="call_peer", peer_name="query_knowledge_base", question="PEER_QUESTION",
            )])
        if kwargs.get("stream"):
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=reply))])])
        return self.response(reply, [])


class HomeMemoryIsolationTests(StorageFixture, HomeSSEFixture):
    model_class = IsolationModel

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.memory_store.side_effect = STORE
        self.setup_chroma(self.database.parent)
        self.loop = asyncio.get_running_loop()
        if archive := getattr(self, "archive_model_inputs", None):
            self.addCleanup(lambda: archive(self.id(), self.model.inputs))

    def assert_clean(self, inputs, *markers):
        self.assertTrue(inputs)
        for stage, messages in inputs:
            with self.subTest(stage=stage):
                for marker in markers:
                    self.assertNotIn(marker, raw(messages))

    async def test_owner_recall_survives_pool_replacement_and_sqlite_recent_window(self):
        await self.request(message=OWN_Q)
        self.assertEqual(self.collection.count(), 1)
        with closing(self.connect()) as conn, conn:
            conn.executemany("INSERT INTO conversations (user_id, role, content) VALUES (17, 'user', ?)",
                             [(f"padding {index}",) for index in range(55)])
        with patch.object(ProviderFactory, "create", return_value=self.model):
            replacement = AgentPool(min_size=1, max_size=1)
        self.model.inputs.clear()
        with patch.object(self.server, "agent_pool", replacement):
            events = []
            await self.request(message="还记得本人历史吗", events=events)
        self.assertEqual(events[-1]["type"], "done")
        main = [messages for stage, messages in self.model.inputs if stage == "orchestrator"]
        self.assertNotIn(OWN_Q, raw(main[0]))  # must come from Chroma, not the recent SQLite window
        self.assertIn(OWN_Q, raw(main[-1]))
        self.assertIn("固定本轮回复", raw(main[-1]))
        self.assertEqual(self.collection.count(), 2)
        self.assertEqual(replacement.stats["available"], 1)

    async def test_interleaved_users_reuse_same_pool_instance_and_ignore_spoofed_identity(self):
        self.commit_turn()
        self.commit_turn(18, OTHER_Q, OTHER_A)
        token18 = self.sign_token(user_id=18)
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO sessions (user_id, token) VALUES (?, ?)", (18, token18))
        instance = self.pool._pool[0]
        with patch.object(self.collection, "query", wraps=self.collection.query) as query:
            for user_id, token, expected, forbidden in (
                (17, self.token, OWN_A, OTHER_A), (18, token18, OTHER_A, OWN_A),
                (17, self.token, OWN_A, OTHER_A),
            ):
                start = len(self.model.inputs)
                events = []
                await self.request(token=token, body={"message": "还记得历史标记吗", "user_id": 999,
                                                      "through_message_id": 999999}, events=events)
                inputs = self.model.inputs[start:]
                self.assertEqual(events[-1]["type"], "done")
                self.assertIn(expected, raw(inputs))
                self.assert_clean(inputs, forbidden)
                self.assertIn({"user_id": str(user_id)}, query.call_args.kwargs["where"]["$and"])
                self.assertIs(self.pool._pool[0], instance)

    async def assert_later_first(self, mode):
        self.commit_turn()
        self.commit_turn(18, OTHER_Q, OTHER_A)
        with patch.object(ProviderFactory, "create", return_value=self.model):
            pool = AgentPool(min_size=2, max_size=2)
        saved, resume = asyncio.Event(), threading.Event()
        save = PipelineCoordinator.save_conversation
        create_plan = OrchestratorAgent._create_plan

        def pause(agent, user_id, role, content, **kwargs):
            message_id = save(agent, user_id, role, content, **kwargs)
            if role == "user" and content == FIRST:
                self.loop.call_soon_threadsafe(saved.set)
                if not resume.wait(timeout=15):
                    raise TimeoutError("offline save gate")
            return message_id

        def plan(agent, message, **kwargs):
            if mode == "stream_fallback" and message == FIRST:
                raise RuntimeError("offline planning failure")
            return create_plan(agent, message, **kwargs)

        events_a, events_b = [], []
        with patch.object(self.server, "agent_pool", pool), \
             patch.object(PipelineCoordinator, "save_conversation", pause), \
             patch.object(OrchestratorAgent, "_create_plan", plan):
            task_a = asyncio.create_task(self.request(message=FIRST, events=events_a))
            try:
                await asyncio.wait_for(saved.wait(), timeout=10)
                await asyncio.wait_for(self.request(message=LATER_Q, events=events_b), timeout=10)
                self.assertEqual(events_b[-1]["type"], "done")
                self.assertIn(LATER_A, raw(self.collection.get()))
                memory_manager.update_business_memory(17, "account", {"account_name": LEGACY})
                memory_manager.update_profile(17, LEGACY)
                memory_manager.store_conversation_summary(17, LEGACY, LEGACY)
                start = len(self.model.inputs)
                self.model.mode = mode
            finally:
                resume.set()
                await asyncio.wait_for(task_a, timeout=15)
        inputs = self.model.inputs[start:]
        self.assert_clean(inputs, LATER_Q, LATER_A, OTHER_Q, OTHER_A, LEGACY)
        self.assertEqual(events_a[-1]["type"], "done")
        self.assertEqual(sum(e["type"] == "done" for e in events_a), 1)
        stages = {stage for stage, _ in inputs}
        if mode != "stream_fallback":
            main = [messages for stage, messages in inputs if stage == "orchestrator"]
            self.assertTrue(any(m.get("tool_call_id") == "recall" and OWN_A in m["content"]
                                for m in main[-1]))
            self.assertIn(intent_from_history([
                {"role": "user", "content": OWN_Q}, {"role": "user", "content": FIRST},
            ]).prompt, main[0][0]["content"])
        if mode == "sub_agents":
            self.assertTrue({"data", "analysis", "knowledge"} <= stages)
            self.assertTrue(any(stage == "knowledge" and messages[-1]["content"] == "PEER_QUESTION"
                                for stage, messages in inputs))
            self.assertTrue(any(m.get("tool_call_id") == "peer" for _, messages in inputs for m in messages))
        if mode == "reply_fallback":
            self.assertIn("data", stages)
        if mode == "stream_fallback":
            self.assertTrue({"data", "stream_reply"} <= stages)
        self.assertEqual(pool.stats["available"], 2)
        self.assertEqual(self.collection.count(), 4)

    async def test_same_user_later_completion_is_excluded_from_main_and_intent(self):
        await self.assert_later_first("main")

    async def test_same_user_later_completion_is_excluded_from_all_sub_agents_and_peer(self):
        await self.assert_later_first("sub_agents")

    async def test_same_user_later_completion_is_excluded_from_sync_fallback(self):
        await self.assert_later_first("reply_fallback")

    async def test_same_user_later_completion_is_excluded_from_stream_fallback(self):
        await self.assert_later_first("stream_fallback")

    async def test_filter_failure_is_traced_and_never_enters_any_model_message(self):
        self.commit_turn(18, OTHER_Q, OTHER_A)
        with patch.object(self.collection, "query", side_effect=RuntimeError(OTHER_A)) as query:
            await self.request(message=FIRST)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(self.events[-1]["type"], "done")
        self.assert_clean(self.model.inputs, OTHER_Q, OTHER_A)
        trace = observability.get_trace(self.events[-1]["trace_id"])
        errors = [s["error_code"] for s in trace["steps"] if s.get("error_code")]
        self.assertEqual(errors, ["conversation_memory_filter_failed"])
        self.assertIn("conversation_memory_filter_failed", raw(self.model.inputs))

    async def test_malformed_owner_receipts_never_enter_main_sub_agents_or_fallback(self):
        self.commit_turn()
        for mode in ("main", "sub_agents", "reply_fallback"):
            self.model.mode = mode
            start = len(self.model.inputs)
            with self.forged_results([OTHER_A, OTHER_Q, LEGACY],
                                     [None, {"user_id": "18"}, {"user_id": "17"}]):
                await self.request(message=FIRST, events=[])
            self.assert_clean(self.model.inputs[start:], OTHER_A, OTHER_Q, LEGACY)

    async def test_reply_save_failure_cannot_write_vector_memory(self):
        with closing(self.connect()) as conn, conn:
            conn.execute("""CREATE TRIGGER fail_reply BEFORE INSERT ON conversations
                WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT, 'PRIVATE_SAVE_ERROR'); END""")
        await self.request(message=FIRST)
        self.assertEqual([e["type"] for e in self.events], ["thinking", "text", "error"])
        self.memory_store.assert_not_called()
        self.assertEqual(self.collection.count(), 0)
        self.assertEqual(self.assistant_replies(), [])

    async def test_disconnect_during_reply_save_still_indexes_committed_turn(self):
        reply_committed = asyncio.Event()
        resume_save = threading.Event()
        original_save = PipelineCoordinator.save_conversation

        def pause_after_commit(agent, user_id, role, content, **kwargs):
            message_id = original_save(agent, user_id, role, content, **kwargs)
            if role == "assistant":
                self.loop.call_soon_threadsafe(reply_committed.set)
                if not resume_save.wait(5):
                    raise TimeoutError("probe reply save was not resumed")
            return message_id

        with patch.object(PipelineCoordinator, "save_conversation", pause_after_commit):
            request = asyncio.create_task(self.request(message=FIRST))
            try:
                await asyncio.wait_for(reply_committed.wait(), timeout=5)
                self.assertEqual(self.assistant_replies(), ["固定本轮回复"])
                self.memory_store.assert_not_called()
                self.disconnect.set()
                await asyncio.wait_for(self.disconnect_seen.wait(), timeout=5)
            finally:
                resume_save.set()
                await asyncio.wait_for(request, timeout=5)

        self.assertEqual([event["type"] for event in self.events], ["thinking", "text"])
        self.memory_store.assert_called_once()
        self.assertEqual(self.collection.count(), 1)
        boundary = self.memory_store.call_args.kwargs["assistant_message_id"] + 1
        recalled = raw(self.recall(boundary=boundary))
        self.assertIn(FIRST, recalled)
        self.assertIn("固定本轮回复", recalled)
        trace = observability.get_trace(self.events[0]["trace_id"])
        self.assertEqual(trace["status"], "completed")
        self.assertEqual(self.pool.stats["available"], 1)

    async def test_memory_write_failure_is_independent_of_committed_done_and_pool_release(self):
        with patch.object(self.collection, "add", side_effect=RuntimeError("PRIVATE_VECTOR_ERROR")), \
             patch.object(memory_manager, "store_conversation_summary", side_effect=RuntimeError("PRIVATE_SUMMARY_ERROR")):
            await self.request(message=FIRST)
        self.assertEqual([e["type"] for e in self.events], ["thinking", "text", "done"])
        self.assertEqual(self.terminal_observations, [("completed", ["固定本轮回复"])])
        trace = observability.get_trace(self.events[-1]["trace_id"])
        errors = [s for s in trace["steps"] if s.get("error_code")]
        self.assertEqual([(s["stage"], s["error_code"]) for s in errors],
                         [("memory", "conversation_memory_write_failed"),
                          ("memory", "conversation_summary_write_failed")])
        self.assertNotIn("PRIVATE_VECTOR_ERROR", raw(trace))
        self.assertNotIn("PRIVATE_SUMMARY_ERROR", raw(trace))
        self.assertEqual(self.pool.stats["available"], 1)

    async def test_failed_generation_and_unauthenticated_request_do_not_index(self):
        await self.request(authenticated=False)
        self.assertEqual(self.status_code, 401)
        self.assertEqual(self.model.inputs, [])
        self.model.mode = "fail_all"
        await self.request(message=FIRST)
        self.assertEqual(self.events[-1]["type"], "error")
        self.memory_store.assert_not_called()
        self.assertEqual(self.collection.count(), 0)


if __name__ == "__main__":
    unittest.main()
