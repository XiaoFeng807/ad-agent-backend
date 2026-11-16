"""Replay the focused multi-intent questions through authenticated product SSE."""

import argparse
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch

from backend import observability
from backend.agent import product_evidence_reply
from backend.database import database, listing_operations, product_documents
from backend.rag import product_evidence as rag
from scripts.eval_product_chat_rerank import CITATION, TraceReader
from scripts.eval_product_generalization_t1 import (
    MULTI_INTENT_FOCUS, PACK, RetrievalAudit, check_pack, focused_checks, read, readback, score, sse,
)
from scripts.eval_product_r5 import loopback
from scripts.product_evidence import import_pack, use_sandbox, write_json
from scripts.verify_product_evidence import login

class FixedEmbedding:
    def __init__(self, *, device=None):
        self._device = device

    def __call__(self, texts):
        return [[int(term in text) for term in ("## bike-rinse |", "Maximum working pressure\t",
                                                "Reservoir rated volume\t", "## 2 | Working water temperature")]
                for text in texts]

    def embed_query(self, texts):
        return [[int(term in text) for term in ("链罩", "水压", "水箱", "水温")] for text in texts]


def replay(output, before):
    _, questions = check_pack()
    questions = {q["id"]: q for q in questions}
    conflict = read(PACK / "sku-conflict-r1.json")
    output.mkdir(parents=True)
    use_sandbox(output / "fixed.sqlite3")
    database.settings.JWT_SECRET = token_urlsafe(48)
    assert database.settings.PRODUCT_R4_CPU_ENABLED and not database.settings.PRODUCT_RERANK_ENABLED
    database.init_db()
    from fast_server import app

    originals = {path.name: path.read_bytes().decode("utf-8") for path in (PACK / "sources").iterdir()}
    with (
        closing(database.get_db()) as writer,
        patch.object(observability, "TRACE_LOG_FILE", output / "trace.jsonl"),
        patch.object(rag, "BGEEmbedding", FixedEmbedding),
        loopback(app, lifespan="off") as client,
    ):
        users, _ = import_pack(writer, PACK / "manifest.json", seed=True)
        auth = login(client, "owner")
        audit = RetrievalAudit()
        traces = TraceReader(observability.TRACE_LOG_FILE)
        rows = {}

        def run(label, q, *, group=None):
            rag.activate(writer, q["target"], q["version"])
            row = sse(client, auth, q, traces, writer, users["owner"], audit,
                      conversation_id=group, refresh_history=True, capture_wire=True)
            checks = readback(client, auth, row, originals, capture_wire=True)
            case = conflict["cases"].get(q["id"])
            rows[label] = score(row, checks, q["required"],
                                conflict=(case, conflict["acceptance"]) if case else None)
            return row

        def focus(label, q):
            row = run(label, q)
            if not before:
                row["multi_intent_checks"] = focused_checks(row)
                row["passed"] &= all(row["multi_intent_checks"].values())
            return row

        def boundary(q, target, version, label):
            rag.activate(writer, target, version)
            removed = q["removed_source"]
            foreign, = [
                hit for hit in map(rag._hit, product_documents.scoped_chunks(
                    writer, users["owner"], rag._scope(target), "structured"))
                if hit["document_id"] == removed["document_id"]
                and any(loc["location"] == removed["location"] and loc["complete"] for loc in hit["locations"])
            ]

            def inject(service, *args, **kwargs):
                result, snapshot = audit.capture(service, *args, **kwargs)
                result["candidates"] = [foreign, *result["candidates"][:rag.RECALL_K - 1]]
                audit.ranked["merged"] = [hit["chunk_id"] for hit in result["candidates"]]
                return result, snapshot

            with patch.object(rag.ProductEvidence, "retrieve", inject):
                row = focus(label, q)
            cid = foreign["chunk_id"]
            row["injected_candidate"] = foreign
            row["passed"] &= (cid in row["retrieval_trace"]["candidate_ids"]
                              and cid not in row["retrieval_trace"]["citation_ids"]
                              and cid not in CITATION.findall(row["committed_history"][-1]["content"]))

        with (
            patch.object(rag.ProductEvidence, "retrieve", autospec=True, side_effect=audit.capture),
            patch.object(FixedEmbedding, "embed_query", autospec=True, side_effect=audit.count),
        ):
            for qid in MULTI_INTENT_FOCUS:
                focus(qid, questions[qid])
            if not before:
                group = client.post("/api/chat/conversations", headers=auth, json={"title": "商品资料引用回归"})
                group_id = group.raise_for_status().json()["data"]["conversation_id"]
                run("N08_chat", questions["N08"])
                run("N08_workgroup", questions["N08"], group=group_id)
                for qid in ("M04", "M08"):
                    q = questions[qid]
                    boundary(q, q["target"], "v1", qid + "_old_version")

                target = {**questions["M03"]["target"], "sku": "DRIFT-P30"}
                release, = [r for r in read(PACK / "manifest.json")["releases"]
                            if r["target"] == questions["M03"]["target"] and r["version"] == "v2"]
                with writer:
                    listing_operations.seed_test_product(writer, users["owner"], **target)
                rag.import_release(writer, {**release, "target": target}, PACK / "sources")
                for qid in ("M04", "M08"):
                    boundary(questions[qid], target, "v2", qid + "_wrong_sku")

                full = questions["M03"]
                duplicate = run("duplicate_original", {
                    **full, "query": "喷头离链罩多远，喷头离链罩多远，工作水压是多少？",
                })
                ids = duplicate["retrieval_trace"]["citation_ids"]
                duplicate["passed"] &= (len(ids) == 2 and len(set(ids)) == 2 and
                    [check["chunk_ids"] for check in duplicate["retrieval_trace"]["fact_checks"]]
                    == [[ids[0]], [ids[0]], [ids[1]]])

                admit = rag.admit_evidence

                def conflicting_rule(result, snapshot, query, **options):
                    snapshot = [dict(row) for row in snapshot]
                    rules = read(PACK / "manifest.json")["releases"]
                    rules = next(r["evidence_rules"] for r in rules if r["target"] == full["target"]
                                 and r["version"] == full["version"])
                    snapshot[0]["evidence_rules"] = rag._json([
                        *rules, {"pattern": "工作水压又是多少", "requires": [["specs", "row:3"]]},
                    ])
                    return admit(result, snapshot, query, **options)

                with patch.object(rag, "admit_evidence", conflicting_rule):
                    ambiguous = run("ambiguous_subquestion", {**full, "required": [], "expectation": "clarify"})
                ambiguous["passed"] = (
                    ambiguous["done"] and ambiguous["history_ok"]
                    and ambiguous["reply"] == "请澄清以下子问题具体指哪项规格：工作水压又是多少。"
                    and ambiguous["committed_history"][-1]["content"] == ambiguous["reply"]
                    and not ambiguous["http_readback"] and not ambiguous["retrieval_trace"]["citation_ids"]
                    and [check["status"] for check in ambiguous["retrieval_trace"]["fact_checks"]]
                    == ["verified", "ambiguous"]
                )

                search = product_evidence_reply.search

                def activate_before_commit(*args, **kwargs):
                    result = search(*args, **kwargs)
                    with closing(database.get_db()) as switch:
                        rag.activate(switch, full["target"], "v1")
                    return result

                partial = questions["M04"]
                rag.activate(writer, partial["target"], partial["version"])
                with patch.object(product_evidence_reply, "search", activate_before_commit):
                    changed = sse(client, auth, partial, traces, writer, users["owner"], audit, capture_wire=True)
                changed["passed"] = (
                    changed["retrieval_trace"]["status"] == "partial_support"
                    and changed["sse_events"][-1].get("error_code") == "product_evidence_scope_changed"
                    and not changed["reply"]
                    and all(entry["role"] != "assistant" for entry in changed["committed_history"])
                    and changed["persistence_trace"]["evidence"]["final_citation_ids"] == []
                )
                rows["partial_version_changed_before_commit"] = changed
        write_json(output / "rows.json", rows)
        write_json(output / "summary.json", {key: row["passed"] for key, row in rows.items()})
        assert all(rows[qid]["all_top20"] for qid in MULTI_INTENT_FOCUS)
        assert all(row["passed"] != before for row in rows.values()), [
            key for key, row in rows.items() if row["passed"] == before
        ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before", action="store_true")
    args = parser.parse_args()
    replay(args.output.resolve(), args.before)
