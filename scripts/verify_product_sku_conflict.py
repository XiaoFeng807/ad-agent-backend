"""Replay SKU selection and mismatch through authenticated product chat SSE."""

import argparse
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch
from uuid import uuid4

from backend import observability
from backend.database import database
from backend.rag import product_chat_search, product_evidence as rag
from scripts.eval_product_chat_rerank import TraceReader
from scripts.eval_product_generalization_t1 import PACK, RetrievalAudit, check_pack, read, readback, score, sse, sse_message
from scripts.eval_product_r5 import loopback
from scripts.product_evidence import import_pack, use_sandbox, write_json
from scripts.verify_product_evidence import login


class FixedEmbedding:
    def __init__(self, *, device=None):
        self._device = device

    def __call__(self, input):
        return [[int(term in text) for term in ("Maximum working pressure\t", "Internal battery capacity\t",
                                                "Nozzle clip replacement code\t")] for text in input]

    def embed_query(self, input):
        return [[int(term in text) for term in ("工作压力", "电芯容量", "DP-NC4")] for text in input]


def replay(output):
    _, frozen_questions = check_pack()
    questions = {q["id"]: q for q in frozen_questions}
    contract = read(PACK / "sku-conflict-r1.json")
    n08 = questions["N08"]
    use_sandbox(output / "fixed.sqlite3")
    output.mkdir(parents=True)
    database.settings.JWT_SECRET = token_urlsafe(48)
    database.init_db()
    from fast_server import app

    originals = {p.name: p.read_bytes().decode("utf-8") for p in (PACK / "sources").iterdir()}
    with (
        closing(database.get_db()) as writer,
        patch.object(observability, "TRACE_LOG_FILE", output / "trace.jsonl"),
        patch.object(rag, "BGEEmbedding", FixedEmbedding),
        loopback(app) as client,
    ):
        users, _ = import_pack(writer, PACK / "manifest.json", seed=True)
        rag.activate(writer, n08["target"], n08["version"])
        for qid in ("S01", "S02"):
            rag.activate(writer, questions[qid]["target"], questions[qid]["version"])
        audit = RetrievalAudit()
        headers = {principal: login(client, principal) for principal in users}
        traces = TraceReader(observability.TRACE_LOG_FILE)
        group = client.post("/api/chat/conversations", headers=headers["owner"], json={"title": "SKU 确认"})
        group_id = group.raise_for_status().json()["data"]["conversation_id"]
        rows = {}

        def run(label, q, *, group=None, view=None, case=None):
            auth = headers[q["principal"]]
            row = sse(client, auth, q, traces, writer, users[q["principal"]], audit,
                      conversation_id=group, view=view, refresh_history=True)
            checks = readback(client, auth, row, originals)
            rows[label] = score(row, checks, q["required"], conflict=(case, contract["acceptance"]) if case else None)
            if not group:
                saved = row["committed_history"]
                refreshed = [(entry["role"], entry["content"]) for entry in row["refreshed_history"][-2:]]
                row["refresh_matches_commit"] = refreshed == [(entry["role"], entry["content"]) for entry in saved]
                row["passed"] &= row["refresh_matches_commit"]

        with (
            patch.object(rag.ProductEvidence, "retrieve", autospec=True, side_effect=audit.capture),
            patch.object(FixedEmbedding, "embed_query", autospec=True, side_effect=audit.count),
            patch.object(product_chat_search, "get_queue") as gpu_queue,
        ):
            pressure = {**questions["V06"], "query": "DRIFT-P12 的宽喷头工作压力是多少？"}
            for label, group in (("chat", None), ("workgroup", group_id)):
                view = uuid4().hex
                run(f"N08_{label}", n08, group=group, view=view, case=contract["cases"]["N08"])
                run(f"confirm_unknown_{label}", {
                    **n08, "target": {**n08["target"], "sku": "DRIFT-P30"}, "expectation": "denied",
                }, group=group, view=view)
                run(f"confirm_bound_{label}", pressure, group=group, view=view)

            view = uuid4().hex
            run("conflict_before_version_change", n08, view=view, case=contract["cases"]["N08"])
            rag.activate(writer, n08["target"], "v1")
            run("confirm_after_version_change", {**questions["V05"], "query": pressure["query"]}, view=view)
            rag.activate(writer, n08["target"], n08["version"])

            run("unauthorized_binding", {**n08, "principal": "other", "expectation": "denied"})
            run("part_only", {**n08, "query": "DP-NC4 卡扣能到店自取吗？"})
            run("similar_prefix", {**n08, "query": "DRIFT-P30-KIT 配件能到店自取吗？"})
            run("labeled_unknown", {**n08, "query": "商品型号 AURORA 的宽喷头工作压力是多少？"},
                case={"bound_sku": "DRIFT-P12", "mentioned_skus": ["AURORA"]})
            arc220 = {**questions["S01"], "query": "这款的电芯容量是多少 mAh？"}
            arc480 = {**questions["S02"], "query": arc220["query"]}
            run("same_name_220", arc220)
            run("same_name_480", arc480)
            query = "ARC-480 的电芯容量是多少 mAh？"
            view = uuid4().hex
            run("arc_conflict", {**arc220, "query": query}, view=view,
                case={"bound_sku": "ARC-220", "mentioned_skus": ["ARC-480"]})
            run("arc_confirm_unauthorized", {**arc480, "principal": "other", "query": query,
                                              "expectation": "denied", "required": []})
            run("arc_confirm_authorized", {**arc480, "query": query}, view=view)

            english_conflict = {"bound_sku": "ARC-220", "mentioned_skus": ["ARC-480"],
                                "forbidden_text": ["Internal battery capacity"]}
            view = uuid4().hex
            for label, prefix in (("sku_is", "The SKU is"), ("sku_number", "SKU number")):
                run(f"{label}_matches", {**arc220, "query": f"{prefix} ARC-220. {arc220['query']}"})
                run(f"{label}_conflicts", {**arc220, "query": f"{prefix} ARC-480. {arc220['query']}"},
                    view=view, case=english_conflict)
            run("sku_direct_matches", {**arc220, "query": f"SKU ARC-220. {arc220['query']}"})
            run("sku_direct_unknown", {**arc220, "query": "SKU NUMBER-123. " + arc220["query"]},
                case={**english_conflict, "mentioned_skus": ["NUMBER-123"]})
            run("sku_number_unknown", {**arc220, "query": "SKU number: AURORA. " + arc220["query"]},
                case={**english_conflict, "mentioned_skus": ["AURORA"]})
            run("english_followup_bound", arc220, view=view)
            confirm = {**arc480, "query": f"SKU number ARC-480. {arc480['query']}"}
            for field, value in (("shop_id", "t1-outlet"), ("marketplace_id", "CA")):
                run(f"english_confirm_wrong_{field}", {
                    **confirm, "target": {**confirm["target"], field: value}, "expectation": "denied", "required": [],
                }, view=view)

            stale_session = headers["owner"]
            headers["owner"] = login(client, "owner")
            audit.reset()
            body = {"message": sse_message(confirm), "chat_view_id": view}
            response = client.post("/api/chat/stream", headers=stale_session, json=body)
            rows["english_confirm_stale_session"] = {
                "request": {"path": "/api/chat/stream", "principal": "owner", **body},
                "sse_http": response.status_code, "response": response.json(),
                "encoding_calls": audit.encoding, "retrieval_calls": audit.retrievals,
                "passed": response.status_code == 401 and audit.encoding == audit.retrievals == 0,
            }
            run("english_confirm_authorized", confirm, view=view)
            gpu_queue.assert_not_called()

        for label in ("part_only", "similar_prefix", "sku_is_matches", "sku_number_matches",
                      "sku_direct_matches", "english_followup_bound", "english_confirm_authorized"):
            rows[label]["passed"] &= rows[label]["retrieval_calls"] == 1
        write_json(output / "rows.json", rows)
        write_json(output / "summary.json", {key: row["passed"] for key, row in rows.items()})
        assert all(row["passed"] for row in rows.values()), [key for key, row in rows.items() if not row["passed"]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    replay(parser.parse_args().output.resolve())
