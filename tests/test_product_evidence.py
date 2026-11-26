"""End-to-end product evidence regressions on a disposable catalog."""

import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import observability
from backend.agent import product_evidence_reply
from backend.database import database
from backend.rag import product_candidate_admission, product_chat_search
from backend.rag import product_evidence as rag
from backend.rag.product_rerank_queue import RerankQueue
from backend.rag.product_query_expansion import alias_query, catalog_queries, merge_channels, part_queries
from scripts.eval_product_rerank import reorder
from scripts.product_evidence import FIXTURES, PASSWORD, setup_fixture

TARGET = {"shop_id": "evidence-main", "marketplace_id": "US", "sku": "METRO-500"}
QUERY = "接触饮料的内胆是什么材质？"
CHAT = "商品资料：evidence-main US METRO-500 " + QUERY


class FixedEmbedding:
    def __init__(self, *, device=None):
        pass

    def __call__(self, input):
        return [[1, 0, 0] if "stainless steel" in text else
                [0, 1, 0] if "Service code LID-M" in text else [0, 0, 1] for text in input]

    def embed_query(self, input):
        return [[0, 1, 0] if "LID-M" in text else [1, 0, 0] for text in input]


class QueryExpansionTests(unittest.TestCase):
    def test_candidate_selection_requires_exclusive_spec_condition_in_the_request(self):
        quote = "Maximum temperature\t72\t°C\tLaboratory test only\tExample product"
        source = {"location": "row:2", "quote": quote}
        proposal = [{"id": 1, "quote": "Maximum temperature"}]
        self.assertEqual(product_candidate_admission._verified_sources(proposal, {1: source}, "What is the operating limit?"), [])
        self.assertEqual(product_candidate_admission._verified_sources(
            proposal, {1: source}, "What is the limit in the laboratory test?"), [source])

    def test_candidate_selection_does_not_confuse_upper_and_lower_spec_limits(self):
        source = {"location": "row:11", "quote": "Maximum input voltage\t72\tV\tSteady input\tExample product"}
        proposal = [{"id": 2, "quote": "Maximum input voltage"}]
        self.assertEqual(product_candidate_admission._verified_sources(proposal, {2: source}, "最低输入电压是多少？"), [])
        self.assertEqual(product_candidate_admission._verified_sources(proposal, {2: source}, "What is the upper limit?"), [source])

    def test_chat_request_keeps_question_after_scope_line(self):
        english = "What is the beam angle at half peak intensity?"
        for separator in ("\n", "  \r\n"):
            self.assertEqual(product_evidence_reply.parse_request(f"商品资料：shop US{separator}{english}"),
                             ("shop", "US", None, english))
        self.assertEqual(product_evidence_reply.parse_request(f"商品资料: shop US unknown\n{english}"),
                         ("shop", "US", "unknown", english))
        self.assertEqual(product_evidence_reply.parse_request("商品资料：shop US"), ("shop", "US", None, ""))
        self.assertIsNone(product_evidence_reply.parse_request("商品资料：shop US unknown"))

    def test_catalog_queries_do_not_inject_part_identifiers_or_expand_unrelated_questions(self):
        replacement = catalog_queries("备用杯盖看起来相同，可以互换吗？")
        self.assertEqual(set(replacement), {"cap_compatibility", "cap_service_part"})
        self.assertIn("service part number", replacement["cap_service_part"])
        for term in ("LID-M", "B42", "METRO"):
            self.assertNotIn(term, " ".join(replacement.values()))
        for query in ("Are spare gaskets interchangeable?", "这台微波炉的功率是多少？", "瓶身多宽？"):
            self.assertEqual(catalog_queries(query), {})

    def test_preserves_conditions_units_and_identifiers(self):
        query = "METRO-750 装着 LID-M500，不装饮料时的空杯重量是多少 g？"
        self.assertEqual(alias_query(query), query + " empty weight")
        query = "METRO-750 的杯身与杯盖能用 55 °C 机洗吗？不可使用漂白剂，LID-M500 要手洗。"
        self.assertEqual(part_queries(query, english_terms=True), [
            query.replace("杯身与杯盖", "杯身") + " bottle body dishwasher wash",
            query.replace("杯身与杯盖", "杯盖") + " cap dishwasher wash",
        ])
        self.assertEqual(part_queries("杯身、杯盖及密封圈能机洗吗？", english_terms=True), [])

    def test_merge_retains_original_prefix_and_unique_candidates(self):
        orders = [["a", "b", "c", "d"], ["a", "e", "f"], ["e", "g", "b"]]
        self.assertEqual(merge_channels(orders, 2), ["a", "b", "e", "g", "c", "f", "d"])

    def test_rerank_keeps_ties_in_recall_order_and_rejects_duplicate_ids(self):
        hits = [{"chunk_id": str(i)} for i in range(rag.RECALL_K)]
        scores = [0.0] * len(hits)
        scores[12] = scores[17] = 1.0
        self.assertEqual(reorder(hits, scores), [hits[12], hits[17], *hits[:12], *hits[13:17], *hits[18:]])
        with self.assertRaises(AssertionError):
            reorder([*hits[:-1], hits[0]], scores)


class ProductEvidenceTests(unittest.TestCase):
    def setUp(self):
        import fast_server

        root = Path("logs").resolve()
        root.mkdir(exist_ok=True)
        path = Path(self.enterContext(TemporaryDirectory(dir=root)))
        self.enterContext(patch.object(database.settings, "DIAGNOSIS_SANDBOX_DB_PATH", str(path / "catalog.sqlite3")))
        self.enterContext(patch.object(database.settings, "DIAGNOSIS_MYSQL_ENABLED", False))
        self.enterContext(patch.object(database.settings, "JWT_SECRET", "product-evidence-regression-test-key"))
        self.enterContext(patch.object(observability, "TRACE_LOG_FILE", path / "traces.jsonl"))
        self.memory = self.enterContext(patch("backend.memory.conversation_memory.store_conversation"))
        self.summary = self.enterContext(patch("backend.memory.memory_manager.store_conversation_summary"))
        self.enterContext(patch.object(rag, "BGEEmbedding", FixedEmbedding))
        database.init_db()
        self.pack = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        with closing(database.get_db()) as conn:
            self.users = setup_fixture(conn, self.pack)
            for release in self.pack["releases"]:
                rag.import_release(conn, {**release, "evidence_rules": self.pack["evidence_rules"]}, FIXTURES / "sources")
            self.activate(conn, "v1")
            for key in (2, 3, 4):
                target = self.pack["releases"][key]["target"]
                rag.activate(conn, target, "v1")
        self.client = self.enterContext(TestClient(fast_server.app))
        self.owner = self.login("owner")
        self.other = self.login("other")

    def login(self, principal):
        response = self.client.post("/api/login", json={"username": f"product-evidence-{principal}", "password": PASSWORD})
        return {"Authorization": "Bearer " + response.json()["data"]["token"]}

    def activate(self, conn, version):
        rag.activate(conn, TARGET, version)

    def search(self, headers=None, **overrides):
        return self.client.post("/api/product-evidence/search", headers=headers,
                                json={**TARGET, "query": QUERY, **overrides})

    def original(self, chunk_id, headers=None):
        return self.client.get(f"/api/product-evidence/chunks/{chunk_id}", headers=headers)

    def test_auth_scope_and_version_switch(self):
        self.assertEqual(self.search().status_code, 401)
        self.assertEqual(self.search(self.other).status_code, 404)
        self.assertEqual(self.search(self.owner, version="v2").status_code, 422)
        for mode in ("hybrid", "vector"):
            result = self.search(self.owner, mode=mode).json()
            self.assertEqual(result["version"], "v1")
            self.assertIn("304 stainless steel", result["evidence"][0]["quote"])
            for candidate in result["candidates"]:
                self.assertEqual(candidate["sku"], TARGET["sku"])
                self.assertEqual(candidate["marketplace_id"], TARGET["marketplace_id"])
                self.assertEqual(candidate["shop_id"], TARGET["shop_id"])
        chunk_id = result["evidence"][0]["chunk_id"]
        self.assertEqual(self.original(chunk_id).status_code, 401)
        self.assertEqual(self.original(chunk_id, self.other).status_code, 404)
        source = self.original(chunk_id, self.owner).json()
        self.assertEqual(source["original"][source["start_offset"]:source["end_offset"]], source["content"])
        with closing(database.get_db()) as conn:
            self.activate(conn, "v2")
        self.assertEqual(self.original(chunk_id, self.owner).status_code, 404)
        self.assertIn("316 stainless steel", self.search(self.owner).json()["evidence"][0]["quote"])

    def test_unknown_and_partial_coverage_abstain(self):
        unknown = self.search(self.owner, query="能保温几小时？").json()
        self.assertEqual((unknown["status"], unknown["evidence"]), ("insufficient_evidence", []))
        self.assertTrue(unknown["candidates"])
        partial = self.search(self.owner, query="杯身和杯盖都能进洗碗机吗？", top_k=1).json()
        self.assertEqual((partial["reason"], partial["evidence"]), ("incomplete_source_coverage", []))

    def test_incompatible_active_index_returns_service_unavailable(self):
        with closing(database.get_db()) as conn, conn:
            conn.execute("""UPDATE product_document_releases SET embedding_model=?
                            WHERE shop_id=? AND marketplace_id=? AND sku=? AND active=1""",
                         ("different-embedding-revision", *TARGET.values()))
        response = self.search(self.owner, mode="vector", expansion="both")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"], "商品资料索引暂不可用")

    def test_expansion_endpoint_uses_same_scope_and_original_evidence_contract(self):
        query = "杯身和杯盖都能进洗碗机吗？"
        self.assertEqual(self.search(self.owner, query=query, expansion="both").status_code, 422)
        with patch.object(FixedEmbedding, "embed_query", autospec=True, side_effect=FixedEmbedding.embed_query) as encode:
            self.assertEqual(self.search(self.other, query=query, mode="vector", expansion="both").status_code, 404)
            encode.assert_not_called()
            response = self.search(self.owner, query=query, mode="vector", expansion="both", top_k=1)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["evidence"], [])
            self.assertEqual(encode.call_count, 3)
            self.assertEqual(encode.call_args_list[0].args[1], [query])
        wrong_part = self.search(self.owner, mode="vector", expansion="both", query="杯盖单独的高度是多少 mm？").json()
        self.assertEqual((wrong_part["reason"], wrong_part["evidence"]), ("unsupported_question", []))

    def test_named_part_code_requires_matching_source(self):
        result = self.search(self.owner, query="Does LID-M750 fit this SKU?", mode="hybrid").json()
        self.assertEqual((result["status"], result["evidence"]), ("insufficient_evidence", []))
        self.assertEqual(result["reason"], "part_code_unverified")

    def test_concurrent_idempotency_conflict_and_failed_activation(self):
        release = {**self.pack["releases"][0], "version": "concurrent",
                   "evidence_rules": self.pack["evidence_rules"]}

        def run():
            with closing(database.get_db()) as conn:
                return rag.import_release(conn, release, FIXTURES / "sources")

        with ThreadPoolExecutor(2) as executor:
            self.assertEqual(sorted(executor.map(lambda _: run(), range(2))), [False, True])
        with closing(database.get_db()) as conn:
            with self.assertRaises(rag.ReleaseConflict):
                rag.import_release(conn, {**release, "evidence_rules": []}, FIXTURES / "sources")
            with self.assertRaises(LookupError):
                self.activate(conn, "missing")
            self.assertEqual(conn.execute("SELECT count(*) FROM product_documents d JOIN product_document_releases r "
                                          "ON r.id=d.release_id WHERE r.version='concurrent'").fetchone()[0], 3)
        self.assertEqual(self.search(self.owner).json()["version"], "v1")

    def test_failed_import_is_atomic(self):
        release = {**self.pack["releases"][0], "version": "fault"}
        with closing(database.get_db()) as conn:
            conn.execute("""CREATE TRIGGER fault_chunk BEFORE INSERT ON product_document_chunks
                            BEGIN SELECT RAISE(ABORT, 'injected chunk write failure'); END""")
            import sqlite3

            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected chunk write failure"):
                rag.import_release(conn, release, FIXTURES / "sources")
            self.assertEqual(conn.execute("SELECT count(*) FROM product_document_releases WHERE version='fault'").fetchone()[0], 0)

    def test_fact_config_upgrade_preserves_existing_chunks_and_rejects_edits(self):
        from backend.database import product_documents

        fact = {
            "key": "liner_material", "component": "liner", "conditions": "food contact",
            "questions": ["内胆材料"], "missing": "内胆材料",
            "source": None,
        }
        release = {**self.pack["releases"][0], "evidence_rules": self.pack["evidence_rules"]}
        configured = {**release, "fact_rules": [fact]}
        with closing(database.get_db()) as conn:
            def chunk_ids():
                return [row["chunk_id"] for row in product_documents.scoped_chunks(
                    conn, self.users["owner"], rag._scope(TARGET), "structured")]

            before = chunk_ids()
            self.assertTrue(before)
            self.assertTrue(rag.import_release(conn, configured, FIXTURES / "sources"))
            self.assertEqual(chunk_ids(), before)
            self.assertFalse(rag.import_release(conn, release, FIXTURES / "sources"))
            self.assertFalse(rag.import_release(conn, configured, FIXTURES / "sources"))
            with self.assertRaises(rag.ReleaseConflict):
                rag.import_release(conn, {**configured, "fact_rules": [{**fact, "missing": "其他"}]},
                                   FIXTURES / "sources")
            result = rag.ProductEvidence().search(conn, self.users["owner"], TARGET, "内胆材料")
            self.assertEqual(result["missing_sections"], ["内胆材料"])
            self.assertEqual(result["fact_checks"][0]["reason"], "source_not_configured")
            self.assertEqual(result["evidence"], [])

            corrected = {**configured, "fact_rules": [{**fact, "missing": "内胆材料说明"}]}
            self.assertTrue(rag.import_release(conn, corrected, FIXTURES / "sources", previous_facts=[fact]))
            self.assertEqual(chunk_ids(), before)
            self.assertFalse(rag.import_release(conn, corrected, FIXTURES / "sources", previous_facts=[fact]))
            with self.assertRaises(rag.ReleaseConflict):
                rag.import_release(conn, configured, FIXTURES / "sources", previous_facts=[fact])
            result = rag.ProductEvidence().search(conn, self.users["owner"], TARGET, "内胆材料")
            self.assertEqual(result["fact_checks"][0]["missing"], "内胆材料说明")

    def test_fact_import_rejects_foreign_regions_and_wrong_source_conditions(self):
        release = {**self.pack["releases"][0], "version": "invalid-fact"}
        fact = {
            "key": "liner_material", "component": "liner", "conditions": "food contact",
            "questions": ["内胆材料"], "missing": "内胆材料",
        }
        source = {"document_id": "specs", "location": "row:2", "component_pattern": r".+",
                  "value_pattern": r".+", "condition_pattern": "submerged during charging"}
        with closing(database.get_db()) as conn:
            for location, reason in (("row:999", "outside the release"), ("row:2", "condition_unverified")):
                with self.subTest(location=location), self.assertRaisesRegex(ValueError, reason):
                    rag.import_release(conn, {**release, "fact_rules": [{**fact, "source": {
                        **source, "location": location,
                    }}]}, FIXTURES / "sources")
            self.assertFalse(conn.execute(
                "SELECT 1 FROM product_document_releases WHERE version=?", (release["version"],),
            ).fetchone())

    def test_chat_keeps_document_quotes_out_of_generic_history_and_memory(self):
        response = self.client.post("/api/chat/stream", headers=self.owner, json={"message": CHAT})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1]["type"], "done")
        self.assertIn("/api/product-evidence/chunks/", next(event["content"] for event in events if event["type"] == "text"))
        with closing(database.get_db()) as conn:
            self.activate(conn, "v2")
            old_quotes = [row["content"] for row in conn.execute("SELECT content FROM conversations WHERE role='assistant'")
                          if "304 stainless steel" in row["content"]]
        self.assertEqual(old_quotes, [], "obsolete document text persisted into generic chat history")
        self.memory.assert_not_called()
        self.summary.assert_not_called()

    def test_owner_revocation_applies_to_search_and_readback(self):
        chunk_id = self.search(self.owner).json()["evidence"][0]["chunk_id"]
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                         (self.users["other"], TARGET["shop_id"], TARGET["marketplace_id"]))
        self.assertEqual(self.search(self.owner).status_code, 404)
        self.assertEqual(self.original(chunk_id, self.owner).status_code, 404)
        self.assertEqual(self.original(chunk_id, self.other).status_code, 200)

    def chat_trace(self, message=CHAT, headers=None):
        response = self.client.post("/api/chat/stream", headers=headers or self.owner, json={"message": message})
        events = [json.loads(line[6:]) for line in response.raise_for_status().text.splitlines()
                  if line.startswith("data: ")]
        self.assertEqual(events[-1]["type"], "done", events)
        trace_id = events[-1]["trace_id"]
        steps = observability.get_trace(trace_id)["steps"]
        step, = [s["evidence"] for s in steps
                 if s["action"] == "chat_retrieval"]
        commit, = [s["evidence"] for s in steps if s["action"] == "save_reply"]
        self.assertEqual(commit["final_citation_ids"], step.get("citation_ids", []))
        answer, = [event["content"] for event in events if event["type"] == "text"]
        return answer, step, trace_id

    def use_unconfigured_release(self):
        release = {**self.pack["releases"][0], "version": "unconfigured", "evidence_rules": []}
        with closing(database.get_db()) as conn:
            rag.import_release(conn, release, FIXTURES / "sources")
            self.activate(conn, release["version"])
        self.enterContext(patch.object(product_chat_search.product_candidate_admission, "enabled", return_value=True))

    def test_candidate_selection_preserves_verified_part_and_rejects_invalid_citation(self):
        self.use_unconfigured_release()
        missing_question = "能用微波炉加热内胆吗？"

        def select(*args, **payload):
            submitted = json.loads(payload["messages"][-1]["content"])
            self.assertEqual(submitted["question"], QUERY + missing_question)
            if payload["step"] == "product_candidate_verification":
                self.assertEqual([item["id"] for item in submitted["requests"]], [1])
                reply = {"verified": [1]}
            else:
                source = next(item for item in submitted["sources"] if "The inner liner that touches the drink" in item["text"])
                reply = {"parts": [
                    {"clauses": [1], "status": "supported", "sources": [{"id": source["id"], "quote": "304 stainless steel"}]},
                    {"clauses": [2], "status": "supported", "sources": [{"id": source["id"], "quote": "safe in any microwave"}]},
                ]}
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                                                           message=SimpleNamespace(content=json.dumps(reply)))])

        with patch.object(product_chat_search.product_candidate_admission, "create_completion", side_effect=select) as call:
            answer, trace, _ = self.chat_trace(CHAT + missing_question)
            self.assertEqual(call.call_count, 2)
        self.assertEqual(trace["status"], "partial_support")
        self.assertEqual([check["status"] for check in trace["fact_checks"]], ["verified", "missing"])
        self.assertEqual(trace["fact_checks"][1]["reason"], "source_unverified")
        self.assertLess(answer.index("304 stainless steel"), answer.index("证据不足"))
        self.assertIn(missing_question.rstrip("？"), answer)
        self.assertNotIn("safe in any microwave", answer)
        cite, = trace["citation_ids"]
        self.assertEqual(self.original(cite, self.owner).status_code, 200)
        self.assertEqual(self.original(cite, self.other).status_code, 404)
        history = self.client.get("/api/chat/history", headers=self.owner).json()["data"]
        self.assertIn(cite, history[-1]["content"])
        self.assertNotIn("304 stainless steel", history[-1]["content"])

    def test_candidate_selection_rechecks_scope_after_provider_returns(self):
        self.use_unconfigured_release()

        def switch_version(*args, **payload):
            submitted = json.loads(payload["messages"][-1]["content"])
            source = next(item for item in submitted["sources"] if "The inner liner that touches the drink" in item["text"])
            with closing(database.get_db()) as conn:
                self.activate(conn, "v2")
            reply = {"parts": [{"clauses": [1], "status": "supported",
                                "sources": [{"id": source["id"], "quote": "304 stainless steel"}]}]}
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                                                           message=SimpleNamespace(content=json.dumps(reply)))])

        with patch.object(product_chat_search.product_candidate_admission, "create_completion", side_effect=switch_version) as call:
            answer, trace, _ = self.chat_trace()
            call.assert_called_once()
        self.assertEqual(answer, "当前账号无法访问该范围的商品资料，请核对店铺、站点和 SKU。")
        self.assertEqual(trace["status"], "denied")
        self.assertFalse(trace.get("citation_ids"))

    def use_rerank(self, factory, *, queue_size=1, wait=200, inference=1000):
        self.enterContext(patch.object(database.settings, "PRODUCT_RERANK_ENABLED", True))
        self.enterContext(patch.object(database.settings, "PRODUCT_RERANK_USER_IDS",
                                       frozenset(self.users.values())))
        self.enterContext(patch.object(observability, "PRODUCT_RERANK_LATE_FILE",
                                       observability.TRACE_LOG_FILE.with_name("late.jsonl")))
        queue = RerankQueue(1, queue_size, wait, inference, factory=factory)
        self.enterContext(patch.object(product_chat_search, "_queue", queue))
        return queue

    def test_r4_cpu_admits_evidence_beyond_top_five_without_accessing_gpu(self):
        retrieve = rag.ProductEvidence.retrieve
        selected = []

        def ordered(service, conn, user_id, target, query, **options):
            self.assertEqual(options, {"mode": "vector", "expansion": "both", "attribute_expansion": "both"})
            result, rows = retrieve(service, conn, user_id, target, query, **options)
            valid = [hit for hit in map(rag._hit, rows) if hit["document_id"] == "manual"
                     and any(loc["location"] == "page:4" and loc["complete"] for loc in hit["locations"])]
            cite = next(hit for hit in valid if "304 stainless steel" in hit["content"])
            valid_ids = {hit["chunk_id"] for hit in valid}
            hits = [hit for hit in result["candidates"] if hit["chunk_id"] not in valid_ids]
            self.assertGreaterEqual(len(hits), 5)
            hits.insert(5, cite)
            selected.append(cite["chunk_id"])
            return {**result, "candidates": hits[:rag.RECALL_K]}, rows

        with patch.object(database.settings, "PRODUCT_R4_CPU_ENABLED", True), \
                patch.object(database.settings, "PRODUCT_RERANK_ENABLED", False), \
                patch.object(rag.ProductEvidence, "retrieve", ordered), \
                patch.object(product_chat_search, "get_queue") as queue:
            reply, trace, _ = self.chat_trace()
            self.assertEqual(trace["path"], "r4_cpu")
            self.assertEqual(trace["citation_ids"], selected)
            self.assertEqual(trace["candidate_ids"][5], selected[0])
            self.assertIn("304 stainless steel", reply)
            self.assertEqual(self.original(selected[0], self.owner).status_code, 200)
            queue.assert_not_called()

    def test_fixed_r2_order_and_q29_missing_candidate(self):
        self.enterContext(patch.object(database.settings, "PRODUCT_R4_CPU_ENABLED", False))
        frozen = json.loads((FIXTURES / "rerank/frozen-candidates.json").read_bytes())
        replay = {q["id"]: q for q in json.loads((FIXTURES / "rerank/gpu/questions.json").read_bytes())}
        fixed = {q["query"]: q for q in frozen["questions"] if q["id"] in {"Q13", "Q14", "Q29"}}
        omit = set()
        current_ids = {}
        suppress_default = False
        retrieve = rag.ProductEvidence.retrieve

        def fixed_retrieve(service, conn, user_id, target, query, **options):
            result, rows = retrieve(service, conn, user_id, target, query, **options)
            if options.get("expansion") == "both":
                by_id = {row["chunk_id"]: rag._hit(row) for row in rows}
                keys = [key for key in fixed[query]["candidate_chunk_ids"] if key not in omit]
                keys.extend(key for key, hit in by_id.items() if key not in keys and not any(
                    loc["location"] == "page:13" and loc["complete"] for loc in hit["locations"]
                ))
                current_ids[query] = keys[:rag.RECALL_K]
                result["candidates"] = [by_id[key] for key in current_ids[query]]
            elif query in current_ids and omit:
                # The archived hybrid baseline finds both sources for Q29.
                by_id = {row["chunk_id"]: rag._hit(row) for row in rows}
                result["candidates"] = [by_id[key] for key in fixed[query]["candidate_chunk_ids"]
                                        if not suppress_default or key not in omit]
            return result, rows

        class FixedR2:
            def score(self, query, passages):
                q = fixed[query]
                order = replay[q["id"]]["rerank"]["order"]
                keys = current_ids[query]
                if not omit and passages != [frozen["chunks"][key]["content"] for key in keys]:
                    raise AssertionError("Frozen candidate text changed")
                return [-order.index(key) if key in order else -len(order) for key in keys]

        self.use_rerank(FixedR2)
        with patch.object(rag.ProductEvidence, "retrieve", fixed_retrieve):
            for query, q in fixed.items():
                with self.subTest(q=q["id"]):
                    message = "商品资料：" + " ".join((*q["target"].values(), query))
                    answer, step, _ = self.chat_trace(message)
                    self.assertEqual(step["scored_chunk_ids"], step["candidate_ids"])
                    self.assertEqual(step["ranked_ids"], replay[q["id"]]["rerank"]["order"])
                    self.assertEqual(len(step["citation_ids"]), 2)
                    self.assertEqual(step["path"], "rerank_fill" if q["id"] == "Q29" else "rerank_top5")
                    for key in step["citation_ids"]:
                        self.assertIn(key, answer)
                        self.assertEqual(self.original(key, self.owner).status_code, 200)
                    if q["id"] == "Q29":
                        fill, = step["fill"]
                        self.assertEqual((fill["document_id"], fill["location"], fill["rank"]), ("manual", "page:13", 12))
                        omit.update(key for key in step["candidate_ids"] if any(
                            loc["location"] == "page:13" and loc["complete"] for loc in frozen["chunks"][key]["locations"]
                        ))
                        answer, step, _ = self.chat_trace(message)
                        self.assertEqual(step["path"], "default")
                        self.assertEqual(step["fallback_reason"], "incomplete_source_coverage")
                        self.assertEqual(len(step["rerank_retrieval"]["candidate_ids"]), 20)
                        self.assertEqual(step["default_retrieval"]["mode"], "hybrid")
                        self.assertIn("chunk_id", fill)
                        self.assertIn(fill["chunk_id"], step["default_retrieval"]["candidate_ids"])
                        self.assertIn("page:13", answer)
                        suppress_default = True
                        answer, step, _ = self.chat_trace(message)
                        self.assertEqual(step["fallback_reason"], "incomplete_source_coverage")
                        self.assertEqual(step["citation_ids"], [])
                        self.assertIn("Refitting the seal", answer)
                        print("Q29 counterexample:", json.dumps(step, ensure_ascii=False))

    def test_gpu_faults_fall_back_and_late_job_retains_slot(self):
        self.enterContext(patch.object(database.settings, "PRODUCT_R4_CPU_ENABLED", True))
        baseline, _, _ = self.chat_trace()
        started, release = Event(), Event()
        scored = []

        class BlockedGPU:
            def score(self, query, passages):
                scored.append(query)
                started.set()
                if not release.wait(5):
                    raise RuntimeError("barrier timed out")
                return [0] * len(passages)

        queue = self.use_rerank(BlockedGPU, wait=450, inference=1050)
        with ThreadPoolExecutor(2) as executor:
            first = executor.submit(self.chat_trace)
            try:
                self.assertTrue(started.wait(3))
                waiting = executor.submit(self.chat_trace)
                self.await_pending(queue)
                self.assert_fault(self.chat_trace(), baseline, "queue_full")
                self.assert_fault(waiting.result(3), baseline, "queue_timeout")
                trace_id = self.assert_fault(first.result(3), baseline, "inference_timeout")
                self.assertEqual(queue.running, 1)
                self.assertEqual(len(scored), 1)
            finally:
                release.set()
        deadline = monotonic() + 2
        late = observability.PRODUCT_RERANK_LATE_FILE
        while not late.exists() and monotonic() < deadline:
            sleep(.01)
        late_record, = map(json.loads, late.read_text(encoding="utf-8").splitlines())
        self.assertEqual(late_record["trace_id"], trace_id)
        self.assertGreater(late_record["inference_ms"], 0)
        with closing(database.get_db()) as conn:
            receipts = [row["content"] for row in conn.execute("SELECT content FROM conversations WHERE role='assistant'")]
            self.assertEqual(len(receipts), 4)
            self.assertEqual(len(set(receipts)), 1)
            self.assertNotIn("304 stainless steel", receipts[0])
        self.memory.assert_not_called()
        self.summary.assert_not_called()

    def test_disconnected_chat_discards_result_and_keeps_gpu_slot(self):
        import fast_server

        started, release = Event(), Event()

        class BlockedGPU:
            def score(self, query, passages):
                started.set()
                if not release.wait(5):
                    raise RuntimeError("barrier timed out")
                return [0] * len(passages)

        queue = self.use_rerank(BlockedGPU, inference=5000)
        events = []
        incoming = [{"type": "http.request", "body": json.dumps({"message": CHAT}).encode()}]

        async def receive():
            if incoming:
                return incoming.pop()
            while not started.is_set():
                await asyncio.sleep(.005)
            return {"type": "http.disconnect"}

        async def send(message):
            if message.get("body", b"").startswith(b"data: "):
                events.append(json.loads(message["body"][6:]))

        async def disconnect():
            await asyncio.wait_for(fast_server.app({
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "method": "POST", "scheme": "http", "path": "/api/chat/stream", "query_string": b"",
                "headers": [(b"authorization", self.owner["Authorization"].encode())],
            }, receive, send), timeout=3)

        try:
            asyncio.run(disconnect())
            self.assertEqual(queue.running, 1)
            self.assertEqual([event["type"] for event in events], ["thinking"])
        finally:
            release.set()
        late = observability.PRODUCT_RERANK_LATE_FILE
        deadline = monotonic() + 2
        while not late.exists() and monotonic() < deadline:
            sleep(.01)
        late_record, = map(json.loads, late.read_text(encoding="utf-8").splitlines())
        self.assertEqual(late_record["reason"], "cancelled")
        self.assertEqual(late_record["trace_id"], events[0]["trace_id"])
        self.assertEqual(observability.get_trace(late_record["trace_id"])["status"], "interrupted")
        with closing(database.get_db()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM conversations WHERE role='assistant'").fetchone()[0], 0)
        self.memory.assert_not_called()
        self.summary.assert_not_called()
        print("Cancelled GPU:", json.dumps(late_record))

    def test_gpu_unavailable_then_flag_off(self):
        baseline, _, _ = self.chat_trace()

        def unavailable():
            raise RuntimeError("device unavailable")

        self.use_rerank(unavailable)
        self.assert_fault(self.chat_trace(), baseline, "gpu_unavailable")
        with patch.object(database.settings, "PRODUCT_RERANK_USER_IDS", frozenset()), \
                patch.object(product_chat_search, "get_queue") as queue:
            self.assertEqual(self.chat_trace()[0], baseline)
            queue.assert_not_called()
        with patch.object(database.settings, "PRODUCT_RERANK_ENABLED", False), \
                patch.object(product_chat_search, "get_queue") as queue:
            self.assertEqual(self.chat_trace()[0], baseline)
            queue.assert_not_called()

    def test_scope_changes_after_search_cannot_publish_obsolete_evidence(self):
        class FixedGPU:
            def score(self, query, passages):
                return [0] * len(passages)

        self.use_rerank(FixedGPU)
        search = product_evidence_reply.search
        owner_id = self.users["owner"]
        for grouped in (False, True):
            for change in ("version", "owner", "session"):
                with self.subTest(grouped=grouped, change=change), closing(database.get_db()) as conn:
                    self.owner = self.login("owner")
                    self.activate(conn, "v1")
                    old = self.search(self.owner).json()["evidence"][0]["chunk_id"]
                    body = {"message": CHAT}
                    if grouped:
                        created = self.client.post("/api/chat/conversations", headers=self.owner,
                                                   json={"title": "商品资料发布边界"})
                        body["conversation_id"] = created.raise_for_status().json()["data"]["conversation_id"]
                    previous_id = conn.execute("SELECT coalesce(max(id), 0) FROM conversations").fetchone()[0]
                    published_candidate = []

                    def change_after_search(*args, **kwargs):
                        result = search(*args, **kwargs)
                        if not published_candidate:
                            published_candidate.extend(cite["chunk_id"] for cite in result["evidence"])
                            with closing(database.get_db()) as writer:
                                if change == "version":
                                    self.activate(writer, "v2")
                                elif change == "owner":
                                    with writer:
                                        writer.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? "
                                                       "AND marketplace_id=?",
                                                       (self.users["other"], TARGET["shop_id"], TARGET["marketplace_id"]))
                                else:
                                    with writer:
                                        writer.execute("DELETE FROM sessions WHERE user_id=?", (owner_id,))
                        return result

                    try:
                        with patch.object(product_evidence_reply, "search", side_effect=change_after_search):
                            response = self.client.post("/api/chat/stream", headers=self.owner, json=body)
                        self.assertIn(old, published_candidate, response.text)
                        events = [json.loads(line[6:]) for line in response.raise_for_status().text.splitlines()
                                  if line.startswith("data: ")]
                        terminals = [event for event in events if event["type"] in {"done", "error"}]
                        self.assertEqual(terminals, [events[-1]])
                        self.assertNotIn("304 stainless steel", response.text)
                        self.assertNotIn(old, response.text)
                        if change in {"owner", "session"}:
                            self.assertNotIn("/api/product-evidence/chunks/", response.text)
                        if grouped:
                            persisted = conn.execute("SELECT content FROM chat_group_entries WHERE kind='assistant' "
                                                     "AND run_id=?", (events[0]["run_id"],)).fetchall()
                        else:
                            persisted = conn.execute("SELECT content FROM conversations WHERE role='assistant' "
                                                     "AND user_id=? AND id>?", (owner_id, previous_id)).fetchall()
                        self.assertTrue(all(old not in row["content"] and "304 stainless steel" not in row["content"]
                                            for row in persisted))
                        self.assertEqual(self.original(old, self.owner).status_code, 401 if change == "session" else 404)
                    finally:
                        if change == "owner":
                            with conn:
                                conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                                             (owner_id, TARGET["shop_id"], TARGET["marketplace_id"]))
        self.memory.assert_not_called()
        self.summary.assert_not_called()

    def await_pending(self, queue):
        deadline = monotonic() + 2
        while not queue.pending and monotonic() < deadline:
            sleep(.005)
        self.assertEqual(len(queue.pending), 1)

    def assert_fault(self, output, baseline, reason):
        answer, step, trace_id = output
        self.assertEqual(answer, baseline)
        cpu_path = "r4_cpu" if database.settings.PRODUCT_R4_CPU_ENABLED else "default"
        self.assertEqual((step["path"], step["cpu_path"], step["fallback_reason"]), (cpu_path, cpu_path, reason))
        print("GPU fault:", json.dumps({"trace_id": trace_id, "reason": reason, "citation_ids": step["citation_ids"],
                                      "queue_depth": step.get("queue_depth"), "queue_wait_ms": step.get("queue_wait_ms")}))
        return trace_id

    def test_queued_release_transfer_and_session_revocation(self):
        started, release = Event(), Event()
        queries = []

        class BarrierGPU:
            def score(self, query, passages):
                queries.append(query)
                started.set()
                if not release.wait(5):
                    raise RuntimeError("barrier timed out")
                return [0] * len(passages)

        queue = self.use_rerank(BarrierGPU, wait=5000, inference=5000)
        for change in ("version", "owner", "session"):
            with self.subTest(change=change), closing(database.get_db()) as conn:
                old = self.search(self.owner).json()["evidence"][0]["chunk_id"]
                started.clear()
                release.clear()
                queries.clear()
                with ThreadPoolExecutor(2) as executor:
                    # Occupy the slot using an unrelated, authorized SKU.
                    first = executor.submit(self.chat_trace, CHAT.replace("METRO-500", "METRO-750"))
                    try:
                        self.assertTrue(started.wait(3))
                        waiting = executor.submit(self.chat_trace)
                        self.await_pending(queue)
                        if change == "version":
                            self.activate(conn, "v2")
                        elif change == "owner":
                            with conn:
                                conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                                             (self.users["other"], TARGET["shop_id"], TARGET["marketplace_id"]))
                        else:
                            with conn:
                                conn.execute("DELETE FROM sessions WHERE user_id=?", (self.users["owner"],))
                    finally:
                        release.set()
                    first.result(5)
                    answer, step, _ = waiting.result(5)
                self.assertEqual(len(queries), 1)
                self.assertEqual(step["scored_chunks"], 0)
                self.assertEqual(step["fallback_reason"], "scope_changed_while_queued")
                if change == "version":
                    self.assertIn("316 stainless steel", answer)
                    self.assertEqual(self.original(old, self.owner).status_code, 404)
                    self.assertEqual(self.original(step["citation_ids"][0], self.owner).status_code, 200)
                else:
                    self.assertFalse(step.get("citation_ids"))
                    self.assertNotIn("stainless steel", answer)
                    self.assertEqual(self.original(old, self.owner).status_code, 404 if change == "owner" else 401)
                print("Queued scope change:", json.dumps({"change": change, "old_chunk": old,
                       "old_http": self.original(old, self.owner).status_code, "scored_chunks": step["scored_chunks"],
                       "fallback_reason": step["fallback_reason"], "citation_ids": step.get("citation_ids", [])}))
                if change == "owner":
                    with conn:
                        conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                                     (self.users["owner"], TARGET["shop_id"], TARGET["marketplace_id"]))
