"""Fixed-model SSE tests for request-local rule receipt reuse."""

from threading import Barrier
from unittest.mock import patch

from backend.agent.tests import test_home_delegation as fixture
from backend.agent.tests.test_home_sse_tracing import tool_call
from backend.rag.review_rule_demo import DEMO_RULES
from backend.tools import tools


SEARCH = "search_knowledge"
V1 = "ctr-review-v1#low-ctr"
V2 = "ctr-review-v2#low-ctr"
ALIAS = "点击率下降复核 v1"
OTHER = "比较窗口口径 v1"
HITS = [dict(chunk_id=chunk, rule_id=rule_id, version=version, title=title, content=content)
        for chunk, rule_id, version, title, content in DEMO_RULES]


def read(call_id, query):
    return tool_call(call_id, name=SEARCH, query=query)


def receipt(*hits):
    return {"context": "\n".join(hit["content"] for hit in hits), "sources": [], "evidence": list(hits)}


class Task26R5RuleEvidenceTests(fixture.HomeDelegationFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.actual = []
        self.evidence["actual_knowledge_calls"] = self.actual
        self.responses = {V1: receipt(HITS[0]), V2: receipt(HITS[1])}

        def lookup(query):
            self.actual.append(query)
            return self.responses[query]

        self.enterContext(patch.object(tools, SEARCH, side_effect=lookup))

    def verify_counts(self, steps, actual, reused):
        searches = [s for s in steps if s["stage"] == "tool_result" and s["action"] == SEARCH
                    and s.get("evidence", {}).get("retrieved")]
        reuses = [s for s in steps if s["stage"] == "tool_reuse" and s["action"] == SEARCH]
        self.assertEqual((len(searches), len(reuses)), (actual, reused))
        return reuses

    async def test_main_and_worker_reuse_each_others_artifacts(self):
        def worker(_, receipts):
            return fixture.worker_summary("已核对") if receipts else fixture.reply(
                "", read("worker-v1", ALIAS), read("worker-v2", V2))

        def main(receipts):
            if not receipts:
                return fixture.reply("", read("main-v1", V1))
            if "batch" not in receipts:
                return fixture.reply("", fixture.dispatch("batch", fixture.task("rule", tools=(SEARCH,))))
            if "main-v2" not in receipts:
                return fixture.reply("", read("main-v2", "ctr-review#low-ctr v2"))
            if "by-id" not in receipts:
                return fixture.reply("", read("by-id", "artifact:" + receipts["main-v2"]["artifact_id"]))
            return fixture.reply("两版规则已核对")

        self.worker = worker
        self.select(main)
        await self.request()
        steps = self.complete("两版规则已核对")
        self.assertEqual(self.actual, [V1, V2])
        reuses = self.verify_counts(steps, 2, 3)
        first = self.main_receipts["main-v1"]
        second = self.main_receipts["main-v2"]
        self.assertTrue(second["reused"])
        self.assertEqual(self.main_receipts["by-id"], second)
        self.assertNotEqual(first["artifact_id"], second["artifact_id"])
        self.assertEqual([s["evidence"]["artifact_id"] for s in reuses],
                         [first["artifact_id"], second["artifact_id"], second["artifact_id"]])
        first_worker, second_worker = self.main_receipts["batch"]["evidence"]
        for original, adopted in zip((first, second), (first_worker, second_worker)):
            self.assertEqual(adopted["artifact_id"], original["artifact_id"])
            self.assertEqual(adopted["runtime_call_id"], original["runtime_call_id"])
            self.assertEqual(adopted["result"], original["result"])

    async def test_parallel_workers_with_same_rule_run_one_lookup(self):
        ready = Barrier(2, timeout=5)

        def worker(inputs, receipts):
            if receipts:
                return fixture.worker_summary("找到条文")
            name = inputs["task"]["task_id"]
            ready.wait()
            return fixture.reply("", read(name, V1 if name == "left" else ALIAS))

        self.worker = worker
        self.select(lambda receipts: fixture.reply("两个回执已核对") if receipts else fixture.reply(
            "", fixture.dispatch("batch", fixture.task("left", tools=(SEARCH,)), fixture.task("right", tools=(SEARCH,)))))
        await self.request()
        steps = self.complete("两个回执已核对")
        self.assertEqual(self.actual, [V1])
        self.verify_counts(steps, 1, 1)
        left, right = self.main_receipts["batch"]["evidence"]
        self.assertEqual(left["artifact_id"], right["artifact_id"])
        self.assertEqual(left["runtime_call_id"], right["runtime_call_id"])
        self.assertEqual(left["result"], right["result"])
        self.assertEqual(self.main_receipts["batch"]["states"], {"left": "succeeded", "right": "succeeded"})

    async def test_free_text_mixed_hits_keep_new_evidence_and_different_targets(self):
        first = "CTR 怎么判定"
        mixed = "比较窗口和待复核条件有哪些"
        self.responses.update({first: receipt(HITS[0]), mixed: receipt(HITS[0], HITS[3])})
        self.select(lambda receipts: fixture.reply("已核对阈值和窗口") if receipts else fixture.reply(
            "", read("first", first), read("mixed", mixed), read("explicit", OTHER)))
        await self.request()
        steps = self.complete("已核对阈值和窗口")
        self.assertEqual(self.actual, [first, mixed])
        self.verify_counts(steps, 2, 1)
        received = self.main_receipts["mixed"]
        self.assertEqual([hit["chunk_id"] for hit in received["result"]["evidence"]],
                         [HITS[0]["chunk_id"], HITS[3]["chunk_id"]])
        self.assertEqual(self.main_receipts["explicit"]["artifact_id"], received["artifact_id"])
        self.assertEqual([s["evidence"]["new_chunk_ids"] for s in steps if s["stage"] == "knowledge_evidence"],
                         [[HITS[0]["chunk_id"]], [HITS[3]["chunk_id"]]])

    async def test_cross_request_artifact_is_unavailable_and_rule_reads_again(self):
        self.select(lambda receipts: fixture.reply("首轮完成") if receipts else fixture.reply("", read("one", V1)))
        await self.request()
        original = self.main_receipts["one"]["artifact_id"]
        self.events = []
        self.select(lambda receipts: fixture.reply("本轮规则已读取") if receipts else fixture.reply(
            "", read("stale", "artifact:" + original), read("two", ALIAS)))
        await self.request(message="再读本轮规则")
        self.assertEqual(self.events[-1]["type"], "done")
        self.assertEqual(self.actual, [V1, V1])
        self.assertEqual(self.main_receipts["stale"]["error_code"], "knowledge_artifact_not_found")
        self.assertNotEqual(self.main_receipts["two"]["artifact_id"], original)

    async def test_missing_rule_lookup_does_not_mask_a_distinct_section(self):
        self.responses[V1] = receipt()
        distinct = "ctr-review-v1#low-volume"
        self.responses[distinct] = receipt(HITS[2])

        def main(receipts):
            if not receipts:
                return fixture.reply("", read("empty", V1), read("duplicate", ALIAS))
            if "found" not in receipts:
                return fixture.reply("", read("found", distinct))
            return fixture.reply("补查获得条文")

        self.select(main)
        await self.request()
        steps = self.complete("补查获得条文")
        self.assertEqual(self.actual, [V1, distinct])
        self.verify_counts(steps, 2, 0)
        self.assertEqual(self.main_receipts["empty"]["error_code"], "knowledge_missing")
        self.assertEqual(self.main_receipts["duplicate"]["error_code"], "duplicate_read")
        self.assertTrue(self.main_receipts["found"]["success"])
