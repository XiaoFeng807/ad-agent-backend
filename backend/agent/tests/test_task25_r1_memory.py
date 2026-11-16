"""TASK-025-R1: fixed SDK/embedding, real FastAPI SSE and isolated SQL/Chroma."""
import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from backend import observability
from backend.agent.agent_pool import AgentPool
from backend.agent.llm_provider import ProviderFactory
from backend.agent.pipeline import PipelineCoordinator
from backend.agent.tests.test_chat_memory_isolation import IsolationModel, StorageFixture, STORE, raw
from backend.agent.tests.test_home_sse_tracing import HomeSSEFixture, tool_call
from backend.database import chat_groups, chat_storage, database
from backend.memory import conversation_memory as memory


class AuditModel(IsolationModel):
    def __init__(self):
        super().__init__()
        self.requests = []

    def create(self, messages, **kwargs):
        self.requests.append(deepcopy({"messages": messages, **kwargs}))
        self.inputs.append(("main", deepcopy(messages)))
        names = {t["function"]["name"] for t in kwargs.get("tools") or []}
        question = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        answer = "固定答复 " + question
        calls = []
        if kwargs.get("max_tokens") == 500:
            answer = '{"steps": []}'
        elif "search_conversation_memory" in names and not any(m.get("tool_call_id") == "recall" for m in messages):
            calls = [tool_call("recall", name="search_conversation_memory", query="历史标记")]
            answer = None
        if kwargs.get("stream"):
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=answer))])])
        return self.response(answer, calls)


class FrozenClock(datetime):
    instant = datetime(2026, 11, 12, 16, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


class Task25MemoryTests(StorageFixture, HomeSSEFixture):
    model_class = AuditModel

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.memory_store.side_effect = STORE
        self.setup_chroma(self.database.parent)
        self.evidence = []
        self.addCleanup(self.archive)

    def archive(self):
        if root := os.environ.get("TASK25_R1_EVIDENCE"):
            path = Path(root) / self._testMethodName
            path.mkdir(parents=True, exist_ok=True)
            for name, data in (("sdk-requests", self.model.requests), ("checks", self.evidence)):
                (path / f"{name}.json").write_text(raw(data), encoding="utf-8")
            if self.trace_file.exists():
                (path / "traces.jsonl").write_bytes(self.trace_file.read_bytes())
            with closing(self.connect()) as conn:
                state = {table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
                         for table in ("conversations", "conversation_turns", "chat_group_runs", "chat_group_entries")}
            (path / "sql.json").write_text(raw(state), encoding="utf-8")

    def pad(self, group=None):
        if group:
            for i in range(26):
                run, _ = chat_groups.accept(17, group, f"pad-{i}", f"无关问题 {i}", 1)
                run.save_reply(f"无关回复 {i}", 1)
            return
        with closing(self.connect()) as conn, conn:
            conn.executemany("INSERT INTO conversations (user_id,role,content) VALUES (17,'user',?)",
                             [(f"无关填充 {i}",) for i in range(55)])

    async def probe(self, question, answer, ids, *, ttl="2026-11-13", group=None, allow=False,
                    clock="2026-11-12T15:59:59+00:00"):
        meta = self.metadata(**ids)
        if ttl is None:
            meta.pop("ttl")
        else:
            meta["ttl"] = ttl
        if group:
            meta.update(schema_version=3, storage_namespace="sqlite", conversation_id=group,
                        content_sha256=memory._content_digest((question, answer)))
        doc = memory._document(question, answer)
        self.model.inputs.clear()
        start = len(self.model.requests)
        FrozenClock.instant = datetime.fromisoformat(clock)
        events = []
        with patch.object(memory, "datetime", FrozenClock), self.forged_results([doc], [meta]) as query:
            await self.request(body={"message": "过去讨论过的冷链运输中，如何防止鲜花花瓣冻伤？",
                                     **({"conversation_id": group} if group else {})}, events=events)
        self.assertEqual(query.call_count, 1)  # The candidate reached the SQL/TTL verification boundary.
        self.assertEqual([e["type"] for e in events if e["type"] in {"done", "error"}], ["done"])
        messages = [m for request in self.model.requests[start:] for m in request["messages"]]
        if allow:
            self.assertTrue(any(m["role"] == "tool" and doc in json.loads(m["content"]).get("result", "")
                                for m in messages))
            self.assertTrue(all(question not in raw(m) and answer not in raw(m)
                                for m in messages if m["role"] != "tool"))
        else:
            for marker in (question, answer, *re.findall(r"T25_[A-Z_]+", question + " " + answer)):
                self.assertNotIn(marker, raw(messages))
        self.evidence.append({"candidate": {"document": doc, "metadata": meta}, "clock": clock,
                              "allow": allow, "query_calls": query.call_count, "sse": events,
                              "sdk_request_range": [start, len(self.model.requests)]})

    def seed_group(self, question, answer):
        group = chat_groups.create(17, "固定记忆回归")["conversation_id"]
        run, _ = chat_groups.accept(17, group, "seed-r1", question, 1)
        answer_id, _ = run.save_reply(answer, 1)
        STORE(17, question, answer, user_message_id=run.message_id, assistant_message_id=answer_id,
              conversation_id=group)
        return group, {"user_message_id": run.message_id, "assistant_message_id": answer_id}

    async def test_ttl_midnight_and_invalid_values_both_entrypoints(self):
        for group_mode in (False, True):
            q, a = f"R1_TTL_{group_mode}_Q", f"R1_TTL_{group_mode}_A"
            if group_mode:
                group, ids = self.seed_group(q, a)
            else:
                group, ids = None, self.commit_turn(question=q, answer=a)
            self.pad(group)
            for clock, allow in (("2026-11-12T15:59:59+00:00", True),
                                 ("2026-11-12T16:00:00+00:00", False),
                                 ("2026-11-12T16:00:01+00:00", False)):
                await self.probe(q, a, ids, group=group, clock=clock, allow=allow)
            for ttl in (None, "", 20261113, "not-a-date", "2026-1-13", "2026/11/13", "2026-02-30",
                        "2026-11-13 ", "2026-11-13T00:00:00", "２０２６-１１-１３", "0000-01-01"):
                await self.probe(q, a, ids, group=group, ttl=ttl)

    async def test_original_three_failures_reach_verification_and_leave_zero_markers(self):
        # Original T1 seed text and original query; the fixed input clock is the same.
        from scripts.verify_task25_t1 import SEEDS, marker
        for suffix, ttl in (("edge", "2026-11-12"), ("invalid", "not-a-date")):
            source = "ttl_" + suffix
            q, a = (SEEDS[source][0] + " " + marker(source, "Q"),
                    SEEDS[source][1] + " " + marker(source, "A"))
            group, ids = self.seed_group(q, a)
            self.pad(group)
            await self.probe(q, a, ids, group=group, ttl=ttl, clock="2026-11-11T16:00:00+00:00")
        turns = []
        for source in ("legacy_first", "legacy_second"):
            q, a = SEEDS[source][0] + " " + marker(source, "Q"), SEEDS[source][1] + " " + marker(source, "A")
            turns.append((q, a, self.commit_turn(question=q, answer=a)))
        self.pad()
        await self.probe(turns[0][0], turns[1][1],
                         {**turns[0][2], "assistant_message_id": turns[1][2]["assistant_message_id"]})

    async def test_legal_sse_turn_and_cross_turn_with_correct_digest(self):
        actual_add = self.collection.add

        def committed_before_vector(**kwargs):
            meta, = kwargs["metadatas"]
            with closing(self.connect()) as conn:
                pair = chat_storage.committed_turn(conn, 17, meta["user_message_id"], meta["assistant_message_id"])
            self.assertIsNotNone(pair)  # Separate connection proves both reply and pair have committed.
            self.evidence.append({"before_vector_add": {"metadata": meta, "committed_pair": pair}})
            return actual_add(**kwargs)

        with patch.object(self.collection, "add", side_effect=committed_before_vector):
            await self.request(message="R1_NEW_SSE_Q")
        with closing(self.connect()) as conn:
            pair = dict(conn.execute("SELECT * FROM conversation_turns").fetchone())
            q, a = chat_storage.committed_turn(conn, 17, pair["user_message_id"], pair["assistant_message_id"])
        ids = {k: pair[k] for k in ("user_message_id", "assistant_message_id")}
        self.pad()
        await self.probe(q, a, ids, allow=True)
        later = self.commit_turn(question="R1_SECOND_Q", answer="R1_SECOND_A")
        self.pad()
        mixed = {**ids, "assistant_message_id": later["assistant_message_id"],
                 "content_sha256": memory._content_digest((q, "R1_SECOND_A"))}
        await self.probe(q, "R1_SECOND_A", mixed)
        with self.assertRaisesRegex(ValueError, "uncommitted_turn"):
            STORE(17, q, "R1_SECOND_A", user_message_id=ids["user_message_id"],
                  assistant_message_id=later["assistant_message_id"])

    async def test_concurrent_reverse_completion_with_id_holes(self):
        saved, resume = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        save = PipelineCoordinator.save_conversation
        with patch.object(ProviderFactory, "create", return_value=self.model):
            pool = AgentPool(min_size=2, max_size=2)

        def pause(agent, user_id, role, content, **kwargs):
            result = save(agent, user_id, role, content, **kwargs)
            if role == "user" and content == "R1_SLOW_Q":
                loop.call_soon_threadsafe(saved.set)
                if not resume.wait(15):
                    raise TimeoutError("fixed gate")
            return result

        first, second = [], []
        with patch.object(self.server, "agent_pool", pool), patch.object(PipelineCoordinator, "save_conversation", pause):
            pending = asyncio.create_task(self.request(message="R1_SLOW_Q", events=first))
            try:
                await asyncio.wait_for(saved.wait(), 10)
                with closing(self.connect()) as conn, conn:
                    conn.execute("INSERT INTO conversations (id,user_id,role,content) VALUES (100,18,'user','hole')")
                    conn.execute("DELETE FROM conversations WHERE id=100")
                await self.request(message="R1_FAST_Q", events=second)
            finally:
                resume.set()
                await asyncio.wait_for(pending, 20)
        with closing(self.connect()) as conn:
            rows = [dict(r) for r in conn.execute("""SELECT t.*,q.content AS question,a.content AS answer
                FROM conversation_turns t JOIN conversations q ON q.id=t.user_message_id
                JOIN conversations a ON a.id=t.assistant_message_id ORDER BY t.assistant_message_id""")]
        self.assertEqual([r["question"] for r in rows], ["R1_FAST_Q", "R1_SLOW_Q"])
        self.assertGreater(rows[0]["user_message_id"], 100)
        self.assertEqual(rows[1]["user_message_id"], 1)
        self.assertEqual(self.collection.count(), 2)
        self.assertEqual([e["type"] for e in first if e["type"] in {"done", "error"}], ["done"])
        self.assertEqual([e["type"] for e in second if e["type"] in {"done", "error"}], ["done"])
        self.evidence.append({"committed_pairs_in_completion_order": rows, "sse": [first, second]})

    async def test_reply_or_association_failure_rolls_back_and_never_indexes(self):
        for table in ("conversations", "conversation_turns"):
            with closing(self.connect()) as conn, conn:
                condition = "WHEN NEW.role='assistant'" if table == "conversations" else ""
                conn.execute(f"CREATE TRIGGER reject_reply BEFORE INSERT ON {table} {condition} "
                             "BEGIN SELECT RAISE(ABORT,'fixed save failure'); END")
            events = []
            with patch.object(self.collection, "add", wraps=self.collection.add) as add:
                await self.request(message="R1_FAILURE_Q", events=events)
            self.assertEqual([e["type"] for e in events if e["type"] in {"done", "error"}], ["error"])
            add.assert_not_called()
            with closing(self.connect()) as conn, conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM conversation_turns").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM conversations WHERE role='assistant'").fetchone()[0], 0)
                conn.execute("DROP TRIGGER reject_reply")
            self.evidence.append({"failed_table": table, "vector_calls": add.call_count, "sse": events})

    async def test_legacy_upgrade_preserves_history_and_only_trusted_pairs_can_reindex(self):
        # Same view, adjacent IDs, matching text and digest still lack provenance.
        q, a = "R1_OLD_Q", "R1_OLD_A"
        with closing(self.connect()) as conn, conn:
            conn.execute("DROP TABLE conversation_turns")
            conn.execute("INSERT INTO conversations VALUES (1,17,'user',?,CURRENT_TIMESTAMP,1,'same')", (q,))
            conn.execute("INSERT INTO conversations VALUES (2,17,'assistant',?,CURRENT_TIMESTAMP,1,'same')", (a,))
            before = [tuple(r) for r in conn.execute("SELECT * FROM conversations")]
        for _ in range(3):
            database.init_db()
        with closing(self.connect()) as conn:
            self.assertEqual([tuple(r) for r in conn.execute("SELECT * FROM conversations")], before)
            self.assertEqual(conn.execute("SELECT count(*) FROM conversation_turns").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "uncommitted_turn"):
            STORE(17, q, a, user_message_id=1, assistant_message_id=2)
        self.collection.add(ids=["old-pair"], documents=[memory._document(q, a)],
                            metadatas=[self.metadata(user_message_id=1, assistant_message_id=2)])
        self.pad()
        await self.probe(q, a, {"user_message_id": 1, "assistant_message_id": 2,
                               "content_sha256": memory._content_digest((q, a))})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixed") as client:
            response = await client.get("/api/chat/conversations/legacy-17/history",
                                        headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(response.status_code, 200)
        self.assertIn(q, response.text)
        self.assertIn(a, response.text)
        self.evidence.append({"history_api": response.json(), "old_rows": before, "initializations": 3})
        trusted = self.commit_turn(question="R1_TRUSTED_Q", answer="R1_TRUSTED_A", index=False)
        STORE(17, "R1_TRUSTED_Q", "R1_TRUSTED_A", **trusted)
        self.assertIn("R1_TRUSTED_A", raw(self.recall()))

    async def test_writes_and_purge_share_shanghai_expiry(self):
        FrozenClock.instant = datetime.fromisoformat("2026-11-12T16:00:00+00:00")
        with patch.object(memory, "datetime", FrozenClock):
            for group_mode in (False, True):
                q, a = f"R1_STORE_{group_mode}_Q", f"R1_STORE_{group_mode}_A"
                if group_mode:
                    self.seed_group(q, a)
                else:
                    self.commit_turn(question=q, answer=a)
            data = self.collection.get()
            self.assertEqual({m["ttl"] for m in data["metadatas"]}, {"2026-12-13"})
            self.assertEqual({m["timestamp"] for m in data["metadatas"]}, {"2026-11-13 00:00:00"})
        self.collection.delete(ids=data["ids"])
        values = [None, "not-a-date", "2026-1-13", "2026-02-30", "2026-11-12", "2026-11-13", "2026-11-14"]
        self.collection.add(ids=[str(i) for i in range(len(values))], documents=["purge"] * len(values),
                            metadatas=[{"user_id": "17", **({"ttl": v} if v else {})} for v in values])
        for instant, expected in (("2026-11-12T15:59:59+00:00", 5), ("2026-11-12T16:00:00+00:00", 1),
                                  ("2026-11-12T16:00:01+00:00", 0)):
            FrozenClock.instant = datetime.fromisoformat(instant)
            with patch.object(memory, "datetime", FrozenClock):
                self.assertEqual(memory._purge_expired(), expected)
            self.evidence.append({"clock": instant, "deleted": expected, "remaining": self.collection.get()})
