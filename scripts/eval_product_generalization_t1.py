"""Run the held-out product evidence acceptance on the real authenticated SSE route."""

import argparse
import json
import os
import re
import sys
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch

from backend import observability
from backend.database import database, product_documents
from backend.rag import chinese_embedding, product_evidence as rag
from scripts.eval_product_chat_rerank import CITATION, TraceReader
from scripts.eval_product_query_expansion_r4 import PINNED_MODEL, check_inputs, digest
from scripts.eval_product_r5 import TERMINALS, history, loopback
from scripts.product_evidence import FIXTURES, import_pack, use_sandbox, write_json
from scripts.verify_product_evidence import CHUNKS, SEARCH, login

PACK = FIXTURES / "generalization-t1"
BASELINE = Path("logs/product-r7/final/default/questions.json")
SCENARIOS = ("same_name", "version", "multi_document", "no_answer")
MULTI_INTENT_FOCUS = {
    "M03": ("bike_rinse_distance", "wide_nozzle_pressure", None),
    "M04": ("bike_rinse_distance", "wide_nozzle_pressure", "宽喷头最大工作压力"),
    "M07": ("reservoir_rated_volume", "pump_inlet_temperature", None),
    "M08": ("reservoir_rated_volume", "pump_inlet_temperature", "泵入口水温上限"),
}


def read(path):
    return json.loads(path.read_bytes())


def inputs():
    return {
        "files": {path.relative_to(PACK).as_posix(): digest(path) for path in [
            PACK / "manifest.json", PACK / "questions.json", PACK / "probes.json", PACK / "rubric.md",
            *sorted((PACK / "sources").iterdir()),
        ]},
        "model": PINNED_MODEL,
        "settings": {
            "strategy": "structured", "mode": "vector", "expansion": "both",
            "candidate_k": rag.RECALL_K, "policy": asdict(rag.DEFAULT_EXPANSION_POLICY),
            "device": "cpu", "PRODUCT_R4_CPU_ENABLED": True, "PRODUCT_RERANK_ENABLED": False,
        },
    }


def check_pack():
    frozen = read(PACK / "freeze.json")
    assert frozen == inputs(), "T1 frozen inputs or retrieval configuration changed"
    questions = read(PACK / "questions.json")
    pack = read(PACK / "manifest.json")
    catalog = {tuple(rag._scope(row["target"])): row["principal"] for row in pack["catalog"]}
    releases = {(rag._scope(r["target"]), r["version"]): r for r in pack["releases"]}
    assert len({q["id"] for q in questions}) == len(questions) == 48
    assert Counter(q["scenario"] for q in questions) == dict.fromkeys(SCENARIOS, 12)
    for q in questions:
        if q["expectation"] == "clarify":
            assert q["target"] is None and len(q["scope_choices"]) == 2
            assert all(catalog[rag._scope(scope)] == q["principal"] for scope in q["scope_choices"])
            continue
        scope = rag._scope(q["target"])
        assert catalog[scope] == q["principal"]
        release = releases[scope, q["version"]]
        docs = {doc["id"]: doc for doc in release["documents"]}
        for ref in [*(r for group in q["required"] for r in group), *q.get("distractors", [])]:
            doc = docs[ref["document_id"]]
            assert doc["file"] == ref["filename"], q["id"]
            raw = (PACK / "sources" / doc["file"]).read_bytes().decode("utf-8")
            assert ref["quote"] == raw[ref["start_offset"]:ref["end_offset"]], q["id"]
            region, = [region for region in rag.source_regions(doc["kind"], raw)
                       if region["location"] == ref["location"]]
            assert all(region[key] == ref[key] for key in ("start_offset", "end_offset")), q["id"]
        if q["expectation"] == "partial":
            counterpart, = [r for r in questions if r.get("pair") == q["pair"] and r["expectation"] == "supported"]
            full_docs = {d["id"]: d for d in releases[scope, counterpart["version"]]["documents"]}
            removed = q["removed_source"]
            assert (q["query"], q["target"]) == (counterpart["query"], counterpart["target"])
            assert docs == {key: val for key, val in full_docs.items() if key != removed["document_id"]}
            assert any(removed in group for group in counterpart["required"])
    return frozen, questions


def runtime():
    from huggingface_hub import try_to_load_from_cache
    from importlib.metadata import version
    import torch

    assert os.environ.get("AD_OFFLINE_ACCEPTANCE") == "1"
    assert os.environ.get("PYTHON_DOTENV_DISABLED") == "1"
    assert os.environ.get("LLM_PROVIDER") == "mock"
    assert not any(name.upper() in {"PRODUCT_R4_CPU_ENABLED", "PRODUCT_RERANK_ENABLED"} for name in os.environ)
    assert database.settings.PRODUCT_R4_CPU_ENABLED and not database.settings.PRODUCT_RERANK_ENABLED
    assert rag.BGE_ID == PINNED_MODEL
    assert not torch.cuda.is_available(), "Use the CPU runtime"
    weights = try_to_load_from_cache(PINNED_MODEL.split("@")[0], "model.safetensors",
                                     revision=chinese_embedding.BGE_REVISION)
    assert isinstance(weights, str), "Missing pinned BGE weights; set HF_HOME to the local cache"
    return {"python": sys.version.split()[0], "torch": torch.__version__,
            "sentence_transformers": version("sentence-transformers"), "weights_sha256": digest(Path(weights))}


class RetrievalAudit:
    def __init__(self):
        self.retrieve = rag.ProductEvidence.retrieve
        self.encode = rag.BGEEmbedding.embed_query
        self.reset()

    def reset(self):
        self.encoding = 0
        self.retrievals = 0
        self.ranked = None

    def capture(self, service, conn, uid, target, query, **options):
        self.retrievals += 1
        result, rows = self.retrieve(service, conn, uid, target, query, **{**options, "audit": True})
        self.ranked = {
            "target": target, "version": result["version"], "query": query, "options": options, "queries": result["queries"],
            "channels": {key: ids(hits) for key, hits in result["channels"].items()},
            "full_orders": result["full_vector_orders"], "merged": ids(result["candidates"]),
            "pool": {hit["chunk_id"]: hit for hit in map(rag._hit, rows)},
        }
        return {key: value for key, value in result.items() if key not in rag.AUDIT_FIELDS}, rows

    def count(self, encoder, texts):
        assert encoder._device == "cpu"
        self.encoding += len(texts)
        return self.encode(encoder, texts)


def ids(hits):
    return [hit["chunk_id"] for hit in hits]


def sse_message(q):
    if q["target"] is None:
        scope = q["unbound_scope"]
        return f"商品资料：{scope['shop_id']} {scope['marketplace_id']}\n{q['query']}"
    return "商品资料：" + " ".join([*rag._scope(q["target"]), q["query"]])


def sse(client, auth, q, trace_reader, writer, uid, audit, *, conversation_id=None, view=None, refresh_history=False,
        capture_wire=False):
    from uuid import uuid4

    view = view or uuid4().hex
    message = sse_message(q)
    body = {"message": message, "chat_view_id": view}
    if conversation_id:
        body["conversation_id"] = conversation_id
    audit.reset()
    with client.stream("POST", "/api/chat/stream", headers=auth, json=body) as response:
        wire = response.read().decode("utf-8") if capture_wire else None
        lines = wire.split("\n") if capture_wire else response.iter_lines()
        events = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
    terminal = [event for event in events if event["type"] in TERMINALS]
    trace_id = terminal[-1].get("trace_id") if terminal else None
    trace = trace_reader.read(trace_id, full=True) if trace_id else {}
    steps = trace.get("steps", [])
    retrieval = next((s["evidence"] for s in steps if s["action"] == "chat_retrieval"), {})
    persistence = next((s for s in steps if s["action"] == "save_reply"), {})
    if conversation_id:
        refresh = client.get(f"/api/chat/conversations/{conversation_id}/history", headers=auth).raise_for_status().json()["data"]
        run_id = terminal[-1]["run_id"]
        saved = [{"id": entry["id"], "role": entry["kind"], "content": entry["content"]}
                 for entry in refresh["entries"] if entry["run_id"] == run_id]
    else:
        refresh = (client.get("/api/chat/history", headers=auth).raise_for_status().json()["data"]
                   if refresh_history else None)
        saved = history(writer, uid, view)[-2:]
    text = "".join(event["content"] for event in events if event["type"] == "text")
    ranked = audit.ranked
    merged = ranked["merged"] if ranked else []
    attestation = (
        retrieval.get("candidate_ids", []) == merged
        and retrieval.get("path", "r4_cpu") == "r4_cpu"
        and not retrieval.get("eligible", False) and not retrieval.get("scored_chunks", 0)
        and audit.encoding == (len(ranked["queries"]) if ranked else 0)
    )
    return {
        "id": q["id"], "input": q, "request": {"path": "/api/chat/stream", "principal": q["principal"], **body},
        "sse_http": response.status_code, "sse_events": events, "trace_id": trace_id,
        "done": bool(events) and terminal == [events[-1]] and terminal[0]["type"] == "done",
        "reply": text, "encoding_calls": audit.encoding, "retrieval_calls": audit.retrievals,
        "ranked": ranked, "retrieval_trace": retrieval,
        "persistence_trace": persistence, "committed_history": saved, "ranking_attested": attestation,
        "refreshed_history": refresh,
        **({"sse_wire": wire} if capture_wire else {}),
    }


def readback(client, auth, row, original_files, *, capture_wire=False):
    checks = []
    q = row["input"]
    previous = 0
    for link in CITATION.finditer(row["reply"]):
        cid = link[1]
        response = client.get(CHUNKS + cid, headers=auth)
        block = row["reply"][previous:link.start()]
        previous = link.end()
        check = {"chunk_id": cid, "http": response.status_code}
        if capture_wire:
            check["http_body"] = response.text
        checks.append(check)
        if response.status_code != 200:
            check.update(ok=False, errors=["unexpected_http_status"])
            continue
        hit = response.json()
        ranked_hit = (row["ranked"] or {}).get("pool", {}).get(cid)
        filename = hit["filename"]
        original = original_files.get(filename)
        original_hash = sha256(hit["original"].encode()).hexdigest()
        locations = [loc for loc in hit["locations"] if loc["complete"] and
                     f"{filename} · {loc['location']} · {loc['section']}\n" in block]
        expected_regions = [] if original is None else rag.source_regions(hit["kind"], original)
        start, end = hit["start_offset"], hit["end_offset"]
        loc = locations[0] if len(locations) == 1 else {}
        quote = hit["original"][loc.get("start_offset", 0):loc.get("end_offset", 0)]
        tests = {
            "id": hit["chunk_id"] == cid, "scope": bool(q["target"]) and
            all(hit[k] == v for k, v in q["target"].items()),
            "version": hit["version"] == q["version"] and hit["active"],
            "original": hit["original"] == original,
            "ranked_chunk": ranked_hit == {key: value for key, value in hit.items() if key != "original"},
            "chunk_offsets": 0 <= start < end <= len(hit["original"]) and hit["content"] == hit["original"][start:end],
            "location": bool(loc) and start <= loc["start_offset"] < loc["end_offset"] <= end
            and {k: v for k, v in loc.items() if k != "complete"} in expected_regions,
            "quote": bool(quote) and block.endswith(f"\n{quote}\n原文回查（需登录） "),
        }
        check.update(**loc, filename=filename, quote=quote, version=hit["version"], document_id=hit["document_id"],
                     scope={key: hit[key] for key in product_documents.SCOPE_KEYS},
                     chunk_offsets=[start, end], original_sha256=original_hash,
                     errors=[key for key, ok in tests.items() if not ok], ok=all(tests.values()))
    return checks


def focused_checks(row):
    q = row["input"]
    first, second, missing = MULTI_INTENT_FOCUS[q["id"]]
    checks = row["retrieval_trace"].get("fact_checks", [])
    cites = ids(row["http_readback"])
    reply = row["reply"]
    saved = row["committed_history"][-1]["content"]
    refreshed = [(entry["role"], entry["content"]) for entry in row["refreshed_history"][-2:]]
    committed = [(entry["role"], entry["content"]) for entry in row["committed_history"]]
    lead = f"证据不足。本次检索缺少以下依据：{missing}。" if missing else ""
    return {
        "facts_in_question_order": [check["facts"] for check in checks] == [[first], [second]],
        "fact_outcomes": [check["status"] for check in checks] == ["verified", "missing" if missing else "verified"],
        "fact_citation_ids": [cid for check in checks for cid in check["chunk_ids"]] == cites,
        "ordered_http_originals": len(cites) == len(q["required"]) and all(
            source["ok"] and any(all(source[key] == val for key, val in ref.items()) for ref in group)
            for source, group in zip(row["http_readback"], q["required"])
        ),
        "status": row["retrieval_trace"].get("status") == ("partial_support" if missing else "supported"),
        "citation_ids": row["retrieval_trace"].get("citation_ids") == CITATION.findall(saved) == cites,
        "refreshed_history": committed == refreshed,
        "missing_lead": not missing or reply.startswith(lead + "\n\n") and saved.startswith(lead + "\n\n"),
        "no_missing_fact_value": not missing or all(
            q["removed_source"]["quote"] not in text
            and not re.search(r"\d(?:\.\d+)?\s*(?:bar\b|°C\b)", text, re.I)
            for text in (reply, saved)
        ),
    }


def ref_rank(group, pool, order):
    for rank, cid in enumerate(order, 1):
        hit = pool.get(cid)
        if hit is None:
            continue
        for ref in group:
            if hit["document_id"] != ref["document_id"] or hit["filename"] != ref.get("filename", hit["filename"]):
                continue
            if any(loc["location"] == ref["location"] and loc["complete"] for loc in hit["locations"]):
                start = hit["start_offset"]
                if "quote" not in ref or hit["content"][ref["start_offset"] - start:ref["end_offset"] - start] == ref["quote"]:
                    return rank
    return None


def answer_body(row, checks):
    return "\n\n".join([
        f"{row['input']['target']['sku']} · {row['input']['version']}",
        *(f"{s['filename']} · {s['location']} · {s['section']}\n{s['quote']}\n原文回查（需登录） {CHUNKS}{s['chunk_id']}"
          for s in checks),
    ])


def insufficient_reply(row, checks, only_gold):
    reply = row["reply"]
    generic = "证据不足。当前有效资料未提供足以支持所问事实的完整依据。"
    missing_prefix = r"证据不足。(?:本次检索|当前版本)缺少以下依据："
    lead, separator, body = reply.partition("\n\n")
    specific = bool(re.fullmatch(missing_prefix + r"[^。\n]+。", lead))
    if not checks:
        return not separator and (lead == generic or specific), lead
    partial = row["input"]["expectation"] == "partial"
    valid_lead = lead == "证据不足。" or specific
    return (partial and only_gold and valid_lead and bool(separator) and body == answer_body(row, checks)), lead


def conflict_checks(row, case, acceptance):
    trace = row["retrieval_trace"]
    reply = acceptance["reply"].format(bound_sku=case["bound_sku"], mentioned_skus="、".join(case["mentioned_skus"]))
    history_reply = row["committed_history"][-1]["content"] if row["committed_history"] else ""
    refreshed = row["refreshed_history"]
    if isinstance(refreshed, dict):
        run_id = row["sse_events"][-1].get("run_id")
        assistant = [entry["content"] for entry in refreshed["entries"]
                     if entry["kind"] == "assistant" and entry["run_id"] == run_id]
    else:
        assistant = [reply_entry["content"] for user_entry, reply_entry in zip(refreshed, refreshed[1:])
                     if user_entry["role"] == "user" and user_entry["content"] == row["request"]["message"]
                     and reply_entry["role"] == "assistant"][-1:]
    return {
        "sse_confirmation": row["reply"] == reply,
        "history_confirmation": history_reply == reply and assistant == [reply],
        "binding_unchanged": (row["input"]["target"]["sku"] == case["bound_sku"] == trace.get("bound_sku")
                              and bool(row["committed_history"])
                              and row["committed_history"][0]["content"] == row["request"]["message"]),
        "mentioned_skus": trace.get("mentioned_skus") == case["mentioned_skus"],
        "trace_block": (trace.get("status"), trace.get("blocked_at")) ==
                       (acceptance["trace_status"], acceptance["trace_blocked_at"]),
        "zero_retrieval": row["retrieval_calls"] == acceptance["retrieval_calls"] and row["ranked"] is None,
        "zero_encoding": row["encoding_calls"] == acceptance["encoding_calls"],
        "zero_scoring": trace.get("scored_chunks") == acceptance["gpu_scored_chunks"],
        "zero_sse_citations": CITATION.findall(row["reply"]) == acceptance["sse_citation_ids"],
        "zero_history_citations": CITATION.findall(history_reply) == acceptance["history_citation_ids"],
        "zero_trace_citations": (trace.get("citation_ids") ==
                                 row["persistence_trace"].get("evidence", {}).get("final_citation_ids") ==
                                 acceptance["final_citation_ids"]),
        "zero_evidence_text": all(term not in text for term in case.get("forbidden_text", [])
                                  for text in [row["reply"], history_reply, *assistant]),
    }


def score(row, checks, groups, *, conflict=None):
    q, ranked = row["input"], row["ranked"]
    qstatus = q["expectation"]
    groups = groups or []
    pool = ranked["pool"] if ranked else {}
    order = ranked["merged"] if ranked else []
    ranks = []
    for group in groups:
        ranks.append({"gold": group, "indexed": ref_rank(group, pool, pool) is not None,
                      "merged": ref_rank(group, pool, order),
                      "channels": {name: ref_rank(group, pool, keys) for name, keys in (ranked or {}).get("channels", {}).items()},
                      "channel_full": {name: ref_rank(group, pool, keys) for name, keys in (ranked or {}).get("full_orders", {}).items()}})
    match = lambda src, ref: all(src.get(key) == value for key, value in ref.items())
    final_ranks = [any(s["ok"] and any(match(s, r) for r in group) for s in checks) for group in groups]
    cites = ids(checks)
    history_refs = CITATION.findall(row["committed_history"][-1]["content"]) if row["committed_history"] else []
    persisted = row["persistence_trace"].get("evidence", {}).get("final_citation_ids", [])
    history_ok = ([entry["role"] for entry in row["committed_history"]] == ["user", "assistant"]
                  and history_refs == persisted == cites and row["persistence_trace"].get("status") == "ok")
    target = {**q["target"], "version": q["version"]} if q["target"] else {}
    scoped = all(all(hit[key] == value for key, value in target.items()) for hit in pool.values())
    if qstatus == "denied":
        scoped &= row["retrieval_trace"].get("status") == "denied"
    top20 = bool(ranks) and all(item["merged"] is not None for item in ranks)
    only_gold = all(src["ok"] and any(match(src, ref) for group in groups for ref in group) for src in checks)
    complete = bool(final_ranks) and all(final_ranks) and only_gold
    abstention_ok, abstention_lead = insufficient_reply(row, checks, only_gold)
    if conflict:
        row["sku_conflict_checks"] = conflict_checks(row, *conflict)
        behavior_ok = all(row["sku_conflict_checks"].values())
    elif qstatus == "supported":
        behavior_ok = complete and row["reply"] == answer_body(row, checks)
    elif qstatus == "denied":
        behavior_ok = (row["reply"] == "当前账号无法访问该范围的商品资料，请核对店铺、站点和 SKU。"
                       and not (cites or pool or row["encoding_calls"] or row["retrieval_calls"]))
    elif qstatus == "clarify":
        behavior_ok = bool(re.search(r"(?:选择|指定).*SKU", row["reply"])) and not (cites or pool or row["encoding_calls"])
    else:
        terms = q.get("missing_terms", ())
        missing_explained = (all(re.search(pattern, abstention_lead) for pattern in terms) if terms else
                             bool(q.get("missing") and q["missing"] in abstention_lead))
        requires_detail = qstatus == "partial" or q.get("scenario") == "version"
        behavior_ok = abstention_ok
        if requires_detail:
            behavior_ok &= missing_explained
        row["specific_missing_reported"] = missing_explained
    unsupported = qstatus in {"partial", "insufficient"} and not (behavior_ok if conflict else abstention_ok)
    verdict = None
    if not (row["done"] and row["ranking_attested"] and history_ok and scoped):
        verdict = "publication_or_scope"
    elif conflict:
        verdict = None if behavior_ok else "sku_conflict"
    elif qstatus == "supported" and not top20:
        missed = [item for item in ranks if item["merged"] is None]
        if any(not item["indexed"] for item in missed):
            verdict = "incomplete_ingestion"
        elif any(not any(rank is not None for rank in item["channels"].values()) for item in missed):
            verdict = "channel_recall"
        else:
            verdict = "merge_top20"
    elif qstatus == "supported" and not complete:
        if any(not source["ok"] for source in checks):
            verdict = "citation_http"
        elif row["retrieval_trace"].get("citation_ids", []) != cites:
            verdict = "publication_or_scope"
        else:
            verdict = "citation_admission"
    elif not behavior_ok or unsupported:
        verdict = ("citation_admission" if qstatus in {"partial", "insufficient"}
                   and row["retrieval_trace"].get("admission_reason") in {"unsupported_question", "ambiguous_question"}
                   else "answer_content")
    row.update(http_readback=checks, gold_ranks=ranks, final_complete=complete, all_top20=top20,
               history_ok=history_ok, scope_ok=scoped, unsupported_output=unsupported, first_deviation=verdict,
               passed=verdict is None)
    return row

def baseline_groups(q):
    return [[{"document_id": doc, "location": loc}] for doc, loc in q["required"]]


def baseline_summary(rows, saved_baseline):
    answerable = [row for row in rows if row["input"]["expectation"] == "supported"]
    multi = [row for row in answerable if len(row["gold_ranks"]) > 1]
    unknown = [row for row in rows if row["input"]["expectation"] == "insufficient"]
    denied = [row for row in rows if row["input"]["expectation"] == "denied"]
    regressions = []
    for row in answerable:
        old = saved_baseline[row["id"]]["B"]["sources"]
        fields = ("document_id", "location", "version", "quote", "chunk_id")
        previous = {tuple(src[key] for key in fields) for src in old}
        current = {tuple(src[key] for key in fields) for src in row["http_readback"] if src["ok"]}
        if previous - current:
            regressions.append({"id": row["id"], "lost": sorted(previous - current)})
    return {
        "answerable_complete": [sum(row["final_complete"] for row in answerable), len(answerable)],
        "multi_complete": [sum(row["final_complete"] for row in multi), len(multi)],
        "http_readback": [sum(check["ok"] for row in rows for check in row["http_readback"]),
                          sum(len(row["http_readback"]) for row in rows)],
        "no_answer_zero_wrong_citation": [sum(not row["http_readback"] and row["passed"] for row in unknown), len(unknown)],
        "denied_before_encoding": [sum(row["passed"] for row in denied), len(denied)],
        "lost_baseline_sources": regressions,
        "passed": all(row["passed"] for row in rows) and not regressions,
    }


def new_summary(rows, probes):
    result = {}
    for scenario in SCENARIOS:
        batch = [row for row in rows if row["input"]["scenario"] == scenario]
        answerable = [row for row in batch if row["input"]["expectation"] == "supported"]
        needed = [record for row in answerable for record in row["gold_ranks"]]
        multi = [row for row in answerable if len(row["gold_ranks"]) > 1]
        stats = {
            "questions": len(batch),
            "verdict_pass": [sum(row["passed"] for row in batch), len(batch)],
            "complete_citations": [sum(row["final_complete"] for row in answerable), len(answerable)],
            "unsupported_output": sum(row["unsupported_output"] for row in batch),
        }
        for k in (5, 20):
            def hit(record):
                return record["merged"] is not None and record["merged"] <= k
            stats[f"evidence_at_{k}"] = [sum(map(hit, needed)), len(needed)]
            stats[f"multi_all_at_{k}"] = [sum(all(map(hit, row["gold_ranks"])) for row in multi), len(multi)]
        result[scenario] = stats

    checks = [check for row in rows for check in row["http_readback"]]
    recall_misses = [row["id"] for row in rows if row["input"]["expectation"] == "supported" and not row["all_top20"]]
    verification_misses = [row["id"] for row in rows if row["all_top20"] and (
        row["input"]["expectation"] == "supported" and
        row["first_deviation"] in ("citation_admission", "citation_http", "publication_or_scope")
    )]
    failed = {row["id"]: {"stage": row["first_deviation"],
                         "reason": row["retrieval_trace"].get("admission_reason"),
                         "minimum_reproduction": {key: row["input"][key]
                                                  for key in ("principal", "target", "version", "query")},
                         "request_message": row["request"]["message"]}
              for row in rows if not row["passed"]}
    threshold = all(
        total == 0 or count * 10 >= total * 9
        for stats in result.values() for count, total in (stats["evidence_at_20"], stats["complete_citations"])
    )
    abstain_and_isolate = all(row["passed"] for row in rows if row["input"]["expectation"] != "supported")
    integrity = (all(check["ok"] for check in checks) and all(probe["passed"] for probe in probes.values())
                 and all(row["first_deviation"] != "answer_content" or not row["final_complete"] for row in rows)
                 and all(row["done"] and row["ranking_attested"] and row["history_ok"] and row["scope_ok"] for row in rows))
    return {
        "scenarios": result, "http_readback": [sum(check["ok"] for check in checks), len(checks)],
        "top20_missing_questions": recall_misses, "top20_complete_citation_verification_failures": verification_misses,
        "failed": failed, "threshold_passed": threshold, "passed": threshold and abstain_and_isolate and integrity,
    }


def current_pool(conn, uid, target):
    return product_documents.scoped_chunks(conn, uid, rag._scope(target), "structured")


def permission_probes(client, headers, writer, users, run_row, questions):
    trials = read(PACK / "probes.json")
    probes = {}
    for q in trials["denials"]:
        lookup_scope = q["lookup_scope"]
        if lookup_scope:
            rag.activate(writer, lookup_scope, "v1")
        auth = headers[q["principal"]]
        row = run_row(q)
        row["search_http"] = client.post(SEARCH, headers=auth, json={**q["target"], "query": q["query"]}).status_code
        candidates = current_pool(writer, users[q["lookup_principal"]], lookup_scope) if lookup_scope else []
        row["blocked_http"] = {hit["chunk_id"]: client.get(CHUNKS + hit["chunk_id"], headers=auth).status_code
                               for hit in candidates}
        row["passed"] &= (row["search_http"] == 404 and (bool(candidates) or not lookup_scope)
                          and all(code == 404 for code in row["blocked_http"].values()))
        probes[q["id"]] = row

    switch = trials["switch"]
    by_id = {q["id"]: q for q in questions}
    target, principal = switch["target"], switch["principal"]
    auth, uid = headers[principal], users[principal]
    states = {}
    for label, version, query_id in (("before", switch["from"], switch["before"]), ("after", switch["to"], switch["after"])):
        rag.activate(writer, target, version)
        states[label] = run_row(by_id[query_id])
        states[label]["current_chunks"] = [hit["chunk_id"] for hit in current_pool(writer, uid, target)]
    old = {cid: client.get(CHUNKS + cid, headers=auth).status_code for cid in states["before"]["current_chunks"]}
    new = {cid: client.get(CHUNKS + cid, headers=auth).status_code for cid in states["after"]["current_chunks"]}
    probes["version_switch"] = {**states, "old_http": old, "new_http": new,
        "passed": bool(old) and bool(new) and set(old.values()) == {404} and set(new.values()) == {200}
        and all(row["done"] and row["history_ok"] and row["scope_ok"] and row["ranking_attested"]
                and all(hit["ok"] for hit in row["http_readback"]) for row in states.values())}
    return probes


def sku_conflict_regression(rows, previous_path, original_score):
    previous = {row["id"]: row for row in map(json.loads, previous_path.read_text(encoding="utf-8").splitlines())}
    assert len(previous) == len(rows) == original_score[1]
    assert sum(row["passed"] for row in previous.values()) == original_score[0]
    differences = []
    preserved_failures = {}
    conflict_results = {}
    for row in rows:
        qid = row["id"]
        old = previous[qid]
        assert row["input"] == old["input"], qid
        if "sku_conflict_checks" in row:
            conflict_results[qid] = {"passed": row["passed"], "checks": row["sku_conflict_checks"],
                                     "original_passed": old["passed"], "original_stage": old["first_deviation"]}
        elif (any(row[k] != old[k] for k in ("passed", "first_deviation", "reply", "http_readback", "history_ok"))
              or row["retrieval_trace"].get("admission_reason") != old["retrieval_trace"].get("admission_reason")):
            differences.append(qid)
        elif not row["passed"]:
            preserved_failures[qid] = {"stage": old["first_deviation"],
                                      "reason": old["retrieval_trace"].get("admission_reason")}
    return {"original_score": original_score, "current_score": [sum(row["passed"] for row in rows), len(rows)],
            "sku_conflicts": conflict_results, "unexpected_changes": differences,
            "preserved_failures": preserved_failures,
            "previously_passing_still_pass": [sum(row["passed"] and previous[row["id"]]["passed"] for row in rows),
                                              original_score[0]],
            "passed": not differences and all(result["passed"] for result in conflict_results.values())}


def multi_intent_regression(rows, previous_path):
    previous = {row["id"]: row for row in map(json.loads, previous_path.read_text(encoding="utf-8").splitlines())}
    assert len(previous) == len(rows) == 48 and sum(row["passed"] for row in previous.values()) == 29
    preserved_passes, preserved_failures, changes, focus = [], {}, [], {}
    for row in rows:
        qid = row["id"]
        old = previous[qid]
        assert row["input"] == old["input"], qid
        assert (row["ranked"] or {}).get("merged") == (old["ranked"] or {}).get("merged"), qid
        if qid in MULTI_INTENT_FOCUS:
            focus[qid] = {"passed": row["passed"], "checks": row["multi_intent_checks"]}
            continue
        if (any(row[key] != old[key] for key in ("passed", "first_deviation", "reply", "http_readback", "history_ok"))
                or row["retrieval_trace"].get("admission_reason") != old["retrieval_trace"].get("admission_reason")):
            changes.append(qid)
        elif old["passed"]:
            preserved_passes.append(qid)
        else:
            preserved_failures[qid] = {"stage": old["first_deviation"],
                                      "reason": old["retrieval_trace"].get("admission_reason")}
    return {"focus": focus, "preserved_passes": preserved_passes, "preserved_failures": preserved_failures,
            "unexpected_changes": changes, "passed": not changes and len(preserved_passes) == 29
            and all(result["passed"] for result in focus.values())}


def evaluate(mode, output, baseline_path, reviewer=None, previous_path=None, multi_intent_path=None):
    frozen, questions = check_pack()
    model = runtime()
    if mode == "baseline":
        original_questions, question_sha256, source_hashes = check_inputs("frozen")
        expected = {row["id"]: row for row in read(baseline_path)}
        assert len(expected) == 54
        for q in original_questions:
            assert all(expected[q["id"]][key] == value for key, value in q.items())
        questions = [{**q, "expectation": ("denied" if not q["access"] else
                                          "supported" if q["has_answer"] else "insufficient")}
                     for q in original_questions]
        pack_path = FIXTURES / "manifest.json"
        frozen = {"questions_sha256": question_sha256, "sources_sha256": source_hashes,
                  "previous_sse_sha256": digest(baseline_path), "model": frozen["model"], "settings": frozen["settings"]}
    else:
        pack_path = PACK / "manifest.json"

    contract_path = PACK / "sku-conflict-r1.json"
    contract = read(contract_path) if previous_path or multi_intent_path else None
    if contract:
        assert contract["freeze_sha256"] == digest(PACK / "freeze.json")
    output.mkdir(parents=True)
    if mode == "heldout":
        write_json(output / f"{reviewer}-review.json",
                   {"confirmed": True, "reviewer": reviewer, "freeze_sha256": digest(PACK / "freeze.json")})
    write_json(output / "run-config.json", {"inputs": frozen, "runtime": model, **(
        {"sku_conflict_contract_sha256": digest(contract_path),
         "previous_rows_sha256": digest(previous_path or multi_intent_path)}
        if contract else {})})
    use_sandbox(output / "evaluation.sqlite3")
    database.init_db()
    database.settings.JWT_SECRET = token_urlsafe(48)
    from fast_server import app

    audit = RetrievalAudit()
    originals = {p.name: p.read_bytes().decode("utf-8") for p in (pack_path.parent / "sources").iterdir()}
    with (
        closing(database.get_db()) as writer,
        patch.object(observability, "TRACE_LOG_FILE", output / "trace.jsonl"),
        patch.object(rag.ProductEvidence, "retrieve", lambda service, *a, **kw: audit.capture(service, *a, **kw)),
        patch.object(rag.BGEEmbedding, "embed_query", lambda encoder, texts: audit.count(encoder, texts)),
    ):
        users, builds = import_pack(writer, pack_path, seed=True)
        assert all(row["inserted"] for row in builds)
        write_json(output / "import.json", builds)
        with loopback(app, lifespan="off") as client, (output / "rows.jsonl").open("w", encoding="utf-8") as journal:
            traces = TraceReader(observability.TRACE_LOG_FILE)
            headers = {principal: login(client, principal) for principal in users}
            def run_row(q):
                uid = users[q["principal"]]
                auth = headers[q["principal"]]
                case = contract["cases"].get(q["id"]) if contract else None
                focus = multi_intent_path and q["id"] in MULTI_INTENT_FOCUS
                row = sse(client, auth, q, traces, writer, uid, audit, refresh_history=True, capture_wire=bool(focus))
                checks = readback(client, auth, row, originals, capture_wire=bool(focus))
                score(row, checks, baseline_groups(q) if mode == "baseline" else q.get("required", []),
                      conflict=(case, contract["acceptance"]) if case else None)
                if focus:
                    row["multi_intent_checks"] = focused_checks(row)
                    row["passed"] &= all(row["multi_intent_checks"].values())
                elif mode == "baseline":
                    refreshed = [(entry["role"], entry["content"]) for entry in row["refreshed_history"][-2:]]
                    row["refresh_matches_commit"] = refreshed == [
                        (entry["role"], entry["content"]) for entry in row["committed_history"]
                    ]
                    row["passed"] &= row["refresh_matches_commit"]
                return row

            rows = []
            for q in questions:
                if q["target"] and q["version"]:
                    rag.activate(writer, q["target"], q["version"])
                row = run_row(q)
                rows.append(row)
                journal.write(json.dumps(row, ensure_ascii=False) + "\n")
                journal.flush()
                print(q["id"], row["first_deviation"] or "pass", flush=True)

            probes = permission_probes(client, headers, writer, users, run_row, questions) if mode == "heldout" else {}
            if probes:
                write_json(output / "probes.json", probes)

    summary = baseline_summary(rows, expected) if mode == "baseline" else new_summary(rows, probes)
    if previous_path:
        summary["sku_conflict_regression"] = sku_conflict_regression(rows, previous_path, contract["original_score"])
    if multi_intent_path:
        summary["multi_intent_regression"] = multi_intent_regression(rows, multi_intent_path)
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if contract:
        regression = "multi_intent_regression" if multi_intent_path else "sku_conflict_regression"
        return int(not (summary[regression]["passed"] and all(p["passed"] for p in probes.values())))
    return int(not summary["passed"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "baseline", "heldout"))
    parser.add_argument("--output", type=Path)
    review = parser.add_mutually_exclusive_group()
    review.add_argument("--human-reviewed", dest="reviewer", action="store_const", const="human",
                        help="Record human verification of the frozen annotations")
    review.add_argument("--ai-reviewed", dest="reviewer", action="store_const", const="ai",
                        help="Record AI verification of the frozen annotations")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    regressions = parser.add_mutually_exclusive_group()
    regressions.add_argument("--sku-conflict-regression", type=Path, metavar="FIRST_RUN_ROWS",
                             help="Check the SKU clarification contract and compare against the archived 48 rows")
    regressions.add_argument("--multi-intent-regression", type=Path, metavar="R1_ROWS",
                             help="Check multi-intent admission and compare all 48 rows to R1")
    args = parser.parse_args()
    if args.mode == "check":
        check_pack()
        print(f"48 held-out inputs verified without inference. Freeze SHA-256 {digest(PACK / 'freeze.json')}")
        return 0
    if args.output is None:
        parser.error("baseline and heldout require --output")
    if args.mode == "heldout" and not args.reviewer:
        parser.error("Record the annotation review with --human-reviewed or --ai-reviewed")
    if (args.sku_conflict_regression or args.multi_intent_regression) and args.mode != "heldout":
        parser.error("The held-out regression comparisons require heldout mode")
    return evaluate(args.mode, args.output.resolve(), args.baseline, args.reviewer,
                    args.sku_conflict_regression, args.multi_intent_regression)


if __name__ == "__main__":
    raise SystemExit(main())
