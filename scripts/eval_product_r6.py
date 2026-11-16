"""Replay TASK-044-R6 through the authenticated SSE and original-source endpoints."""

import argparse
import json
import os
from contextlib import closing, contextmanager
from hashlib import sha256
from itertools import permutations
from pathlib import Path
from secrets import token_urlsafe
from time import perf_counter
from unittest.mock import patch

import httpx

from backend import observability
from backend.database import chat_groups, chat_storage, database
from backend.rag import product_chat_search, product_evidence as rag
from backend.rag.product_rerank_model import REVISION, WEIGHT_SHA256, Reranker
from scripts.eval_product_chat_rerank import CITATION, TraceReader
from scripts.eval_product_query_expansion_r4 import read_scope
from scripts.eval_product_r5 import freeze, history, loopback, request, timeouts
from scripts.eval_product_rerank import copy_catalog
from scripts.product_evidence import FIXTURES, p95, rank_locations, write_json
from scripts.verify_product_evidence import CHUNKS, login

GROUPS = ("A", "B", "C")
REPETITIONS = 5
MISSING = {"Q11": ("specs", "row:4"), "Q12": ("specs", "row:5"), "Q13": ("manual", "page:6"),
           "Q14": ("manual", "page:6"), "Q16": ("manual", "page:6"), "Q40": ("specs", "row:5")}
FOCUS = {*MISSING, "Q21", "Q26", "Q29", "Q39"}


@contextmanager
def group_settings(group):
    with patch.multiple(database.settings, PRODUCT_R4_CPU_ENABLED=group == "B",
                        PRODUCT_RERANK_ENABLED=group == "C"):
        yield


class Meter:
    def __init__(self):
        self.encodes = 0
        self.queue_accesses = 0
        self.loads = 0
        self.model = None
        self.embed = rag.BGEEmbedding.embed_query
        self.get_queue = product_chat_search.get_queue

    def encode(self, embedding, texts):
        assert embedding._device == "cpu"
        result = self.embed(embedding, texts)
        self.encodes += len(texts)
        return result

    def queue(self):
        self.queue_accesses += 1
        return self.get_queue()

    def load(self):
        self.loads += 1
        self.model = Reranker()
        return self.model

    def counts(self):
        return {"encoding_calls": self.encodes, "gpu_queue_accesses": self.queue_accesses,
                "gpu_model_loads": self.loads, "gpu_score_calls": self.model.calls if self.model else 0}


def append(file, row):
    file.write(json.dumps(row, ensure_ascii=False) + "\n")
    file.flush()


def candidates(trace):
    return trace.get("candidate_ids", trace.get("default_retrieval", {}).get("candidate_ids", []))


def sample(client, headers, q, traces, meter, group, raw, repeat, order, *, allowed):
    before = meter.counts()
    start = perf_counter()
    stamp = {"id": q["id"], "group": group, "repeat": repeat, "order": order}
    try:
        with group_settings(group):
            row = request(client, headers, q)
    except httpx.TimeoutException as error:
        append(raw, {**stamp, "timeout": type(error).__name__, "elapsed_ms": (perf_counter() - start) * 1000})
        raise
    trace = traces.read(row["trace_id"])
    after = meter.counts()
    usage = {key: value - before[key] for key, value in after.items()}
    ids = candidates(trace)
    cites = CITATION.findall(row["reply"])
    timing = {"done_ms": row["elapsed_ms"], "retrieval_ms": trace.get("retrieval_ms",
               trace.get("default_retrieval", {}).get("ms")), "gpu_ms": trace.get("inference_ms"),
              "queue_wait_ms": trace.get("queue_wait_ms"), "fallback_reason": trace.get("fallback_reason")}
    append(raw, {**stamp, **timing, **usage, "trace_id": row["trace_id"], "path": trace["path"],
                 "candidate_ids": ids, "citation_ids": cites, "timeout": trace.get("fallback_reason")
                 if trace.get("fallback_reason") in {"inference_timeout", "queue_timeout"} else None})
    assert cites == trace.get("citation_ids", []), (stamp, trace, cites)
    assert len(set(ids)) == len(ids) and set(ids) <= allowed and set(cites) <= set(ids), (stamp, ids, cites)
    assert not usage["gpu_model_loads"], (stamp, usage)
    if not q["access"]:
        assert not ids and not cites and trace["status"] == "denied" and not any(usage.values()), (stamp, trace, usage)
    else:
        assert usage["encoding_calls"] > 0 and timing["retrieval_ms"] is not None, (stamp, trace, usage)
        if group in ("B", "C") and "candidate_chunk_ids" in q and ids != q["candidate_chunk_ids"]:
            raise AssertionError({"ordered_r4_drift": stamp, "expected": q["candidate_chunk_ids"], "actual": ids,
                                  "trace_id": row["trace_id"]})
        if group == "C":
            assert usage["gpu_queue_accesses"] == usage["gpu_score_calls"] == 1, (stamp, usage, trace)
            assert trace["path"] in {"rerank_top5", "rerank_fill"} and timing["gpu_ms"] is not None, (stamp, trace)
            assert trace["scored_chunk_ids"] == ids and sorted(trace["ranked_ids"]) == sorted(ids), (stamp, trace)
        else:
            assert not any(value for key, value in usage.items() if key.startswith("gpu_")), (stamp, usage)
            assert trace["path"] == ("r4_cpu" if group == "B" else "default"), (stamp, trace)
            assert trace["scored_chunks"] == 0 and timing["gpu_ms"] is None, (stamp, trace)
    return {**row, "trace": trace, "usage": usage, "timing": timing}


def readback(client, headers, q, reply, chunks, raw, group):
    sources = []
    previous = 0
    for entry in CITATION.finditer(reply):
        key = entry[1]
        start = perf_counter()
        response = client.get(CHUNKS + key, headers=headers)
        elapsed = (perf_counter() - start) * 1000
        append(raw, {"id": q["id"], "group": group, "chunk_id": key, "status": response.status_code,
                     "elapsed_ms": elapsed})
        hit = response.raise_for_status().json()
        assert hit["chunk_id"] == key and hit["version"] == q["version"], (q["id"], key)
        assert all(hit[k] == v for k, v in q["target"].items()), (q["id"], key, hit)
        original = (FIXTURES / "sources" / hit["filename"]).read_bytes()
        original_hash = sha256(original).hexdigest()
        assert hit["original"] == original.decode("utf-8")
        assert hit["content"] == hit["original"][hit["start_offset"]:hit["end_offset"]]
        if key in chunks:
            expected = chunks[key]
            assert original_hash == expected["original_sha256"]
            assert all(hit[field] == expected[field] for field in (
                "content", "locations", "start_offset", "end_offset", "version", "document_id", "filename",
            )), (q["id"], key)
        block = reply[previous:entry.start()]
        previous = entry.end()
        location, = [loc for loc in hit["locations"] if loc["complete"] and
                     f"{hit['filename']} · {loc['location']} · {loc['section']}\n" in block]
        quote = hit["original"][location["start_offset"]:location["end_offset"]]
        assert quote and block.endswith(f"\n{quote}\n原文回查（需登录） "), (q["id"], key)
        sources.append({**location, "chunk_id": key, "quote": quote, "document_id": hit["document_id"],
                        "filename": hit["filename"], "version": hit["version"], "target": q["target"],
                        "chunk_start_offset": hit["start_offset"], "chunk_end_offset": hit["end_offset"],
                        "original_sha256": original_hash, "http": response.status_code, "http_ms": elapsed})
    return sources


def refs(sources):
    return {(source["document_id"], source["location"]): source["quote"] for source in sources}


def check_history(writer, uid, row):
    saved = history(writer, uid, row["chat_view_id"])
    assert [entry["role"] for entry in saved] == ["user", "assistant"], (row["id"], saved)
    receipt = saved[-1]["content"]
    assert CITATION.findall(receipt) == CITATION.findall(row["reply"]), (row["id"], saved, row["reply"])
    if row["sources"]:
        expected = [f"{row['sources'][0]['target']['sku']} · {row['sources'][0]['version']}"]
        for source in row["sources"]:
            label = f"{source['filename']} · {source['location']} · {source['section']}"
            expected.append(f"{label}\n原文回查（需登录） {CHUNKS}{source['chunk_id']}")
        assert receipt == "\n\n".join(expected), (row["id"], receipt)
    else:
        assert receipt == row["reply"], (row["id"], receipt)
    return saved


def inspect(client, headers, writer, uid, q, row, chunks, http_raw, group):
    row["sources"] = readback(client, headers[q["principal"]], q, row["reply"], chunks, http_raw, group)
    actual = refs(row["sources"])
    assert len(actual) == len(row["sources"])
    row["complete"] = bool(q["required"]) and set(actual) == set(map(tuple, q["required"]))
    assert not set(actual) - set(map(tuple, q["required"])), (q["id"], group, actual)
    row["history"] = check_history(writer, uid, row)


def replay(client, headers, writer, users, questions, chunks, baseline, traces, meter, output, raw, http_raw):
    rows, pairs = {}, []
    orders = tuple(permutations(GROUPS))
    for index, q in enumerate(questions):
        key, uid = q["id"], users[q["principal"]]
        rag.activate(writer, q["target"], q["version"])
        complete, allowed = read_scope(writer, uid, q)
        assert q["access"] == bool(allowed), (key, allowed)
        first = {}
        order = orders[index % len(orders)]
        for group in order:
            row = sample(client, headers, q, traces, meter, group, raw, 0, order, allowed=allowed)
            inspect(client, headers, writer, uid, q, row, chunks, http_raw, group)
            first[group] = row
        a, b, c = (first[group] for group in GROUPS)
        assert a["reply"] == baseline[key]["off"]["reply"] and candidates(a["trace"]) == candidates(
            baseline[key]["off"]["trace"]), (key, "R5 default drift", a["trace"])
        assert set(refs(a["sources"]).items()) <= set(refs(b["sources"]).items()), (key, "CPU regression")
        comparisons = []
        a_ids = candidates(a["trace"])
        b_ids = candidates(b["trace"])
        for ref in map(tuple, q["required"]):
            eligible = complete[ref]
            a_rank = next((i for i, cid in enumerate(a_ids, 1) if cid in eligible), None)
            b_rank = rank_locations([chunks[cid] for cid in b_ids], ref, complete=True)
            c_rank = rank_locations([chunks[cid] for cid in c["trace"]["ranked_ids"]], ref, complete=True)
            quote = refs(b["sources"]).get(ref)
            comparisons.append({"source": ref, "A_candidate_rank": a_rank, "B_candidate_rank": b_rank,
                                "C_candidate_rank": c_rank, "B_first_complete_chunk_id": b_ids[b_rank - 1]
                                if b_rank else None, "original": quote, "A_finally_cited": ref in refs(a["sources"])})
        rows[key] = {**q, **first, "required_evidence": comparisons}
        write_json(output / "questions.json", list(rows.values()))
        if key in FOCUS:
            write_json(output / f"{key}.json", rows[key])
        if key in MISSING:
            assert [tuple(ref["source"]) for ref in comparisons if ref["A_candidate_rank"] is None] == [
                MISSING[key]], (key, comparisons)
            assert not a["complete"] and b["complete"] and c["complete"], (key, first)
        if not q["access"]:
            continue
        for repeat in range(1, REPETITIONS + 1):
            order = orders[(index + repeat) % len(orders)]
            pair = {"id": key, "repeat": repeat, "order": order}
            for group in order:
                row = sample(client, headers, q, traces, meter, group, raw, repeat, order, allowed=allowed)
                assert row["reply"] == first[group]["reply"], (key, group, repeat, "SSE drift")
                assert row["trace"].get("ranked_ids") == first[group]["trace"].get("ranked_ids"), (key, group, repeat)
                pair[group] = row["timing"]
            pair["B_minus_A_ms"] = pair["B"]["done_ms"] - pair["A"]["done_ms"]
            pair["C_minus_B_ms"] = pair["C"]["done_ms"] - pair["B"]["done_ms"]
            pairs.append(pair)
        if (index + 1) % 10 == 0:
            print(f"Completed real chat replay and measurements through {key}", flush=True)
    return rows, pairs


def denied_site(client, headers, base, traces, meter, raw, output):
    query = {**base, "id": "wrong_site", "access": False,
             "target": {**base["target"], "marketplace_id": "GB"}}
    query.pop("candidate_chunk_ids")
    rows = {group: sample(client, headers, query, traces, meter, group, raw, "scope", GROUPS, allowed=set())
            for group in GROUPS}
    write_json(output / "wrong-site.json", rows)


def scope_probes(client, headers, writer, users, query, traces, chunks, output, http_raw, *,
                 groups=GROUPS, configure=group_settings):
    rows = []
    target = query["target"]
    owner, other = users["owner"], users["other"]
    retrieve = rag.ProductEvidence.retrieve
    save_message = chat_storage.save_message
    save_group = chat_groups.GroupRun.save_reply

    def transfer(uid):
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                         (uid, target["shop_id"], target["marketplace_id"]))

    for group in groups:
        old_ids = CITATION.findall(query[group]["reply"])
        assert old_ids and query["version"] == "v1"
        for boundary, grouped in (("after_retrieval", False), ("before_commit", False), ("before_commit", True)):
            for change in ("version", "transfer", "session"):
                headers["owner"] = login(client, "owner")
                rag.activate(writer, target, "v1")
                conversation = None
                if grouped:
                    created = client.post("/api/chat/conversations", headers=headers["owner"], json={"title": "R6 权限联测"})
                    conversation = created.raise_for_status().json()["data"]["conversation_id"]
                observed = []

                def mutate():
                    assert not observed
                    observed.append(change)
                    if change == "transfer":
                        transfer(other)
                    else:
                        with closing(database.get_db()) as conn, conn:
                            if change == "version":
                                rag.activate(conn, target, "v2")
                            else:
                                conn.execute("DELETE FROM sessions WHERE user_id=?", (owner,))

                def after_retrieval(service, *args, **kwargs):
                    result, release_rows = retrieve(service, *args, **kwargs)
                    if not observed:
                        assert result["version"] == "v1", result
                        assert set(old_ids) <= set(product_chat_search.chunk_ids(result["candidates"]))
                        if group in ("B", "C"):
                            assert product_chat_search.chunk_ids(result["candidates"]) == query["candidate_chunk_ids"]
                        mutate()
                    return result, release_rows

                def before_message(*args, **kwargs):
                    if kwargs.get("validate") is not None:
                        mutate()
                    return save_message(*args, **kwargs)

                def before_group(*args, **kwargs):
                    if kwargs.get("validate") is not None:
                        mutate()
                    return save_group(*args, **kwargs)

                try:
                    patch_point = (patch.object(rag.ProductEvidence, "retrieve", after_retrieval)
                                   if boundary == "after_retrieval" else
                                   patch.object(chat_groups.GroupRun, "save_reply", before_group) if grouped else
                                   patch.object(chat_storage, "save_message", before_message))
                    with configure(group), patch_point:
                        terminal = "error" if boundary == "before_commit" else "done"
                        row = request(client, headers, query, group=conversation, terminal=terminal)
                    assert observed == [change]
                    if terminal == "error":
                        assert row["events"][-1]["error_code"] == "product_evidence_scope_changed" and not row["reply"]
                    if grouped:
                        persisted = [dict(r) for r in writer.execute(
                            "SELECT content FROM chat_group_entries WHERE kind='assistant' AND run_id=?",
                            (row["events"][-1]["run_id"],),
                        )]
                    else:
                        persisted = [r for r in history(writer, owner, row["chat_view_id"]) if r["role"] == "assistant"]
                    assert not any(cid in json.dumps([row["events"], persisted], ensure_ascii=False) for cid in old_ids)
                    if terminal == "error":
                        assert not persisted
                    old_http = {key: client.get(CHUNKS + key, headers=headers["owner"]).status_code for key in old_ids}
                    assert set(old_http.values()) == {401 if change == "session" else 404}, old_http
                    recovery = None
                    if change == "version":
                        v2 = {**query, "id": f"{query['id']}_scope_v2", "version": "v2"}
                        # Some paths can recover within the original request; verify each emitted reference independently.
                        if CITATION.search(row["reply"]):
                            row["sources"] = readback(client, headers["owner"], v2, row["reply"], chunks, http_raw, group)
                            assert persisted == check_history(writer, owner, row)[-1:]
                        with configure(group):
                            recovery = request(client, headers, v2)
                        recovery["sources"] = readback(client, headers["owner"], v2, recovery["reply"], chunks, http_raw, group)
                        assert recovery["sources"] and not set(old_ids) & set(CITATION.findall(recovery["reply"]))
                        recovery["history"] = check_history(writer, owner, recovery)
                    elif change == "transfer":
                        new_owner = {key: client.get(CHUNKS + key, headers=headers["other"]).status_code for key in old_ids}
                        assert set(new_owner.values()) == {200}, new_owner
                        recovery = {"transferred_owner_http": new_owner}
                    rows.append({"group": group, "boundary": boundary, "grouped": grouped, "change": change,
                                 "old_chunk_ids": old_ids, "old_http": old_http, "assistant_history": persisted,
                                 "sse": row, "trace": traces.read(row["trace_id"], full=True), "recovery": recovery})
                    write_json(output / "scope.json", rows)
                finally:
                    if change == "transfer":
                        transfer(owner)
    return rows


def summarize(rows, pairs, scopes, faults):
    answerable = [q for q in rows.values() if q["has_answer"]]
    multi = [q for q in answerable if len(q["required"]) > 1]
    unanswered = [q for q in rows.values() if q["access"] and not q["has_answer"]]
    counts = {group: [sum(q[group]["complete"] for q in answerable), len(answerable)] for group in GROUPS}
    multi_counts = {group: [sum(q[group]["complete"] for q in multi), len(multi)] for group in GROUPS}
    recovered = [q["id"] for q in answerable if not q["A"]["complete"] and q["B"]["complete"]]
    assert counts == {"A": [38, 44], "B": [44, 44], "C": [44, 44]}, counts
    assert multi_counts == {"A": [4, 7], "B": [7, 7], "C": [7, 7]} and set(recovered) == set(MISSING)
    assert len(unanswered) == 8 and all(not q[group]["sources"] for q in unanswered for group in GROUPS)
    assert len(pairs) == 52 * REPETITIONS and len(scopes) == 27 and len(faults) == len(MISSING)
    assert all(len(rows[qid]["required_evidence"]) == len(rows[qid]["B"]["sources"]) for qid in FOCUS)
    for (qid, doc, loc), terms in {
        ("Q21", "faq", "faq:share"): ("exact SKU", "Appearance alone is insufficient"),
        ("Q21", "specs", "row:7"): ("LID-M750", "B42"),
        ("Q26", "manual", "page:10"): ("Never heat any part in a microwave or oven",),
        ("Q29", "faq", "faq:seal"): ("密封圈是否扭曲", "用静水试漏"),
        ("Q29", "manual", "page:13"): ("flat side facing the cap", "raised edge facing the bottle"),
        ("Q39", "specs", "row:3"): ("76\tmm",),
        ("Q39", "specs", "row:4"): ("255\tmm",),
        ("Q39", "manual", "page:15"): ("narrowest point", "lift the bottle straight out"),
    }.items():
        assert all(term in refs(rows[qid]["B"]["sources"])[doc, loc] for term in terms), (qid, doc, loc)

    latency = {}
    for group in GROUPS:
        latency[group] = {metric: p95(values) if (values := [pair[group][metric] for pair in pairs
                                                            if pair[group][metric] is not None]) else None
                          for metric in ("retrieval_ms", "gpu_ms", "done_ms")}
        latency[group]["http_ms"] = p95([cite["http_ms"] for q in rows.values() for cite in q[group]["sources"]])
    deltas = {key: p95([pair[key] for pair in pairs]) for key in ("B_minus_A_ms", "C_minus_B_ms")}
    a_supported = [q for q in answerable if q["A"]["complete"]]
    return {"passed": True, "answerable_complete": counts, "multi_complete": multi_counts, "recovered": recovered,
            "A_supported_retained_in_B": sum(refs(q["A"]["sources"]) == refs(q["B"]["sources"]) for q in a_supported),
            "B_C_source_differences": [q["id"] for q in rows.values() if refs(q["B"]["sources"]) != refs(q["C"]["sources"])],
            "B_C_chunk_id_differences": [q["id"] for q in rows.values() if
                                        CITATION.findall(q["B"]["reply"]) != CITATION.findall(q["C"]["reply"])],
            "top_5_required_sources": {
                group: sum(0 < (e[f"{group}_candidate_rank"] or 21) <= 5
                           for q in answerable for e in q["required_evidence"]) for group in ("B", "C")},
            "no_answer_false_citations": {group: [0, len(unanswered)] for group in GROUPS},
            "http_citations_verified": {group: sum(len(q[group]["sources"]) for q in rows.values()) for group in GROUPS},
            "candidate_scope_violations": 0, "ordered_r4_drift": 0, "gpu_loads_and_scores_A_B": 0,
            "denied_before_encoding_and_scoring": [q["id"] for q in rows.values() if not q["access"]],
            "scope_changes_passed": len(scopes), "wrong_site_probes_passed": len(GROUPS),
            "paired_samples": len(pairs), "p95_ms": latency, "paired_added_p95_ms": deltas,
            "timed_timeouts": {group: sum(pair[group]["fallback_reason"] in {"queue_timeout", "inference_timeout"}
                                           for pair in pairs) for group in GROUPS},
            "induced_gpu_timeout_probes_passed": len(faults),
            "induced_timeout_done_p95_ms": p95([row["elapsed_ms"] for row in faults.values()])}


def evaluate(run, r5, output):
    output.mkdir(parents=True)
    questions, chunks = freeze(run, output)
    baseline = {q["id"]: q for q in json.loads(r5.read_bytes())}
    baseline_hash = sha256(r5.read_bytes()).hexdigest()
    copy_catalog(run / "evaluation.sqlite3", output / "evaluation.sqlite3")
    with patch.object(observability, "TRACE_LOG_FILE", output / "traces.jsonl"), \
            patch.object(observability, "PRODUCT_RERANK_LATE_FILE", output / "late.jsonl"), \
            patch.object(database.settings, "JWT_SECRET", token_urlsafe(48)):
        from fast_server import app

        traces = TraceReader(observability.TRACE_LOG_FILE)
        late = TraceReader(observability.PRODUCT_RERANK_LATE_FILE)
        with loopback(app) as client, closing(database.get_db()) as writer, \
                (output / "samples.jsonl").open("w", encoding="utf-8") as raw, \
                (output / "http.jsonl").open("w", encoding="utf-8") as http_raw:
            users = {p: writer.execute("SELECT id FROM users WHERE username=?", (f"product-evidence-{p}",)).fetchone()[0]
                     for p in ("owner", "other")}
            headers = {p: login(client, p) for p in users}
            plan = {"groups": GROUPS, "concurrency": 1, "repetitions": REPETITIONS, "r5_sha256": baseline_hash,
                    "queue_size": 3, "queue_timeout_ms": 400, "inference_timeout_ms": 700,
                    "allowlist": list(users.values()), "embedding": rag.BGE_ID, "embedding_device": "cpu"}
            write_json(output / "plan.json", plan)
            meter = Meter()
            with patch.multiple(database.settings, PRODUCT_RERANK_USER_IDS=frozenset(users.values()),
                                PRODUCT_RERANK_MAX_CONCURRENCY=plan["concurrency"],
                                PRODUCT_RERANK_QUEUE_SIZE=plan["queue_size"],
                                PRODUCT_RERANK_QUEUE_TIMEOUT_MS=plan["queue_timeout_ms"],
                                PRODUCT_RERANK_INFERENCE_TIMEOUT_MS=plan["inference_timeout_ms"]), \
                    patch.object(rag.BGEEmbedding, "embed_query", lambda embed, texts: meter.encode(embed, texts)):
                # Load once in the gray group so measured requests use the production inference timeout.
                with group_settings("C"):
                    queue = product_chat_search.get_queue()
                    queue.factory = meter.load
                    model = queue._model()
                assert meter.loads == 1 and model.metadata["device"] == "cuda:0"
                assert model.metadata["revision"] == REVISION and model.metadata["weight_sha256"] == WEIGHT_SHA256
                write_json(output / "model.json", model.metadata)
                with patch.object(product_chat_search, "get_queue", meter.queue):
                    rows, pairs = replay(client, headers, writer, users, questions, chunks, baseline, traces, meter,
                                         output, raw, http_raw)
                    write_json(output / "pairs.json", pairs)
                    denied_site(client, headers, rows["Q26"], traces, meter, raw, output)
                    scopes = scope_probes(client, headers, writer, users, rows["Q26"], traces, chunks, output, http_raw)
                    headers["owner"] = login(client, "owner")
                    focused = [{**rows[key], "off": rows[key]["A"]} for key in MISSING]
                    faults = timeouts(client, headers, writer, users, model, focused, traces, late, chunks, output)
                summary = summarize(rows, pairs, scopes, faults)
                write_json(output / "summary.json", summary)
                print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r4-run", required=True, type=Path)
    parser.add_argument("--r5-questions", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if os.environ.get("AD_OFFLINE_ACCEPTANCE") != "1" or os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        parser.error("Set AD_OFFLINE_ACCEPTANCE=1 and PYTHON_DOTENV_DISABLED=1 before replay")
    os.environ.update(LLM_PROVIDER="mock", DIAGNOSIS_MYSQL_ENABLED="0", PRODUCT_RERANK_ENABLED="0",
                      PRODUCT_R4_CPU_ENABLED="0")
    database.settings.LLM_PROVIDER = "mock"
    database.settings.PRODUCT_RERANK_ENABLED = database.settings.PRODUCT_R4_CPU_ENABLED = False
    evaluate(args.r4_run.resolve(), args.r5_questions.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
