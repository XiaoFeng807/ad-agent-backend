"""Compare R1 and R4 with pinned BGE, full channel ranks, and source readback."""

import argparse
import json
import sys
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from backend.database import database
from backend.rag import product_evidence as rag
from scripts.eval_product_query_expansion import FROZEN_SHA256, group_summary, report_row, search
from scripts.product_evidence import FIXTURES, import_pack, rank_locations, use_sandbox, write_json

R4 = FIXTURES / "query-expansion-r4"
PINNED_MODEL = "BAAI/bge-small-zh-v1.5@7999e1d3359715c523056ef9478215996d62a620"
TARGETS = {
    ("Q21", "faq", "faq:share"), ("Q21", "specs", "row:7"),
    ("Q26", "manual", "page:10"), ("Q39", "manual", "page:15"),
}
NEW_CHANNELS = {
    "direct": set(), "replacement": {"cap_compatibility", "cap_service_part"},
    "microwave": {"microwave"}, "holder": {"holder_dimensions", "holder_clearance"},
}
POLICIES = {"r1": rag.R1_EXPANSION_POLICY, "r4": rag.DEFAULT_EXPANSION_POLICY}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def check_inputs(stage):
    source_hashes = json.loads((R4 / "source-hashes.json").read_bytes())
    assert set(source_hashes) == {"manifest.json", *(f"sources/{p.name}" for p in (FIXTURES / "sources").iterdir())}
    actual = {name: digest(FIXTURES / name) for name in source_hashes}
    assert actual == source_hashes and rag.BGE_ID == PINNED_MODEL
    path = FIXTURES / "frozen.jsonl" if stage == "frozen" else R4 / "development.jsonl"
    input_hash = digest(path)
    if stage == "frozen":
        assert input_hash == FROZEN_SHA256
    questions = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert len({q["id"] for q in questions}) == len(questions)
    if stage == "frozen":
        freeze = json.loads((R4 / "freeze.json").read_bytes())
        query_module = FIXTURES.parents[1] / "backend/rag/product_query_expansion.py"
        assert freeze == {
            "model": rag.BGE_ID, "development_sha256": digest(R4 / "development.jsonl"),
            "query_expansion_sha256": sha256(query_module.read_text(encoding="utf-8").encode()).hexdigest(),
            "policies": {name: asdict(policy) for name, policy in POLICIES.items()},
            "sources_sha256": actual,
        }
    else:
        questions = [{"principal": "owner", "access": True, "has_answer": bool(q["required"]), **q}
                     for q in questions]
        frozen_queries = {json.loads(line)["query"] for line in (FIXTURES / "frozen.jsonl").read_bytes().splitlines()}
        assert all(q["query"] not in frozen_queries for q in questions)
    return questions, input_hash, actual


def evaluate_question(services, conn, user, question, repetitions):
    if not question["access"]:
        for service in services.values():
            with patch.object(service.embedding, "embed_query", wraps=service.embedding.embed_query) as encoder:
                assert search(service, conn, user, question, "both", audit=True) is None
                encoder.assert_not_called()
        return dict.fromkeys(services), {name: [] for name in services}
    results = {name: search(service, conn, user, question, "both", audit=True)
               for name, service in services.items()}
    timings = {name: [] for name in services}
    names = tuple(services)
    for repeat in range(repetitions):
        for name in (names if repeat % 2 == 0 else names[::-1]):
            start = perf_counter()
            result = search(services[name], conn, user, question, "both", audit=False)
            timings[name].append(round((perf_counter() - start) * 1000, 3))
            assert result == {k: v for k, v in results[name].items() if k not in rag.AUDIT_FIELDS}
    return results, timings


def annotate(row, result, r1, full_sources, authorized_ids, release_rows):
    if result is None:
        row["blocked_before_encoding"] = True
        return
    assert result["version"] == row["version"]
    assert len(row["candidate_chunk_ids"]) == len(set(row["candidate_chunk_ids"])) == rag.RECALL_K
    stages = {stage: set(keys) for stage, keys in result["full_vector_orders"].items()}
    stages.update(pool={hit["chunk_id"] for hit in result["pool"]},
                  merged=set(row["candidate_chunk_ids"]), citations={cite["chunk_id"] for cite in row["evidence"]})
    row["unauthorized_chunk_counts"] = {stage: len(ids - authorized_ids) for stage, ids in stages.items()}
    new_channels = set(result["queries"]) - set(r1["queries"])
    old_ids = {hit["chunk_id"] for hit in r1["candidates"]}
    row["new_channel_additions"] = {
        name: [{"chunk_id": key, "merged_rank": rank} for rank, key in enumerate(row["candidate_chunk_ids"], 1)
               if key in row["channel_chunk_ids"][name] and key not in old_ids]
        for name in result["queries"] if name in new_channels
    }
    for entry in row["source_ranks"]:
        ref = tuple(entry["source"])
        complete_ids = full_sources.get(ref, set())
        entry["indexed_complete"] = bool(complete_ids)
        entry["raw_channel_ranks"] = {
            name: next((rank for rank, key in enumerate(keys, 1) if key in complete_ids), None)
            for name, keys in result["full_vector_orders"].items()
        }
        entry["displaced_at_merge"] = entry["complete_rank"] is None and any(entry["channel_ranks"].values())
    if row.get("r1_partial"):
        row["partial_source_ranks"] = [
            {"source": ref, "rank": rank_locations(result["candidates"], ref, complete=True)}
            for ref in row["r1_partial"]
        ]
    if row.get("admit_partial"):
        partial_ids = set().union(*(full_sources[tuple(ref)] for ref in row["admit_partial"]))
        partial = [hit for hit in result["candidates"] if hit["chunk_id"] in partial_ids]
        admitted = rag.admit_evidence({**result, "candidates": partial}, release_rows, row["query"],
                                      report_all_missing=True)
        row["partial_admission"] = {key: admitted[key] for key in ("status", "reason", "evidence", "missing_sections")}
        row["partial_admission"]["candidates"] = [hit["chunk_id"] for hit in partial]


def read_scope(conn, user, question):
    target = question["target"]
    rows = conn.execute(
        """SELECT c.chunk_id, d.document_id, d.regions, c.start_offset, c.end_offset
           FROM product_document_releases r
           JOIN product_documents d ON d.release_id=r.id
           JOIN product_document_chunks c ON c.source_id=d.id
           JOIN listing_test_shops s ON s.shop_id=r.shop_id AND s.marketplace_id=r.marketplace_id
           WHERE s.user_id=? AND r.shop_id=? AND r.marketplace_id=? AND r.sku=?
             AND r.version=? AND r.active=1 AND c.strategy='structured'""",
        (user, target["shop_id"], target["marketplace_id"], target["sku"], question["version"]),
    ).fetchall()
    complete = {}
    for row in rows:
        for region in json.loads(row["regions"]):
            if row["start_offset"] <= region["start_offset"] and row["end_offset"] >= region["end_offset"]:
                complete.setdefault((row["document_id"], region["location"]), set()).add(row["chunk_id"])
    return complete, {row["chunk_id"] for row in rows}


def gate_development(rows, baseline, summary):
    failures = []
    for row, r1 in zip(rows, baseline, strict=True):
        new_channels = set(row["queries"]) - set(r1["queries"])
        if new_channels != NEW_CHANNELS[row["intent"]]:
            failures.append({"id": row["id"], "unexpected_channels": sorted(new_channels)})
        if row.get("r1_partial") and (not all(entry["rank"] for entry in r1["partial_source_ranks"])
                                      or all(entry["complete_rank"] for entry in r1["source_ranks"])):
            failures.append({"id": row["id"], "r1_not_partial": True})
        if partial := row.get("partial_admission"):
            if (len(partial["candidates"]) != len(row["admit_partial"]) or partial["evidence"]
                    or partial["reason"] != "incomplete_source_coverage" or not row["evidence_matches_gold"]):
                failures.append({"id": row["id"], "partial_admission": partial})
    if summary["still_missing_top_20"]:
        failures.append({"missing_required": summary["still_missing_top_20"]})
    return failures


def gate_frozen(rows, baseline, summaries):
    previous = json.loads((FIXTURES / "query-expansion" / "both.json").read_bytes())
    mismatches = [row["id"] for old, row in zip(previous, baseline, strict=True)
                  if any(row[key] != old[key] for key in ("id", "candidate_chunk_ids", "channel_chunk_ids", "evidence"))]
    targets = [item for item in summaries["r4"]["new_top_20"] if (item["id"], *item["source"]) in TARGETS]
    rescued = []
    for row, old in zip(rows, baseline, strict=True):
        for new, before in zip(row.get("source_ranks", []), old.get("source_ranks", []), strict=True):
            if (row["id"], *new["source"]) in TARGETS:
                rescued.append({"id": row["id"], "target": row["target"], "version": row["version"],
                                "r1": before, "r4": new})
    expected = {"r1": ([48, 52], [5, 7]), "r4": ([52, 52], [7, 7])}
    passed = (not mismatches and len(targets) == 4 and
              all((summaries[name]["evidence_at_20"], summaries[name]["multi_all_at_20"]) == metrics
                  for name, metrics in expected.items()) and
              all(row["evidence_matches_gold"] for row in rows if row["access"]))
    return passed, {"baseline_mismatches": mismatches, "targets_in_top_20": [len(targets), 4],
                    "target_ranks": rescued}


def evaluate(stage, output, repetitions):
    questions, input_hash, source_hashes = check_inputs(stage)
    output.mkdir(parents=True)
    use_sandbox(output / "evaluation.sqlite3")
    database.init_db()
    documents = {path.name: path.read_bytes().decode("utf-8") for path in (FIXTURES / "sources").iterdir()}
    services = {name: rag.ProductEvidence(expansion_policy=policy) for name, policy in POLICIES.items()}
    reports = {name: [] for name in services}
    lookup = {}
    with closing(database.get_db()) as writer:
        users, builds = import_pack(writer, FIXTURES / "manifest.json", seed=True)
        assert all(build["inserted"] for build in builds)
        for question in questions:
            rag.activate(writer, question["target"], question["version"])
            user = users[question["principal"]]
            with closing(database.get_db(read_only=True)) as conn:
                conn.execute("BEGIN")
                results, timings = evaluate_question(services, conn, user, question, repetitions)
                full_sources, authorized_ids = read_scope(conn, user, question)
                for name, result in results.items():
                    row = report_row(question, result, results["r1"], timings[name], conn, user, lookup, documents)
                    release_rows = []
                    if question.get("admit_partial"):
                        release_rows = rag.store.scoped_chunks(conn, user, rag._scope(question["target"]), "structured")
                    annotate(row, result, results["r1"], full_sources, authorized_ids, release_rows)
                    reports[name].append(row)
    summaries = {name: group_summary(rows, reports["r1"]) for name, rows in reports.items()}
    summary = {"stage": stage, "input_sha256": input_hash, "sources_sha256": source_hashes,
               "model": rag.BGE_ID, "python": sys.version.split()[0], "questions": len(questions),
               "repetitions": repetitions, "policies": {name: asdict(policy) for name, policy in POLICIES.items()},
               "builds": builds, "groups": summaries}
    passed = True
    for name, metrics in summaries.items():
        rows = reports[name]
        metrics["unauthorized_chunks"] = sum(sum(row.get("unauthorized_chunk_counts", {}).values()) for row in rows)
        passed &= (not metrics["unauthorized_chunks"] and not any(metrics["leaks"].values())
                   and metrics["lookup"][0] == metrics["lookup"][1] and not metrics["no_answer_false_citations"]
                   and not metrics["citation_readback_failures"] and not metrics["lost_top_20"]
                   and not metrics["lost_top_5"] and metrics["denials"][0] == metrics["denials"][1])
        assert all(row["candidate_chunk_ids"][:5] == row["channel_chunk_ids"]["vector"][:5]
                   for row in rows if row["access"])
        write_json(output / f"{name}.json", rows)
        print(name, *(metrics[key] for key in ("evidence_at_20", "evidence_at_5", "multi_all_at_20", "p95_ms")),
              flush=True)
    if stage == "frozen":
        assert len(questions) == 54
        frozen_passed, gates = gate_frozen(reports["r4"], reports["r1"], summaries)
        summary.update(gates)
        passed &= frozen_passed
    else:
        summary["development_failures"] = gate_development(reports["r4"], reports["r1"], summaries["r4"])
        passed &= not summary["development_failures"]
    summary["passed"] = passed
    write_json(output / "summary.json", summary)
    write_json(output / "originals.json", lookup)
    return int(not passed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("development", "frozen"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=range(1, 51), default=5)
    args = parser.parse_args()
    return evaluate(args.stage, args.output, args.repetitions)


if __name__ == "__main__":
    raise SystemExit(main())
