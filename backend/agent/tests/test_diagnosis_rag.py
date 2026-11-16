"""Exercise real ASGI routes, knowledge tools, Chroma and user reports."""

import asyncio
import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from backend.agent.tests.test_ad_diagnosis_eval import SEVEN_DAYS, STATE_CALLS, StatefulDiagnosisModel
from backend.agent.tests.test_home_sse_tracing import HomeSSEFixture
from backend.database import database
from backend.rag import rag_knowledge
from backend.rag.review_rule_demo import DEMO_RULES, SOURCE, seed_review_rules
from backend.tools import tools
from scripts.eval_review_retrieval import FixedReviewEmbedding


REFERENCE = "ctr-review-v1#low-ctr"
CHUNK_ID, RULE_ID, VERSION, TITLE, ORIGINAL = DEMO_RULES[0]
URL = f"/api/knowledge/chunks/{CHUNK_ID}"


class DiagnosisRAGTests(HomeSSEFixture):
    model_class = StatefulDiagnosisModel

    async def asyncSetUp(self):
        await super().asyncSetUp()
        database.init_db()
        self.enterContext(patch.object(tools, "get_db", self.connect))
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO ad_plans (id, plan_name, user_id) VALUES (101, 'demo plan', 17)")
            conn.executemany("INSERT INTO daily_reports (report_date, user_id, impressions, clicks) VALUES (?, ?, 200, 10)",
                             [(window["start_date"], user_id)
                              for window in SEVEN_DAYS.values() for user_id in (17, 18)])
        self.enterContext(patch.object(rag_knowledge.settings, "RAG_DB_PATH", str(self.database.parent / "rag")))
        self.enterContext(patch.object(rag_knowledge, "BGEEmbedding", FixedReviewEmbedding))
        self.rag = self.enterContext(closing(rag_knowledge.get_rag_knowledge()))
        self.addCleanup(rag_knowledge._open_knowledge.cache_clear)
        self.client = await self.enterAsyncContext(AsyncClient(
            transport=ASGITransport(app=self.server.app), base_url="http://test",
        ))

    async def diagnose(self, *, rule_id=REFERENCE, task_id=None):
        params = {"task_id": task_id} if task_id else {"plan_id": 101, "rule_id": rule_id}
        events = []
        await asyncio.wait_for(self.request(body={"diagnosis": {**params, **SEVEN_DAYS}}, events=events), timeout=10)
        self.events = events
        self.assertEqual([event["type"] for event in events], ["thinking", "text", "done"])
        self.assert_trace("completed", expected_calls=STATE_CALLS, trace_count=len(self.terminal_observations))
        reply = events[1]
        self.assertIn("缺少该计划在基准窗口和当前窗口的历史 CTR", reply["content"])
        self.assertNotIn("缺少成功回执", reply["content"])
        with closing(self.connect()) as conn:
            stored = conn.execute("SELECT reply FROM diagnosis_turns ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(stored["reply"], reply["content"])
        return reply

    def knowledge_receipt(self):
        content = next(message["content"] for _, messages in reversed(self.model.inputs) for message in messages
                       if message.get("tool_call_id") == "call_search")
        return json.loads(json.loads(content))

    async def test_cites_exact_retrieval_and_reads_original(self):
        seed_review_rules(self.rag)
        reply = await self.diagnose()
        receipt = self.knowledge_receipt()
        self.assertIn(ORIGINAL, receipt["context"])
        self.assertIn(SOURCE, receipt["sources"])
        self.assertEqual(reply["citations"], [{
            "chunk_id": CHUNK_ID, "rule_id": RULE_ID, "version": VERSION, "title": TITLE, "url": URL,
        }])
        self.assertIn(URL, reply["content"])
        self.assertNotIn("本轮缺项", reply["content"])
        response = await self.client.get(reply["citations"][0]["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "chunk_id": CHUNK_ID, "rule_id": RULE_ID, "version": VERSION,
            "title": TITLE, "source": SOURCE, "content": ORIGINAL,
        })
        self.assertEqual((await self.client.get("/api/knowledge/chunks/unknown-chunk")).status_code, 404)
        search = (await self.client.get("/api/knowledge/search", params={"q": REFERENCE})).json()
        self.assertEqual([hit["chunk_id"] for hit in search["evidence"]], [CHUNK_ID])
        self.assertEqual(search["results"], [ORIGINAL])

    async def test_temporary_rule_search_failure_retries_and_cites_fresh_chunk(self):
        seed_review_rules(self.rag)
        generate = self.rag.generate
        attempts = []

        def search(query):
            attempts.append(query)
            if len(attempts) == 1:
                raise TimeoutError("temporary search timeout")
            return generate(query)

        with patch.object(self.rag, "generate", side_effect=search):
            await asyncio.wait_for(self.request(body={"diagnosis": {
                "plan_id": 101, "rule_id": REFERENCE, **SEVEN_DAYS,
            }}), timeout=10)

        self.assertEqual(attempts, [REFERENCE, REFERENCE])
        self.assertEqual([event["type"] for event in self.events], ["thinking", "text", "done"])
        self.assertEqual([citation["chunk_id"] for citation in self.events[1]["citations"]], [CHUNK_ID])
        handoff = next(json.loads(message["content"]) for names, messages in self.model.inputs
                       if "query_data" in names for message in messages
                       if message.get("tool_call_id") == "call_knowledge")
        failed, recovered = handoff["receipts"]
        self.assertEqual((failed["status"], failed["chunk_ids"]), ("temporary_failure", []))
        self.assertEqual((recovered["retry_of"], recovered["status"], recovered["chunk_ids"]),
                         (failed["runtime_call_id"], "ok", [CHUNK_ID]))
        self.assert_trace("completed", expected_calls=STATE_CALLS | {
            recovered["runtime_call_id"]: ("search_knowledge", "知识助手"),
        }, failed_calls={"call_search"})

    async def test_exact_rule_survives_hybrid_candidate_cutoff(self):
        self.rag.add_text(ORIGINAL, {
            "rule_id": RULE_ID, "version": VERSION, "title": TITLE, "source": SOURCE,
        }, doc_id=CHUNK_ID)
        distractors = [f"ctr-review-v2-low-ctr-{index:02d}" for index in range(1, 4)]
        for chunk_id in distractors:
            self.rag.add_text(f"参见 {REFERENCE}", {
                "rule_id": RULE_ID, "version": "v2", "title": TITLE, "source": SOURCE,
            }, doc_id=chunk_id)
        ranked = [hit["chunk_id"] for hit in self.rag.hybrid_search(REFERENCE, top_k=4)]
        self.assertCountEqual(ranked[:3], distractors)
        self.assertEqual(ranked[3:], [CHUNK_ID])

        reply = await self.diagnose()
        candidates = [hit["chunk_id"] for hit in self.knowledge_receipt()["evidence"]]
        self.assertEqual((candidates, reply["citations"]), ([CHUNK_ID], [{
            "chunk_id": CHUNK_ID, "rule_id": RULE_ID, "version": VERSION, "title": TITLE, "url": URL,
        }]))
        response = await self.client.get(reply["citations"][0]["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], ORIGINAL)

        task_id = self.events[-1]["task_id"]
        self.rag.collection.delete(ids=[CHUNK_ID])
        self.assertCountEqual(self.rag.collection.get(include=[])["ids"], distractors)
        reply = await self.diagnose(task_id=task_id)
        self.assertEqual(self.knowledge_receipt(), {"context": "", "sources": [], "evidence": []})
        self.assertEqual(reply["citations"], [])
        self.assertIn("未找到指定规则的精确版本证据", reply["content"])
        self.assertEqual((await self.client.get(URL)).status_code, 404)

    async def test_adjacent_versions_are_not_evidence(self):
        seed_review_rules(self.rag)
        reply = await self.diagnose(rule_id="ctr-review-v3#low-ctr")
        self.assertEqual(reply["citations"], [])
        self.assertIn("未找到指定规则的精确版本证据", reply["content"])
        self.assertEqual(self.knowledge_receipt()["evidence"], [])

    async def test_semantic_neighbour_is_not_evidence(self):
        seed_review_rules(self.rag)
        reply = await self.diagnose(rule_id="ctr-review-v1#creative")
        self.assertEqual(reply["citations"], [])
        self.assertIn("未找到指定规则的精确版本证据", reply["content"])
        self.assertEqual(self.knowledge_receipt()["evidence"], [])

    async def test_empty_user_report_is_missing_even_when_query_succeeds(self):
        seed_review_rules(self.rag)
        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM daily_reports WHERE user_id=17 AND report_date=?",
                         (SEVEN_DAYS["baseline"]["start_date"],))
        reply = await self.diagnose()
        self.assertIn("基准窗口用户日报无数据", reply["content"])
        self.assertNotIn("当前窗口用户日报无数据", reply["content"])
        self.assertEqual([item["chunk_id"] for item in reply["citations"]], [CHUNK_ID])

    async def test_empty_collection_is_missing_even_when_query_succeeds(self):
        reply = await self.diagnose()
        self.assertEqual(reply["citations"], [])
        self.assertIn("未找到指定规则的精确版本证据", reply["content"])
        self.assertEqual(self.knowledge_receipt(), {"context": "", "sources": [], "evidence": []})

    async def test_followup_retrieves_fresh_evidence(self):
        seed_review_rules(self.rag)
        first_reply = await self.diagnose()
        self.assertEqual([item["chunk_id"] for item in first_reply["citations"]], [CHUNK_ID])
        task_id = self.events[-1]["task_id"]
        self.rag.collection.delete(ids=[CHUNK_ID])
        second_reply = await self.diagnose(task_id=task_id)
        self.assertEqual(self.events[-1]["task_id"], task_id)
        self.assertEqual(second_reply["citations"], [])
        self.assertIn("未找到指定规则的精确版本证据", second_reply["content"])
        self.assertNotIn(URL, second_reply["content"])


class ReviewRetrievalTests(unittest.TestCase):
    def test_reopen_rebuilds_keyword_index_and_fuses_by_chunk_id(self):
        with TemporaryDirectory() as path:
            with closing(rag_knowledge.RAGKnowledge(db_path=path, embedding_fn=FixedReviewEmbedding())) as rag:
                seed_review_rules(rag)
                seed_review_rules(rag)
                rag.add_text("投放工作日旧指南", {"source": "legacy"}, doc_id="legacy")
                self.assertEqual(rag.collection.count(), len(DEMO_RULES) + 1)
            with closing(rag_knowledge.RAGKnowledge(
                db_path=Path(path), embedding_fn=FixedReviewEmbedding(), create=False,
            )) as rag:
                query = f"请查阅 {REFERENCE}"
                vector_ids = [hit["chunk_id"] for hit in rag.search(query, top_k=rag.collection.count())]
                self.assertNotEqual(vector_ids[0], CHUNK_ID)
                self.assertIn(CHUNK_ID, vector_ids)
                evidence = rag.generate(query, top_k=len(DEMO_RULES) + 1)["evidence"]
                self.assertEqual(evidence[0]["chunk_id"], CHUNK_ID)
                self.assertEqual(sum(hit["chunk_id"] == CHUNK_ID for hit in evidence), 1)
                self.assertIn({"chunk_id": "legacy", "source": "legacy", "content": "投放工作日旧指南"}, evidence)


if __name__ == "__main__":
    unittest.main()
