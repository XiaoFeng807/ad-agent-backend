"""Replay TASK-044-R5 through the authenticated FastAPI service and pinned CUDA model."""

import argparse
import json
import os
import socket
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, Thread
from time import monotonic, perf_counter, sleep
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn

from backend import observability
from backend.agent import product_evidence_reply
from backend.database import database
from backend.rag import product_chat_search, product_evidence as rag
from backend.rag.product_rerank_model import REVISION, WEIGHT_SHA256, Reranker
from scripts.eval_product_chat_rerank import CITATION, TraceReader, read_sources
from scripts.eval_product_query_expansion_r4 import TARGETS, check_inputs, digest
from scripts.eval_product_rerank import check_scope, copy_catalog, reorder
from scripts.product_evidence import FIXTURES, p95, rank_locations, write_json
from scripts.verify_product_evidence import CHUNKS, login

FOCUS = {"Q13", "Q14", "Q21", "Q26", "Q29", "Q39"}
ADDED_P95_LIMIT_MS = 75
REPETITIONS = 5
TERMINALS = {"done", "error", "stopped", "superseded"}


@contextmanager
def loopback(app, *, lifespan="auto"):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen()
        server = uvicorn.Server(uvicorn.Config(app, lifespan=lifespan, log_level="info", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=35, trust_env=False) as client:
                deadline = monotonic() + 30
                while not server.started and thread.is_alive() and monotonic() < deadline:
                    sleep(.05)
                assert server.started, "Loopback FastAPI failed to start"
                yield client
        finally:
            server.should_exit = True
            thread.join(15)
            assert not thread.is_alive(), "Loopback FastAPI failed to stop"


def freeze(run, output):
    questions, question_hash, source_hashes = check_inputs("frozen")
    audit = json.loads((run / "summary.json").read_bytes())
    assert audit["passed"] and audit["input_sha256"] == question_hash and audit["sources_sha256"] == source_hashes
    rows = json.loads((run / "r4.json").read_bytes())
    originals = json.loads((run / "originals.json").read_bytes())
    chunks = {}
    for question, row in zip(questions, rows, strict=True):
        key = question["id"]
        assert all(row[k] == v for k, v in question.items()), key
        ids = row["candidate_chunk_ids"]
        assert len(ids) == len(set(ids)) == (20 if question["access"] else 0), key
        hits = [originals[chunk_id] for chunk_id in ids]
        assert check_scope(hits, question["target"], question["version"]) == 0, key
        assert all(hit["ok"] for hit in hits), key
        chunks.update((hit["chunk_id"], hit) for hit in hits)
        question["candidate_chunk_ids"] = ids
    assert len(questions) == 54 and sum(q["access"] for q in questions) == 52
    path = output / "frozen-candidates.json"
    write_json(path, {"questions": questions, "chunks": chunks})
    write_json(output / "freeze.json", {
        "frozen_candidates_sha256": digest(path), "frozen_questions_sha256": question_hash,
        "source_sha256": source_hashes, "r4_audit": str(run),
        "r4_artifact_sha256": {name: digest(run / name) for name in ("summary.json", "r4.json", "originals.json")},
    })
    return questions, chunks


def request(client, headers, q, *, group=None, terminal="done"):
    view_id = uuid4().hex
    target = q["target"]
    body = {"message": "商品资料：" + " ".join([*rag._scope(target), q["query"]]), "chat_view_id": view_id}
    if group:
        body["conversation_id"] = group
    events = []
    start = perf_counter()
    with client.stream("POST", "/api/chat/stream", headers=headers[q["principal"]], json=body) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event["type"] in TERMINALS:
                    end = perf_counter()
    assert [event for event in events if event["type"] in TERMINALS] == [events[-1]], (q["id"], events)
    assert events[-1]["type"] == terminal, (q["id"], events)
    reply = "".join(event["content"] for event in events if event["type"] == "text")
    return {"id": q["id"], "reply": reply, "events": events, "trace_id": events[-1]["trace_id"],
            "elapsed_ms": (end - start) * 1000, "chat_view_id": view_id}


def readback(client, auth, row, q, originals):
    sources = read_sources(client, auth, row, q)
    for source in sources:
        key = source["chunk_id"]
        result = client.get(CHUNKS + key, headers=auth).raise_for_status().json()
        original_hash = sha256(result["original"].encode()).hexdigest()
        assert original_hash == digest(FIXTURES / "sources" / result["filename"]), (q["id"], key)
        if key in originals:
            expected = originals[key]
            assert original_hash == expected["original_sha256"]
            assert all(result[field] == expected[field] for field in (
                "content", "locations", "start_offset", "end_offset", "version", "document_id", "filename",
            )), (q["id"], key)
        source.update(version=result["version"], original_sha256=original_hash, target=q["target"])
    return sources


def rank_refs(q, chunks, ids):
    hits = [chunks[key] for key in ids]
    return [rank_locations(hits, ref, complete=True) for ref in q["required"]]


def sample(client, headers, q, traces, enabled, *, full=False):
    with patch.object(database.settings, "PRODUCT_RERANK_ENABLED", enabled):
        row = request(client, headers, q)
    trace = traces.read(row["trace_id"])
    assert trace["eligible"] == enabled, row
    assert CITATION.findall(row["reply"]) == trace.get("citation_ids", []), row
    if q["access"] and enabled:
        frozen = q["candidate_chunk_ids"]
        assert trace["candidate_ids"] == trace["scored_chunk_ids"] == frozen, (q["id"], trace)
        assert trace["scored_chunks"] == len(frozen) and sorted(trace["ranked_ids"]) == sorted(frozen), (q["id"], trace)
    else:
        assert trace["scored_chunks"] == 0, (q["id"], trace)
        if not q["access"]:
            assert trace["status"] == "denied" and not trace.get("candidate_ids"), trace
    row["trace"] = trace
    if full:
        row["full_trace"] = traces.read(row["trace_id"], full=True)
    return row


def paired_chat(client, headers, writer, questions, chunks, traces, output, offline):
    rows, pairs = {}, []
    with (output / "samples.jsonl").open("w", encoding="utf-8") as raw:
        for index, q in enumerate(questions):
            key = q["id"]
            rag.activate(writer, q["target"], q["version"])
            first = {mode: sample(client, headers, q, traces, mode == "on", full=key in FOCUS)
                     for mode in ("off", "on")}
            if q["access"]:
                assert first["on"]["trace"]["ranked_ids"] == offline[key]["ranked_ids"], key
            for mode, row in first.items():
                sources = readback(client, headers[q["principal"]], row, q, chunks)
                actual = [[cite["document_id"], cite["location"]] for cite in sources]
                row.update(sources=sources, complete=bool(q["required"]) and sorted(actual) == sorted(q["required"]))
            rows[key] = {**q, "ranking": offline[key], **first}
            write_json(output / "questions.json", list(rows.values()))
            if not q["access"]:
                continue
            for repeat in range(REPETITIONS):
                order = ("off", "on") if (index + repeat) % 2 == 0 else ("on", "off")
                pair = {"id": key, "repeat": repeat + 1, "order": order}
                for mode in order:
                    row = sample(client, headers, q, traces, mode == "on")
                    assert row["reply"] == first[mode]["reply"], (key, mode, "SSE reply drift")
                    assert row["trace"].get("ranked_ids") == first[mode]["trace"].get("ranked_ids"), (key, mode)
                    trace = row["trace"]
                    pair[mode] = {"done_ms": row["elapsed_ms"], "trace_id": row["trace_id"],
                                  "retrieval_ms": (trace["default_retrieval"]["ms"] if mode == "off"
                                                   else trace["retrieval_ms"]),
                                  "gpu_ms": trace.get("inference_ms"), "queue_wait_ms": trace.get("queue_wait_ms"),
                                  "path": trace["path"], "fallback_reason": trace.get("fallback_reason")}
                pair["added_ms"] = pair["on"]["done_ms"] - pair["off"]["done_ms"]
                pairs.append(pair)
                raw.write(json.dumps(pair, ensure_ascii=False) + "\n")
                raw.flush()
            if (index + 1) % 10 == 0:
                print(f"Completed chat and paired measurements through {key}", flush=True)
    return rows, pairs


def score_frozen(model, writer, users, questions, chunks, output):
    report = {}
    for q in questions:
        keys = q["candidate_chunk_ids"]
        hits = [chunks[key] for key in keys]
        scores = []
        elapsed_ms = None
        if q["access"]:
            rag.activate(writer, q["target"], q["version"])
            with closing(database.get_db(read_only=True)) as conn:
                conn.execute("BEGIN")
                for hit in hits:
                    original = rag.read_chunk(conn, users[q["principal"]], hit["chunk_id"])
                    assert original and original["content"] == hit["content"], (q["id"], hit["chunk_id"])
                    assert sha256(original["original"].encode()).hexdigest() == hit["original_sha256"]
                start = perf_counter()
                scores = model.score(q["query"], [hit["content"] for hit in hits])
                elapsed_ms = (perf_counter() - start) * 1000
        ranked_ids = [hit["chunk_id"] for hit in reorder(hits, scores)] if keys else []
        assert len(scores) == len(keys)
        report[q["id"]] = {"scored_chunk_ids": keys, "scores": scores, "ranked_ids": ranked_ids,
                           "inference_ms": elapsed_ms, "required": [
                               {"source": source, "r4_rank": before, "gpu_rank": after,
                                "r4_chunk_id": keys[before - 1] if before else None,
                                "gpu_chunk_id": ranked_ids[after - 1] if after else None}
                               for source, before, after in zip(q["required"], rank_refs(q, chunks, keys),
                                                                rank_refs(q, chunks, ranked_ids), strict=True)]}
    assert model.calls == 52
    write_json(output / "ranking.json", report)
    return report


def history(writer, uid, view):
    return [dict(row) for row in writer.execute(
        "SELECT id, role, content FROM conversations WHERE user_id=? AND chat_view_id=? ORDER BY id", (uid, view),
    )]


def wait_late(late, trace_id):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        try:
            return late.read(trace_id, full=True)
        except KeyError:
            sleep(.01)
    raise AssertionError((trace_id, "Missing late GPU trace"))


def timeouts(client, headers, writer, users, model, focused, traces, late, chunks, output, *, path="default"):
    rows = {}
    for q in focused:
        rag.activate(writer, q["target"], q["version"])
        entered, completed, release = Event(), Event(), Event()

        def hold_score(query, passages):
            entered.set()
            scores = real_score(query, passages)
            completed.set()
            assert release.wait(15), "Timeout replay barrier was never released"
            return scores

        real_score = model.score
        with patch.object(model, "score", side_effect=hold_score), \
                patch.object(database.settings, "PRODUCT_RERANK_ENABLED", True):
            try:
                row = request(client, headers, q)
                assert entered.is_set() and completed.is_set(), q["id"]
                trace = traces.read(row["trace_id"])
                assert trace["fallback_reason"] == "inference_timeout" and trace["path"] == path, trace
                ids = CITATION.findall(row["reply"])
                assert ids == CITATION.findall(q["off"]["reply"]) == trace["citation_ids"], (q["id"], ids)
                uid = users[q["principal"]]
                before = history(writer, uid, row["chat_view_id"])
                assert [r["role"] for r in before] == ["user", "assistant"]
                assert CITATION.findall(before[-1]["content"]) == ids
                assert product_chat_search.get_queue().running == 1
                release_after_done_ms = perf_counter()
            finally:
                release.set()
            late_row = wait_late(late, row["trace_id"])
        after = history(writer, uid, row["chat_view_id"])
        assert before == after and late_row["reason"] == "inference_timeout"
        assert late_row["scored_chunk_ids"] == q["candidate_chunk_ids"] and late_row["scored_chunks"] == 20
        assert traces.read(row["trace_id"], full=True)["status"] == "completed"
        row.update(trace=trace, full_trace=traces.read(row["trace_id"], full=True), late_trace=late_row,
                   history_before_release=before, history_after_release=after,
                   release_after_done=True, late_trace_wait_ms=(perf_counter() - release_after_done_ms) * 1000,
                   sources=readback(client, headers[q["principal"]], row, q, chunks))
        rows[q["id"]] = row
        write_json(output / "timeouts.json", rows)
    return rows


def replay_q29(client, headers, writer, q, model, chunks, traces):
    rag.activate(writer, q["target"], q["version"])
    keys = q["candidate_chunk_ids"]
    seal = {key for key in keys if rank_locations([chunks[key]], ("manual", "page:13"), complete=True)}
    ranked = q["ranking"]["ranked_ids"]
    forced = [key for key in ranked if key not in seal]
    forced[5:5] = [key for key in ranked if key in seal]
    assert seal and set(forced) == set(keys)
    ranks = {key: rank for rank, key in enumerate(forced, 1)}
    with patch.object(model, "score", return_value=[-ranks[key] for key in keys]), \
            patch.object(database.settings, "PRODUCT_RERANK_ENABLED", True):
        row = sample(client, headers, q, traces, True, full=True)
    trace = row["trace"]
    assert trace["ranked_ids"] == forced and trace["path"] == "rerank_fill", trace
    fill, = trace["fill"]
    assert fill["rank"] == 6 and fill["chunk_id"] in seal, fill
    row["sources"] = readback(client, headers[q["principal"]], row, q, chunks)
    assert [(source["document_id"], source["location"]) for source in row["sources"]] == list(map(tuple, q["required"]))
    return row


def scope_probes(client, headers, writer, users, q, traces, chunks, output):
    rows = []
    target = q["target"]
    old_ids = CITATION.findall(q["on"]["reply"])
    assert old_ids and q["version"] == "v1"
    search = product_evidence_reply.search
    owner, other = users["owner"], users["other"]
    for grouped in (False, True):
        for change in ("version", "transfer", "session"):
            headers["owner"] = login(client, "owner")
            rag.activate(writer, target, "v1")
            group = None
            if grouped:
                response = client.post("/api/chat/conversations", headers=headers["owner"], json={"title": "R5 权限联测"})
                group = response.raise_for_status().json()["data"]["conversation_id"]

            def transfer(uid):
                with closing(database.get_db()) as conn, conn:
                    conn.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?",
                                 (uid, target["shop_id"], target["marketplace_id"]))

            def after_search(*args, **kwargs):
                result = search(*args, **kwargs)
                assert result and [cite["chunk_id"] for cite in result["evidence"]] == old_ids
                if change == "version":
                    with closing(database.get_db()) as conn:
                        rag.activate(conn, target, "v2")
                elif change == "transfer":
                    transfer(other)
                else:
                    with closing(database.get_db()) as conn, conn:
                        conn.execute("DELETE FROM sessions WHERE user_id=?", (owner,))
                return result

            try:
                with patch.object(product_evidence_reply, "search", side_effect=after_search), \
                        patch.object(database.settings, "PRODUCT_RERANK_ENABLED", True):
                    row = request(client, headers, q, group=group, terminal="error")
                trace_id = row["trace_id"]
                assert traces.read(trace_id)["scored_chunk_ids"] == q["candidate_chunk_ids"]
                assert row["events"][-1]["error_code"] == "product_evidence_scope_changed" and not row["reply"]
                if grouped:
                    persisted = [dict(r) for r in writer.execute(
                        "SELECT content FROM chat_group_entries WHERE kind='assistant' AND run_id=?",
                        (row["events"][-1]["run_id"],),
                    )]
                else:
                    persisted = [r for r in history(writer, owner, row["chat_view_id"]) if r["role"] == "assistant"]
                assert not persisted, persisted
                http = {key: client.get(CHUNKS + key, headers=headers["owner"]).status_code for key in old_ids}
                assert set(http.values()) == {401 if change == "session" else 404}, http
                recovery = None
                if change == "version":
                    recovery = request(client, headers, {**q, "version": "v2"})
                    recovery["sources"] = readback(client, headers["owner"], recovery, {**q, "version": "v2"}, chunks)
                    assert recovery["sources"] and not set(old_ids) & set(CITATION.findall(recovery["reply"]))
                elif change == "transfer":
                    new_owner = {key: client.get(CHUNKS + key, headers=headers["other"]).status_code for key in old_ids}
                    assert set(new_owner.values()) == {200}
                    recovery = {"transferred_owner_http": new_owner}
                rows.append({"change": change, "grouped": grouped, "old_chunk_ids": old_ids, "old_http": http,
                             "assistant_history": persisted, "sse": row, "trace": traces.read(trace_id, full=True),
                             "recovery": recovery})
                write_json(output / "scope.json", rows)
            finally:
                if change == "transfer":
                    transfer(owner)
    return rows


def summarize(rows, pairs, faults, scopes, model, settings, output):
    questions = list(rows.values())
    answerable = [q for q in questions if q["has_answer"]]
    unanswered = [q for q in questions if q["access"] and not q["has_answer"]]
    denied = [q for q in questions if not q["access"]]
    multi = [q for q in questions if len(q["required"]) > 1]
    focused = {}
    regressions = []
    errors = []
    for q in questions:
        key = q["id"]
        gpu = q["on"]
        actual = {(cite["document_id"], cite["location"]): cite for cite in gpu["sources"]}
        fills = {entry["chunk_id"]: entry for entry in gpu["trace"].get("fill", [])}
        if q["off"]["complete"] and not gpu["complete"]:
            regressions.append({"id": key, "missing": [ref for ref in q["required"] if tuple(ref) not in actual]})
        for rank in q["ranking"]["required"]:
            cite = actual.get(tuple(rank["source"]))
            rank.update(final_cited=cite is not None, citation_chunk_id=cite["chunk_id"] if cite else None)
            if cite and (rank["gpu_rank"] is None or rank["gpu_rank"] > 5):
                rank["fill"] = fills.get(cite["chunk_id"])
                if not rank["fill"] or not 6 <= rank["fill"]["rank"] <= 20:
                    errors.append([key, "unverified same-batch fill", rank])
        if key in FOCUS:
            focused[key] = q
            if not gpu["complete"]:
                errors.append([key, "missing focused citations"])
    for key in ("Q13", "Q14"):
        if not all(1 <= (r["gpu_rank"] or 21) <= 5 for r in focused[key]["ranking"]["required"]):
            errors.append([key, "dishwasher source outside GPU top five"])
    assert {(q["id"], *r["source"]) for q in focused.values() for r in q["ranking"]["required"]} >= TARGETS
    quotes = {(key, cite["document_id"], cite["location"]): cite["quote"]
              for key, q in focused.items() for cite in q["on"]["sources"]}
    for ref, terms in {
        ("Q21", "specs", "row:7"): ("LID-M750", "B42"),
        ("Q39", "specs", "row:3"): ("76",),
        ("Q39", "specs", "row:4"): ("255",),
    }.items():
        if not all(term in quotes.get(ref, "") for term in terms):
            errors.append([ref, "expected source details missing"])
    write_json(output / "questions.json", questions)
    for key, q in focused.items():
        write_json(output / f"{key}.json", {**q, "timeout": faults[key]})

    assert len(pairs) == 260 and len(faults) == len(scopes) == 6 and len(unanswered) == 8 and len(denied) == 2
    counts = [sum(q[mode]["complete"] for q in answerable) for mode in ("off", "on")]
    ranks = [r for q in questions for r in q["ranking"]["required"]]
    no_answer = {mode: [q["id"] for q in unanswered if q[mode]["sources"]] for mode in ("off", "on")}
    added_p95 = p95([pair["added_ms"] for pair in pairs])
    latency = {}
    for mode in ("off", "on"):
        latency[mode] = {metric: p95(values) if (values := [pair[mode][metric] for pair in pairs
                                                            if pair[mode][metric] is not None]) else None
                         for metric in ("retrieval_ms", "gpu_ms", "done_ms", "queue_wait_ms")}
    fills = {q["id"]: q["on"]["trace"]["fill"] for q in questions if q["on"]["trace"].get("fill")}
    return {
        "model": model, "plan": settings, "rank_only": {
            mode: {"evidence_at_5": [sum(0 < (r[mode] or 21) <= 5 for r in ranks), len(ranks)],
                   "multi_at_5": [sum(all(0 < (r[mode] or 21) <= 5 for r in q["ranking"]["required"])
                                      for q in multi), len(multi)]} for mode in ("r4_rank", "gpu_rank")},
        "chat_complete": dict(zip(("off", "on"), [[n, len(answerable)] for n in counts])),
        "chat_multi_complete": {mode: [sum(q[mode]["complete"] for q in multi), len(multi)] for mode in ("off", "on")},
        "newly_complete": [q["id"] for q in answerable if q["on"]["complete"] and not q["off"]["complete"]],
        "regressions": regressions, "errors": errors, "fill": fills,
        "no_answer_false_citations": no_answer, "denied_gpu_scored": {q["id"]: q["on"]["trace"]["scored_chunks"] for q in denied},
        "http_citations_verified": {mode: sum(len(q[mode]["sources"]) for q in questions) for mode in ("off", "on")},
        "scope_probes_passed": len(scopes), "timeouts_passed": len(faults), "p95_ms": latency,
        "paired_samples": len(pairs), "added_p95_ms": added_p95,
        "per_question_added_p95_ms": {key: p95([pair["added_ms"] for pair in pairs if pair["id"] == key])
                                      for key in dict.fromkeys(pair["id"] for pair in pairs)},
        "timeout_p95_ms": {
            "request_to_done": p95([row["elapsed_ms"] for row in faults.values()]),
            "fallback_retrieval": p95([row["trace"]["default_retrieval"]["ms"] for row in faults.values()]),
        },
        "expand_allowlist": counts[1] > counts[0] and not (regressions or errors or any(no_answer.values()))
        and added_p95 <= ADDED_P95_LIMIT_MS,
    }


def evaluate(run, output):
    output.mkdir(parents=True)
    questions, chunks = freeze(run, output)
    db = output / "evaluation.sqlite3"
    copy_catalog(run / "evaluation.sqlite3", db)
    with patch.object(observability, "TRACE_LOG_FILE", output / "traces.jsonl"), \
            patch.object(observability, "PRODUCT_RERANK_LATE_FILE", output / "late.jsonl"), \
            patch.object(database.settings, "JWT_SECRET", token_urlsafe(48)):
        from fast_server import app

        settings = database.settings
        traces = TraceReader(observability.TRACE_LOG_FILE)
        late = TraceReader(observability.PRODUCT_RERANK_LATE_FILE)
        with loopback(app) as client, closing(database.get_db()) as writer:
            users = {p: writer.execute("SELECT id FROM users WHERE username=?", (f"product-evidence-{p}",)).fetchone()[0]
                     for p in ("owner", "other")}
            headers = {p: login(client, p) for p in users}
            plan = {"concurrency": 1, "queue_size": 3, "queue_timeout_ms": 400, "inference_timeout_ms": 700,
                    "repetitions": REPETITIONS, "added_p95_limit_ms": ADDED_P95_LIMIT_MS,
                    "allowlist": list(users), "llm_provider": "mock", "gpu_initially_disabled": True}
            write_json(output / "plan.json", plan)
            with patch.multiple(settings, PRODUCT_RERANK_ENABLED=False,
                                PRODUCT_RERANK_USER_IDS=frozenset(users.values()),
                                PRODUCT_RERANK_MAX_CONCURRENCY=plan["concurrency"],
                                PRODUCT_RERANK_QUEUE_SIZE=plan["queue_size"],
                                PRODUCT_RERANK_QUEUE_TIMEOUT_MS=plan["queue_timeout_ms"],
                                PRODUCT_RERANK_INFERENCE_TIMEOUT_MS=plan["inference_timeout_ms"]):
                model = product_chat_search.get_queue()._model()
                assert isinstance(model, Reranker)
                assert model.metadata["device"] == "cuda:0" and model.metadata["revision"] == REVISION
                assert model.metadata["weight_sha256"] == WEIGHT_SHA256
                write_json(output / "model.json", model.metadata)
                offline = score_frozen(model, writer, users, questions, chunks, output)
                print("Finished 52 pinned CUDA scores on frozen R4 candidates", flush=True)
                rows, pairs = paired_chat(client, headers, writer, questions, chunks, traces, output, offline)
                focused = [rows[q["id"]] for q in questions if q["id"] in FOCUS]
                faults = timeouts(client, headers, writer, users, model, focused, traces, late, chunks, output)
                q29 = rows["Q29"]
                if not q29["on"]["trace"].get("fill"):
                    write_json(output / "Q29-fixed-replay.json", replay_q29(client, headers, writer, q29, model, chunks, traces))
                scopes = scope_probes(client, headers, writer, users, rows["Q26"], traces, chunks, output)
                result = summarize(rows, pairs, faults, scopes, model.metadata, plan, output)
                write_json(output / "summary.json", result)
                print(json.dumps({k: result[k] for k in ("chat_complete", "added_p95_ms", "expand_allowlist", "errors")}),
                      flush=True)
                return int(bool(result["regressions"] or result["errors"] or any(result["no_answer_false_citations"].values())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r4-run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if os.environ.get("AD_OFFLINE_ACCEPTANCE") != "1" or os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        parser.error("Set AD_OFFLINE_ACCEPTANCE=1 and PYTHON_DOTENV_DISABLED=1 before replay")
    os.environ.update(LLM_PROVIDER="mock", DIAGNOSIS_MYSQL_ENABLED="0", PRODUCT_RERANK_ENABLED="0",
                      PRODUCT_R4_CPU_ENABLED="0")
    database.settings.LLM_PROVIDER = "mock"
    database.settings.PRODUCT_R4_CPU_ENABLED = False
    raise SystemExit(evaluate(args.r4_run.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main()
