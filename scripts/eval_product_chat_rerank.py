"""Replay frozen product questions through the authenticated SSE chat endpoint."""

import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from time import perf_counter, sleep

from backend.database import database
from backend.observability import PRODUCT_RERANK_LATE_FILE, TRACE_LOG_FILE
from backend.rag import product_evidence as rag
from backend.rag.product_rerank_model import REVISION, WEIGHT_SHA256
from scripts.eval_product_query_expansion import FROZEN_SHA256
from scripts.eval_product_rerank import EXPECTED_RECALL_MISSES, copy_catalog
from scripts.product_evidence import FIXTURES, p95, rank_locations, write_json
from scripts.verify_product_evidence import CHUNKS, chat, login, server

CONCURRENCY = (1, 2, 4, 8)
BUDGET_MS = {1: 600, 2: 600, 4: 600, 8: 850}
QUEUE_SIZE = 3
QUEUE_WAIT_MS = 400
EXECUTION_MS = 700
CITATION = re.compile(r"/api/product-evidence/chunks/([a-f0-9]{64})")
WASH = {
    "Q13": ("no hotter than 60 °C", "Remove the screw cap and silicone seal"),
    "Q14": ("no hotter than 55 °C", "Remove the cap and seal"),
}


class TraceReader:
    def __init__(self, path):
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0
        self.traces = {}

    def read(self, trace_id, *, full=False):
        if trace_id not in self.traces and self.path.exists():
            with self.path.open("rb") as file:
                file.seek(self.offset)
                for raw in file:
                    if not raw.endswith(b"\n"):
                        break
                    self.offset += len(raw)
                    row = json.loads(raw)
                    self.traces[row["trace_id"]] = row
        row = self.traces[trace_id]
        if full:
            return row
        step, = [s["evidence"] for s in row["steps"] if s["action"] == "chat_retrieval"]
        return step


def request(client, headers, q):
    start = perf_counter()
    result = chat(client, headers[q["principal"]], "商品资料：" + " ".join(
        [q["target"][key] for key in ("shop_id", "marketplace_id", "sku")] + [q["query"]]
    ))
    return {**result, "id": q["id"], "elapsed_ms": (perf_counter() - start) * 1000}


def read_sources(client, auth, row, q):
    sources = []
    for key in CITATION.findall(row["reply"]):
        response = client.get(CHUNKS + key, headers=auth)
        original = response.raise_for_status().json()
        assert original["chunk_id"] == key and original["version"] == q["version"], (q["id"], key)
        assert all(original[k] == v for k, v in q["target"].items()), (q["id"], key)
        assert original["content"] == original["original"][original["start_offset"]:original["end_offset"]]
        matches = [loc for loc in original["locations"] if loc["complete"] and
                   f" · {loc['location']} · {loc['section']}\n" in row["reply"]]
        loc, = matches
        quote = original["original"][loc["start_offset"]:loc["end_offset"]]
        assert quote and quote in row["reply"], (q["id"], key)
        sources.append({**loc, "http": response.status_code, "chunk_id": key, "quote": quote,
                        "document_id": original["document_id"], "filename": original["filename"]})
    return sources


def quality(client, headers, questions, writer, traces):
    outputs = {}
    for q in questions:
        rag.activate(writer, q["target"], q["version"])
        row = request(client, headers, q)
        row["trace"] = traces.read(row["trace_id"])
        row["sources"] = read_sources(client, headers[q["principal"]], row, q)
        assert CITATION.findall(row["reply"]) == row["trace"].get("citation_ids", [])
        outputs[q["id"]] = row
    return outputs


def scope(client, headers, writer, traces, base):
    us = base["target"]
    checks = (
        ("same_name_sku", {**us, "sku": "METRO-750"}, "owner", "v1"),
        ("same_sku_ca", {**us, "marketplace_id": "CA"}, "owner", "v1"),
        ("same_sku_foreign_shop", {**us, "shop_id": "evidence-other"}, "other", "v1"),
        ("wrong_site", {**us, "marketplace_id": "GB"}, "owner", None),
        ("cross_account", us, "other", None),
        ("cross_shop", {**us, "shop_id": "evidence-other"}, "owner", None),
        ("v1", us, "owner", "v1"),
        ("v2", us, "owner", "v2"),
    )
    results = {}
    for label, target, principal, version in checks:
        q = {**base, "id": label, "target": target, "principal": principal, "version": version}
        if version:
            rag.activate(writer, target, version)
        row = request(client, headers, q)
        row["trace"] = traces.read(row["trace_id"])
        row["sources"] = read_sources(client, headers[principal], row, q)
        row["access"] = bool(version)
        results[label] = row
        if version:
            assert row["sources"] and row["trace"]["scored_chunks"] == 20, label
            for key in row["trace"]["candidate_ids"]:
                source = client.get(CHUNKS + key, headers=headers[principal]).raise_for_status().json()
                assert source["version"] == version and all(source[k] == v for k, v in target.items()), label
        else:
            assert row["trace"]["status"] == "denied" and row["trace"]["scored_chunks"] == 0, label
            assert not row["sources"]
    for label, principal in (("v1", "owner"), ("v2", "other"), ("same_sku_foreign_shop", "owner")):
        rejected = {key: client.get(CHUNKS + key, headers=headers[principal]).status_code
                    for key in results[label]["trace"]["candidate_ids"]}
        assert set(rejected.values()) == {404}, label
        results[label]["blocked_readback"] = rejected
    return results


def load_trial(client, headers, writer, questions, traces, repetitions, limit, baseline, gray):
    versions = {}
    for q in questions:
        key = tuple(q["target"][k] for k in ("shop_id", "marketplace_id", "sku"))
        if q["access"]:
            assert versions.setdefault(key, q["version"]) == q["version"]
            rag.activate(writer, q["target"], q["version"])
    started = perf_counter()
    with ThreadPoolExecutor(limit) as pool:
        rows = list(pool.map(lambda q: request(client, headers, q), questions * repetitions))
    duration = perf_counter() - started
    for row in rows:
        row["trace"] = traces.read(row["trace_id"])
        trace = row["trace"]
        expected = baseline if trace.get("fallback_reason") else gray
        assert CITATION.findall(row["reply"]) == CITATION.findall(expected[row["id"]]["reply"]), row
        if trace.get("fallback_reason") in {"queue_timeout", "queue_full", "inference_timeout", "gpu_unavailable"}:
            assert trace["path"] == "default", row
    timings = [row["trace"] for row in rows]
    fallbacks = [t["fallback_reason"] for t in timings if t.get("fallback_reason")]
    wait = [t["queue_wait_ms"] for t in timings if "queue_wait_ms" in t]
    inference = [t["inference_ms"] for t in timings if "inference_ms" in t]
    metrics = {"samples": len(rows), "throughput_per_second": round(len(rows) / duration, 3),
               "queue_depth_max": max(t.get("queue_depth", 0) for t in timings),
               "queue_p95_ms": p95(wait) if wait else None, "queue_samples": len(wait),
               "gpu_p95_ms": p95(inference) if inference else None, "gpu_samples": len(inference),
               "end_to_end_p95_ms": p95([row["elapsed_ms"] for row in rows]),
               "fallback_ratio": len(fallbacks) / len(rows),
               "fallbacks": {reason: fallbacks.count(reason) for reason in sorted(set(fallbacks))}}
    return {"metrics": metrics, "samples": rows}


def compare(questions, baseline, gray, frozen):
    rows, focused = [], {}
    for q, snapshot in zip(questions, frozen["questions"], strict=True):
        key = q["id"]
        assert key == snapshot["id"]
        default, new = baseline[key], gray[key]
        trace = new["trace"]
        required = {tuple(ref) for ref in q["required"]}
        for row in (default, new):
            actual = {(c["document_id"], c["location"]) for c in row["sources"]}
            row["complete"] = bool(required) and actual == required
            assert not actual or row["complete"], (key, actual, required)
        assert default["trace"]["scored_chunks"] == 0 and not default["trace"]["eligible"]
        order = trace.get("ranked_ids", [])
        assert trace["rerank_retrieval"].get("candidate_ids", []) == snapshot["candidate_chunk_ids"], (
            key, "R1 candidate drift",
        )
        assert len(order) == len(set(order)) and set(order) == set(snapshot["candidate_chunk_ids"]), (key, "R2 permutation")
        if not q["access"]:
            assert trace["status"] == "denied" and trace["scored_chunks"] == 0
        if default["complete"]:
            assert new["complete"] and not trace.get("fallback_reason"), (key, "default evidence regressed or fell back")
        hits = [frozen["chunks"][chunk_id] for chunk_id in order]
        ranks = [{"source": ref, "rank": rank_locations(hits, ref, complete=True)} for ref in q["required"]]
        rows.append({**q, "default": default, "gray": new, "r2_ranks": ranks})
        if key in (*WASH, "Q29"):
            assert new["complete"], key
            focused[key] = rows[-1]
        if key in WASH:
            assert all(1 <= ref["rank"] <= 5 for ref in ranks), key
            body, cap = new["sources"]
            assert body["location"] == "page:5" and all(text in body["quote"] for text in WASH[key])
            assert "upper rack only" in body["quote"] and cap["location"] == "page:6"
            assert all(text in cap["quote"] for text in ("cap by hand", "warm soapy water", "Do not put the screw cap",
                                                         "removable silicone seal in a dishwasher"))
        elif key == "Q29":
            fill, = trace["fill"]
            faq, manual = new["sources"]
            assert faq["location"] == "faq:seal" and "静水试漏" in faq["quote"]
            assert manual["location"] == "page:13" and all(text in manual["quote"] for text in (
                "flat side facing the cap", "raised edge facing the bottle", "ring lies flat all the way around the groove",
            ))
            assert (fill["chunk_id"], fill["location"]) == (manual["chunk_id"], "page:13")
            assert 6 <= fill["rank"] <= 20 and order[fill["rank"] - 1] == fill["chunk_id"]
    return rows, focused


def summarize(rows):
    needed = [ref for row in rows for ref in row["r2_ranks"]]
    multi = [row for row in rows if len(row["required"]) > 1]
    missing = [{"id": row["id"], **ref, "final_recovered": row["gray"]["complete"]}
               for row in rows for ref in row["r2_ranks"] if ref["rank"] is None]
    assert {(r["id"], *r["source"]) for r in missing} == EXPECTED_RECALL_MISSES
    assert len(needed) == 52
    return {
        "questions": len(rows), "default_complete": sum(row["default"]["complete"] for row in rows),
        "gray_complete": sum(row["gray"]["complete"] for row in rows),
        "newly_complete": [row["id"] for row in rows if row["gray"]["complete"] and not row["default"]["complete"]],
        "r2_evidence_at_5": [sum(r["rank"] is not None and r["rank"] <= 5 for r in needed), len(needed)],
        "r2_evidence_at_20": [sum(r["rank"] is not None for r in needed), len(needed)],
        "r2_multi_at_5": [sum(all(r["rank"] is not None and r["rank"] <= 5 for r in row["r2_ranks"])
                              for row in multi), len(multi)],
        "gray_multi_complete": [sum(row["gray"]["complete"] for row in multi), len(multi)],
        "no_answer_false_citations": [0, sum(row["access"] and not row["has_answer"] for row in rows)],
        "rejected_scored_chunks": [row["gray"]["trace"]["scored_chunks"] for row in rows if not row["access"]],
        "paths": {path: [row["id"] for row in rows if row["gray"]["complete"] and row["gray"]["trace"]["path"] == path]
                  for path in ("rerank_top5", "rerank_fill", "default")},
        "fallbacks": {row["id"]: row["gray"]["trace"]["fallback_reason"] for row in rows
                      if row["gray"]["trace"].get("fallback_reason")},
        "recall_misses": missing,
    }


def evaluate(catalog, output, repetitions):
    raw = (FIXTURES / "frozen.jsonl").read_bytes()
    assert sha256(raw).hexdigest() == FROZEN_SHA256
    questions = [json.loads(line) for line in raw.splitlines()]
    frozen = json.loads((FIXTURES / "rerank/frozen-candidates.json").read_bytes())
    output.mkdir(parents=True)
    db = output / "evaluation.sqlite3"
    copy_catalog(catalog, db)
    traces = TraceReader(TRACE_LOG_FILE)
    late = TraceReader(PRODUCT_RERANK_LATE_FILE)
    secret = token_urlsafe(48)
    plan = {"expected_concurrency": CONCURRENCY[:3], "overload": CONCURRENCY[-1], "p95_budget_ms": BUDGET_MS,
            "queue_size": QUEUE_SIZE, "queue_timeout_ms": QUEUE_WAIT_MS, "inference_timeout_ms": EXECUTION_MS,
            "repetitions": repetitions}
    write_json(output / "plan.json", plan)
    with closing(database.get_db()) as writer:
        allowlist = [r["id"] for r in writer.execute("SELECT id FROM users WHERE username LIKE 'product-evidence-%'")]
        off = {"PRODUCT_R4_CPU_ENABLED": "0", "PRODUCT_RERANK_ENABLED": "0",
               "PRODUCT_RERANK_USER_IDS": json.dumps(allowlist)}
        on = {**off, "PRODUCT_RERANK_ENABLED": "1", "PRODUCT_RERANK_MAX_CONCURRENCY": "1",
              "PRODUCT_RERANK_QUEUE_SIZE": str(QUEUE_SIZE), "PRODUCT_RERANK_QUEUE_TIMEOUT_MS": str(QUEUE_WAIT_MS),
              "PRODUCT_RERANK_INFERENCE_TIMEOUT_MS": str(EXECUTION_MS)}
        with server(db, output, "default-server", secret, extra_env=off) as (client, _):
            headers = {p: login(client, p) for p in ("owner", "other")}
            request(client, headers, questions[0])
            baseline = quality(client, headers, questions, writer, traces)
            write_json(output / "default.json", baseline)
        with server(db, output, "gray-server", secret, extra_env=on) as (client, _):
            headers = {p: login(client, p) for p in ("owner", "other")}
            q = questions[0]
            rag.activate(writer, q["target"], q["version"])
            cold = [request(client, headers, q)]
            for _ in range(30):
                cold[-1]["trace"] = traces.read(cold[-1]["trace_id"])
                if cold[-1]["trace"].get("inference_ms") and not cold[-1]["trace"].get("fallback_reason"):
                    break
                sleep(.15)
                cold.append(request(client, headers, q))
            write_json(output / "cold.json", cold)
            model = cold[-1]["trace"]["model"]
            assert model["revision"] == REVISION and model["weight_sha256"] == WEIGHT_SHA256
            assert "RTX 5060 Ti" in model["gpu"]
            for row in cold:
                try:
                    row["late"] = late.read(row["trace_id"], full=True)
                except KeyError:
                    pass
            write_json(output / "cold.json", cold)
            gray = quality(client, headers, questions, writer, traces)
            write_json(output / "gray.json", gray)
            rows, focused = compare(questions, baseline, gray, frozen)
            write_json(output / "questions.json", rows)
            for key, row in focused.items():
                row["gray"]["full_trace"] = traces.read(row["gray"]["trace_id"], full=True)
                write_json(output / f"{key}.json", row)
            results = summarize(rows)
            results["model"] = model
            write_json(output / "summary.json", results)
            write_json(output / "scope.json", scope(client, headers, writer, traces, questions[3]))
            # One active version per product is needed during concurrent replay.
            load_queries = [q for q in questions if q["access"] and q["version"] == "v1" and q["has_answer"]]
            plan["load_question_ids"] = [q["id"] for q in load_queries]
            write_json(output / "plan.json", plan)
            for q in load_queries:
                rag.activate(writer, q["target"], q["version"])
                request(client, headers, q)
            concurrent = {}
            for count in CONCURRENCY:
                result = load_trial(client, headers, writer, load_queries, traces, repetitions, count, baseline, gray)
                write_json(output / f"concurrency-{count}.json", result)
                concurrent[count] = result["metrics"]
            results["concurrency"] = concurrent
            results["passed"] = all(row["end_to_end_p95_ms"] <= BUDGET_MS[count]
                                    and row["queue_depth_max"] <= QUEUE_SIZE
                                    for count, row in concurrent.items())
            results["passed"] &= bool(concurrent[8]["fallbacks"].get("queue_full"))
            write_json(output / "summary.json", results)
    return int(not results["passed"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3, choices=range(1, 11))
    args = parser.parse_args()
    if os.environ.get("AD_OFFLINE_ACCEPTANCE") != "1":
        parser.error("Set AD_OFFLINE_ACCEPTANCE=1 before replay")
    raise SystemExit(evaluate(args.catalog.resolve(), args.output.resolve(), args.repetitions))
