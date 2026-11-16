"""Run a paired BGE reranker evaluation against the frozen TASK-044-R1 replay."""

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from time import perf_counter

from backend.database import database, listing_operations
from backend.rag import product_evidence as rag
from backend.rag.chinese_embedding import BGEEmbedding
from backend.rag.product_rerank_model import MODEL, REVISION, Reranker, digest, elapsed
from scripts.eval_product_query_expansion import FROZEN_SHA256, SCOPE, search
from scripts.product_evidence import FIXTURES, p95, rank_locations, use_sandbox, verify_chunk, write_json

ADMISSION_K = 5
THREADS = 4
GROUPS = ("baseline", "rerank")
EXPECTED_RECALL_MISSES = {
    ("Q21", "faq", "faq:share"), ("Q21", "specs", "row:7"),
    ("Q26", "manual", "page:10"), ("Q39", "manual", "page:15"),
}


def ids(hits):
    return [hit["chunk_id"] for hit in hits]


def check_scope(hits, target, version):
    expected = {**target, "version": version}
    return sum(any(hit[k] != expected[k] for k in SCOPE) for hit in hits)


def reorder(hits, scores):
    assert len(hits) == len(scores) == rag.RECALL_K and all(map(math.isfinite, scores))
    ranked = [hits[i] for i in sorted(range(len(hits)), key=lambda i: -scores[i])]
    before, after = Counter(ids(hits)), Counter(ids(ranked))
    assert before == after and all(count == 1 for count in before.values())
    return ranked


def freeze(service, writer, users, questions, reference, documents):
    snapshots, originals = [], {}
    assert [row["id"] for row in reference] == [q["id"] for q in questions]
    for q, old in zip(questions, reference):
        assert all(old[key] == value for key, value in q.items()), q["id"]
        rag.activate(writer, q["target"], q["version"])
        user = users[q["principal"]]
        with closing(database.get_db(read_only=True)) as conn:
            conn.execute("BEGIN")
            retrieved = search(service, conn, user, q, "both", audit=True)
            assert (retrieved is not None) == q["access"], q["id"]
            hits = retrieved["candidates"] if retrieved else []
            keys = ids(hits)
            assert keys == old["candidate_chunk_ids"], (q["id"], "R1 candidate order differs")
            assert len(keys) == len(set(keys)) == (rag.RECALL_K if q["access"] else 0)
            assert check_scope(hits, q["target"], q["version"]) == 0
            assert [rank_locations(hits, ref, complete=True) for ref in q["required"]] == [
                row["complete_rank"] for row in old.get("source_ranks", [])
            ], (q["id"], "R1 source ranks differ")
            for hit in hits:
                key = hit["chunk_id"]
                if key not in originals:
                    originals[key] = verify_chunk(conn, user, hit, documents)
                assert originals[key]["ok"], (q["id"], key)
            snapshots.append({key: q[key] for key in ("id", "principal", "target", "version", "query", "access")}
                             | {"candidate_chunk_ids": keys})
    return snapshots, originals


def trial(service, user, q, snapshot, model=None):
    start = perf_counter()
    with closing(database.get_db(read_only=True)) as conn:
        conn.execute("BEGIN")
        try:
            result, rows = service.retrieve(conn, user, q["target"], q["query"].strip(),
                                            mode="vector", expansion="both")
        except listing_operations.ListingNotFound:
            assert not snapshot["candidate_chunk_ids"]
            return None, {"end_to_end_ms": elapsed(start), "scored_chunks": 0}
        hits = result["candidates"]
        assert ids(hits) == snapshot["candidate_chunk_ids"], (q["id"], "candidate drift")
        assert result["version"] == q["version"] and not check_scope(hits, q["target"], q["version"])
        assert all(hit["active"] for hit in hits)
        timing = {"scored_chunks": 0}
        score_input, scores = [], []
        if model is not None:
            rerank_start = perf_counter()
            score_input = ids(hits)
            scores = model.score(q["query"], [hit["content"] for hit in hits])
            hits = reorder(hits, scores)
            timing.update(rerank_ms=elapsed(rerank_start), scored_chunks=len(scores))
        decision = rag.admit_evidence({**result, "candidates": hits[:ADMISSION_K]}, rows, q["query"].strip())
        timing["end_to_end_ms"] = elapsed(start)
        assert not check_scope(decision["evidence"], q["target"], q["version"])
        return (hits, decision, dict(zip(score_input, scores))), timing


def describe(q, output, originals):
    if output is None:
        return {"status": "denied", "reason": "listing_not_found", "order": [], "evidence": [], "ranks": []}
    hits, decision, scores = output
    evidence = decision["evidence"]
    gold = set(map(tuple, q["required"]))
    actual = {(item["document_id"], item["location"]) for item in evidence}
    for cite in evidence:
        candidate = originals[cite["chunk_id"]]
        assert cite["chunk_id"] in ids(hits[:ADMISSION_K])
        assert all(candidate[key] == cite[key] for key in (*SCOPE, "document_id", "filename"))
        assert cite["complete"] and any(all(cite[key] == value for key, value in loc.items())
                                        for loc in candidate["locations"])
        start = candidate["start_offset"]
        assert cite["quote"] == candidate["content"][cite["start_offset"] - start:cite["end_offset"] - start]
    ranks = [{"source": ref, "rank": rank_locations(hits, ref, complete=True)} for ref in q["required"]]
    return {"status": decision["status"], "reason": decision["reason"],
            "order": ids(hits), "scores_in_baseline_order": scores, "evidence": evidence, "ranks": ranks,
            "citations_match_gold": actual == gold,
            "missing_required_top_5": [r["source"] for r in ranks if (r["rank"] or 21) > ADMISSION_K],
            "reported_missing_sections": decision.get("missing_sections", [])}


def compare(service, writer, users, questions, snapshots, model, originals, repetitions):
    reports = []
    for i, (q, snapshot) in enumerate(zip(questions, snapshots)):
        rag.activate(writer, q["target"], q["version"])
        user = users[q["principal"]]
        warm = {group: trial(service, user, q, snapshot, model if group == "rerank" else None)[0] for group in GROUPS}
        measurements = []
        for run in range(repetitions):
            pair = {}
            order = GROUPS if (run + i) % 2 == 0 else GROUPS[::-1]
            for group in order:
                result, timing = trial(service, user, q, snapshot, model if group == "rerank" else None)
                assert result == warm[group], (q["id"], group, "unstable output")
                pair[group] = timing
            pair["order"] = list(order)
            pair["paired_delta_ms"] = pair["rerank"]["end_to_end_ms"] - pair["baseline"]["end_to_end_ms"]
            measurements.append(pair)
        assert all((out is not None) == q["access"] for out in warm.values()), q["id"]
        groups = {g: describe(q, warm[g], originals) for g in GROUPS}
        b, r = (groups[g] for g in GROUPS)
        changes = [{"id": q["id"], "source": old["source"], "baseline_rank": old["rank"], "rerank_rank": new["rank"]}
                   for old, new in zip(b["ranks"], r["ranks"])]
        entered = [item for item in changes if (item["rerank_rank"] or 21) <= ADMISSION_K <
                   (item["baseline_rank"] or 21)]
        exited = [item for item in changes if (item["baseline_rank"] or 21) <= ADMISSION_K <
                  (item["rerank_rank"] or 21)]
        displaced = [{"chunk_id": key, "baseline_rank": rank, "rerank_rank": r["order"].index(key) + 1}
                     for rank, key in enumerate(b["order"][:ADMISSION_K], 1) if key not in r["order"][:ADMISSION_K]]
        promoted = [{"chunk_id": key, "baseline_rank": b["order"].index(key) + 1, "rerank_rank": rank}
                    for rank, key in enumerate(r["order"][:ADMISSION_K], 1) if key not in b["order"][:ADMISSION_K]]
        reports.append({**q, **groups, "timing": measurements, "rank_changes": changes, "entered_top_5": entered,
                        "exited_top_5": exited, "displaced_chunks": displaced, "promoted_chunks": promoted})
        print(q["id"], b["status"], r["status"], flush=True)
    assert model.calls == sum(q["access"] for q in questions) * (repetitions + 1)
    return reports


def summary(rows):
    authorized = [q for q in rows if q["access"]]
    multi = [q for q in authorized if len(q["required"]) > 1]
    pairs = [timing for q in authorized for timing in q["timing"]]
    counts = {}
    no_answer = [q for q in authorized if not q["has_answer"]]
    for group in GROUPS:
        counts[group] = {"p95_ms": p95([p[group]["end_to_end_ms"] for p in pairs])}
        for k in (5, 20):
            flags = [[(r["rank"] or 21) <= k for r in q[group]["ranks"]] for q in authorized]
            counts[group][f"evidence_at_{k}"] = [sum(map(sum, flags)), sum(map(len, flags))]
            counts[group][f"multi_all_at_{k}"] = [sum(all(f) for f in flags if len(f) > 1), len(multi)]
        counts[group]["false_citations"] = [q["id"] for q in no_answer if q[group]["evidence"]]
        counts[group]["incorrect_citations"] = [q["id"] for q in authorized
                                                  if q[group]["evidence"] and not q[group]["citations_match_gold"]]
    return {"groups": counts, "timed_pairs": len(pairs), "rerank_p95_ms": p95([p["rerank"]["rerank_ms"] for p in pairs]),
            "paired_delta_p95_ms": p95([p["paired_delta_ms"] for p in pairs]),
            "net_top_5": counts["rerank"]["evidence_at_5"][0] - counts["baseline"]["evidence_at_5"][0],
            "net_multi_top_5": counts["rerank"]["multi_all_at_5"][0] - counts["baseline"]["multi_all_at_5"][0],
            "entered_top_5": [x for q in authorized for x in q["entered_top_5"]],
            "exited_top_5": [x for q in authorized for x in q["exited_top_5"]],
            "lost_complete_evidence": [q["id"] for q in authorized if q["baseline"]["status"] == "supported"
                                       and q["rerank"]["status"] != "supported"],
            "still_missing_top_20": [x for q in authorized for x in q["rank_changes"] if x["rerank_rank"] is None],
            "multi": [{"id": q["id"], "ranks": q["rank_changes"],
                       **{g: not q[g]["missing_required_top_5"] for g in GROUPS}} for q in multi],
            "no_answer": [{"id": q["id"], **{g: q[g]["reason"] for g in GROUPS}} for q in no_answer],
            "denied": [{"id": q["id"], "passes": len(q["timing"]), "scored_chunks": 0} for q in rows if not q["access"]]}


def compare_cpu(rows, report):
    archive = FIXTURES / "rerank"
    cpu_rows = json.loads((archive / "questions.json").read_bytes())
    cpu = json.loads((archive / "summary.json").read_bytes())
    assert [q["id"] for q in rows] == [q["id"] for q in cpu_rows]
    assert report["frozen_candidates_sha256"] == cpu["frozen_candidates_sha256"]
    previous, current = cpu["paired_delta_p95_ms"], report["paired_delta_p95_ms"]
    comparison = {
        "cpu_paired_delta_p95_ms": previous, "gpu_paired_delta_p95_ms": current,
        "latency_reduction_ms": round(previous - current, 3),
        "latency_reduction_percent": round(100 * (previous - current) / previous, 2),
        "order_changes": [], "top_5_set_changes": [], "required_rank_changes": [], "citation_changes": [],
    }
    for row, prior in zip(rows, cpu_rows):
        assert row["baseline"]["order"] == prior["baseline"]["order"], row["id"]
        gpu, cpu = row["rerank"], prior["rerank"]
        if gpu["order"] != cpu["order"]:
            comparison["order_changes"].append(row["id"])
        if set(gpu["order"][:ADMISSION_K]) != set(cpu["order"][:ADMISSION_K]):
            comparison["top_5_set_changes"].append(row["id"])
        for old, new in zip(cpu["ranks"], gpu["ranks"]):
            assert old["source"] == new["source"]
            if old["rank"] != new["rank"]:
                comparison["required_rank_changes"].append({
                    "id": row["id"], "source": new["source"], "cpu_rank": old["rank"], "gpu_rank": new["rank"],
                })
        if (gpu["status"], gpu["evidence"]) != (cpu["status"], cpu["evidence"]):
            comparison["citation_changes"].append(row["id"])
    return comparison


def copy_catalog(source, dest):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(dest)) as db:
            src.backup(db)
    use_sandbox(dest)


def verify_scope(run, output, device="cpu"):
    from scripts.verify_product_evidence import CHUNKS, SEARCH, TARGET, login, server

    output.mkdir(parents=True)
    db = output / "evaluation.sqlite3"
    copy_catalog(run / "evaluation.sqlite3", db)
    model = Reranker(device)
    manifest = json.loads((run / "frozen-candidates.json").read_bytes())
    assert digest(run / "frozen-candidates.json") == json.loads((run / "freeze.json").read_bytes())["frozen_candidates_sha256"]
    us = TARGET
    ca = {**us, "marketplace_id": "CA"}
    sku750 = {**us, "sku": "METRO-750"}
    foreign = {**us, "shop_id": "evidence-other"}
    unknown_site = {**us, "marketplace_id": "GB"}
    sets = [
        ("same_name_500_us", us, "v1", "owner", True),
        ("same_name_750_us", sku750, "v1", "owner", True),
        ("same_sku_500_ca", ca, "v1", "owner", True),
        ("cross_shop_correct_account", foreign, "v1", "other", True),
        ("cross_shop_wrong_account", foreign, "v1", "owner", False),
        ("reverse_cross_account", us, "v1", "other", False),
        ("wrong_site", unknown_site, None, "owner", False),
        ("version_2", us, "v2", "owner", True),
    ]
    service = rag.ProductEvidence(embedding=BGEEmbedding(device="cpu"))
    probe_query = "接触饮料的内胆是什么材质？"
    report, allowed = {}, {}
    ranked = json.loads((run / "questions.json").read_bytes())
    with server(db, output, "http", token_urlsafe(48), extra_env={"CUDA_VISIBLE_DEVICES": "-1"}) as (
        client, _
    ), closing(database.get_db()) as writer:
        headers = {p: login(client, p) for p in ("owner", "other")}
        users = {p: writer.execute("SELECT id FROM users WHERE username=?",
                                   (f"product-evidence-{p}",)).fetchone()["id"] for p in headers}
        for label, target, version, principal, access in sets:
            if version is not None:
                rag.activate(writer, target, version)
            q = {"id": label, "target": target, "version": version, "query": probe_query}
            user, auth = users[principal], headers[principal]
            before = model.calls
            with closing(database.get_db(read_only=True)) as conn:
                conn.execute("BEGIN")
                baseline = search(service, conn, user, q, "both", audit=False)
            assert (baseline is not None) == access
            candidates = ids(baseline["candidates"]) if access else []
            result, timing = trial(service, user, q, {"candidate_chunk_ids": candidates}, model)
            response = client.post(SEARCH, headers=auth, json={**target, "query": probe_query,
                                                               "mode": "vector", "expansion": "both"})
            row = {"target": target, "version": version, "principal": principal, "authorized": access,
                   "http_search": response.status_code, "scored_chunks": timing["scored_chunks"],
                   "rerank_calls": model.calls - before, "model_input_ids": candidates, "citations": []}
            assert row["rerank_calls"] == int(access)
            if access:
                hits, decision, _ = result
                assert response.status_code == 200 and ids(response.json()["candidates"]) == candidates
                sources = {}
                for hit in hits:
                    response = client.get(CHUNKS + hit["chunk_id"], headers=auth)
                    assert response.status_code == 200
                    original = response.json()
                    assert original == {**hit, "original": original["original"]}
                    assert original["content"] == original["original"][hit["start_offset"]:hit["end_offset"]]
                    sources[hit["chunk_id"]] = original
                for cite in decision["evidence"]:
                    assert cite["quote"] == sources[cite["chunk_id"]]["original"][cite["start_offset"]:cite["end_offset"]]
                row.update(citations=decision["evidence"], http_lookup_passed=len(sources),
                           model_scope_errors=0, citation_scope_errors=0)
                allowed[label] = sources
            else:
                assert response.status_code == 404 and result is None and not row["scored_chunks"]
                source_label = {"cross_shop_wrong_account": "cross_shop_correct_account",
                                "reverse_cross_account": "same_name_500_us"}.get(label)
                if source_label:
                    statuses = [client.get(CHUNKS + key, headers=auth).status_code for key in allowed[source_label]]
                    assert statuses == [404] * len(statuses)
                    row["chunk_readback_statuses"] = statuses
                else:
                    row["shared_us_ca_chunk_ids"] = sorted(set(allowed["same_sku_500_ca"]) &
                                                           set(allowed["same_name_500_us"]))
                    assert not row["shared_us_ca_chunk_ids"]
            report[label] = row
        old = [key for key, chunk in manifest["chunks"].items()
               if not check_scope([chunk], us, "v1")]
        new = [key for key, chunk in manifest["chunks"].items()
               if not check_scope([chunk], us, "v2")]
        old_codes = [client.get(CHUNKS + key, headers=headers["owner"]).status_code for key in old]
        new_codes = [client.get(CHUNKS + key, headers=headers["owner"]).status_code for key in new]
        assert old_codes == [404] * len(old) and new_codes == [200] * len(new) and old and new
        report["after_switch_v1_to_v2"] = {
            "old_http": dict(zip(old, old_codes)), "new_http": dict(zip(new, new_codes)),
            "old_in_v2_input": sorted(set(old) & set(report["version_2"]["model_input_ids"])),
        }
        assert not report["after_switch_v1_to_v2"]["old_in_v2_input"]
        focused = {}
        wash_expectations = {
            "Q13": {"page:5": ("bottle body", "upper rack only", "no hotter than 60 °C",
                               "Remove the screw cap and silicone seal"),
                    "page:6": ("cap by hand", "warm soapy water", "Do not put the screw cap",
                               "removable silicone seal in a dishwasher")},
            "Q14": {"page:5": ("bottle body", "upper rack only", "no hotter than 55 °C",
                               "Remove the cap and seal"),
                    "page:6": ("cap by hand", "warm soapy water", "Do not put the screw cap",
                               "removable silicone seal in a dishwasher")},
        }
        for q in (row for row in ranked if row["id"] in ("Q13", "Q14")):
            rag.activate(writer, q["target"], q["version"])
            sections = []
            for change in q["rank_changes"]:
                rank = change["rerank_rank"]
                assert rank is not None, (q["id"], change["source"])
                ranked_id = q["rerank"]["order"][rank - 1]
                source = client.get(CHUNKS + ranked_id, headers=headers["owner"]).raise_for_status().json()
                document_id, location = change["source"]
                assert source["document_id"] == document_id and not check_scope([source], q["target"], q["version"])
                region = next(loc for loc in source["locations"] if loc["location"] == location and loc["complete"])
                quote = source["original"][region["start_offset"]:region["end_offset"]]
                expected = wash_expectations[q["id"]][location]
                assert all(text in quote for text in expected), (q["id"], location, quote)
                admitted = [cite for cite in q["rerank"]["evidence"] if cite["location"] == location
                            and cite["document_id"] == document_id]
                if admitted:
                    assert admitted[0]["quote"] == quote and admitted[0]["chunk_id"] == ranked_id
                sections.append({**change, **region, "filename": source["filename"], "chunk_id": ranked_id,
                                 "quote": quote, "http": 200, "admitted": bool(admitted),
                                 "verified_conditions": list(expected)})
            focused[q["id"]] = {"target": q["target"], "version": q["version"], "query": q["query"],
                                "status": q["rerank"]["status"], "sections": sections}
        assert set(focused) == {"Q13", "Q14"}
        write_json(output / "wash-readback.json", focused)
    write_json(output / "scope.json", report)
    return 0


def evaluate(replay, output, repetitions, freeze_only, device="cpu"):
    raw_questions = (FIXTURES / "frozen.jsonl").read_bytes()
    assert sha256(raw_questions).hexdigest() == FROZEN_SHA256
    questions = [json.loads(line) for line in raw_questions.splitlines()]
    assert len({q["id"] for q in questions}) == 54 and sum(q["access"] for q in questions) == 52
    assert sum(len(q["required"]) for q in questions) == 52
    baseline_path = replay / "both.json"
    reference = json.loads(baseline_path.read_bytes())
    archived = json.loads((FIXTURES / "query-expansion/both.json").read_bytes())
    assert [(q["id"], q["candidate_chunk_ids"]) for q in reference] == [
        (q["id"], q["candidate_chunk_ids"]) for q in archived
    ], "Independent replay differs from archived R1 candidates"
    documents = {p.name: p.read_bytes().decode("utf-8") for p in (FIXTURES / "sources").iterdir()}
    output.mkdir(parents=True)
    db = output / "evaluation.sqlite3"
    copy_catalog(replay / "evaluation.sqlite3", db)
    service = rag.ProductEvidence(embedding=BGEEmbedding(device="cpu"))
    with closing(database.get_db()) as writer:
        users = {p: writer.execute("SELECT id FROM users WHERE username=?",
                                   (f"product-evidence-{p}",)).fetchone()["id"] for p in ("owner", "other")}
        snapshots, originals = freeze(service, writer, users, questions, reference, documents)
        manifest = output / "frozen-candidates.json"
        write_json(manifest, {"input_sha256": FROZEN_SHA256, "r1_both_sha256": digest(baseline_path),
                              "questions": snapshots, "chunks": originals})
        metadata = {"input_sha256": FROZEN_SHA256, "r1_both_sha256": digest(baseline_path),
                    "frozen_candidates_sha256": digest(manifest), "frozen_questions": len(questions),
                    "authorized_candidate_occurrences": sum(len(s["candidate_chunk_ids"]) for s in snapshots),
                    "unique_chunks": len(originals), "retrieval_model": rag.BGE_ID}
        cpu_manifest = json.loads((FIXTURES / "rerank/frozen-candidates.json").read_bytes())
        assert cpu_manifest["input_sha256"] == FROZEN_SHA256
        assert snapshots == cpu_manifest["questions"] and originals == cpu_manifest["chunks"], (
            "Frozen candidates differ from the CPU evaluation"
        )
        write_json(output / "freeze.json", metadata)
        if freeze_only:
            return 0
        print("Loading", MODEL, REVISION, flush=True)
        model = Reranker(device)
        first = next(snapshot for snapshot in snapshots if snapshot["access"])
        start = perf_counter()
        first_scores = model.score(first["query"], [originals[key]["content"] for key in first["candidate_chunk_ids"]])
        assert len(first_scores) == rag.RECALL_K and all(map(math.isfinite, first_scores))
        model.metadata["first_20_pairs_ms"] = elapsed(start)
        model.metadata["local_cold_start_ms"] = model.metadata["local_load_ms"] + model.metadata["first_20_pairs_ms"]
        model.calls = 0
        write_json(output / "model.json", model.metadata)
        reports = compare(service, writer, users, questions, snapshots, model, originals, repetitions)
        if model.device.type == "cuda":
            model.metadata["peak_allocated_gpu_bytes"] = model.torch.cuda.max_memory_allocated(model.device)
            model.metadata["peak_reserved_gpu_bytes"] = model.torch.cuda.max_memory_reserved(model.device)
            write_json(output / "model.json", model.metadata)
    report = {**metadata, **summary(reports), "model": model.metadata, "repetitions": repetitions,
              "question_warmups_per_group": 1, "rerank_question_calls": model.calls,
              "scope_errors": {"model_inputs": 0, "citations": 0},
              "candidate_source_lookups": metadata["authorized_candidate_occurrences"]}
    if device == "cuda":
        report["cpu_comparison"] = compare_cpu(reports, report)
    write_json(output / "questions.json", reports)
    write_json(output / "summary.json", report)
    base, new = (report["groups"][group] for group in GROUPS)
    assert [base[key] for key in ("evidence_at_5", "evidence_at_20", "multi_all_at_5", "multi_all_at_20")] == [
        [28, 52], [48, 52], [1, 7], [5, 7]]
    assert new["evidence_at_20"] == base["evidence_at_20"] and new["multi_all_at_20"] == base["multi_all_at_20"]
    assert {(x["id"], *x["source"]) for x in report["still_missing_top_20"]} == EXPECTED_RECALL_MISSES
    return int(any(g["incorrect_citations"] or g["false_citations"] for g in (base, new)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--r1-replay", type=Path)
    inputs.add_argument("--verify-run", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, choices=range(1, 21), default=5)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--freeze-only", action="store_true")
    args = parser.parse_args()
    if not args.freeze_only or args.verify_run:
        import torch

        torch.set_num_threads(THREADS)
        torch.set_num_interop_threads(1)
    if args.verify_run:
        return verify_scope(args.verify_run.resolve(), args.output.resolve(), args.device)
    return evaluate(args.r1_replay.resolve(), args.output.resolve(), args.repetitions, args.freeze_only, args.device)


if __name__ == "__main__":
    sys.exit(main())
