"""Evaluate structured BGE query expansion on development or frozen catalog questions."""

import argparse
import json
import sys
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from backend.database import database, listing_operations
from backend.rag import product_evidence as rag
from backend.rag.product_query_expansion import EXPANSIONS, ExpansionPolicy
from scripts.product_evidence import FIXTURES, import_pack, p95, rank_locations, summarize, use_sandbox, verify_chunk, write_json

FROZEN_SHA256 = "fb76bb6616ecbed4729c371919de3cdabdfac93e5d1cc58f90ea01df33a15eba"
SCOPE = ("shop_id", "marketplace_id", "sku", "version")


def search(service, conn, user, question, group, *, audit):
    try:
        return service.search(conn, user, question["target"], question["query"],
                              mode="vector", expansion=group, audit=audit)
    except listing_operations.ListingNotFound:
        return None


def report_row(question, result, baseline, timings, conn, user, lookup, documents):
    if result is None:
        return {**question, "status": "denied", "latency_ms": timings, "encoding_calls": 0,
                "queries": {}, "channel_chunk_ids": {}, "candidate_chunk_ids": [], "evidence": []}
    hits = result["candidates"]
    channels = {name: rows for name, rows in result["channels"].items() if name != "keyword"}
    ranks = [{"source": ref, "complete_rank": rank_locations(hits, ref, complete=True),
              "channel_ranks": {name: rank_locations(rows, ref, complete=True) for name, rows in channels.items()}}
             for ref in question["required"]]
    expected = {**question["target"], "version": question["version"]}
    stages = {"scoring_pool": result["pool"], **channels, "merged_top_20": hits, "citations": result["evidence"]}
    scope_checks = {stage: {key: sum(hit[key] != expected[key] for hit in rows) for key in SCOPE}
                    for stage, rows in stages.items()}
    ids = [hit["chunk_id"] for hit in hits]
    original = [hit["chunk_id"] for hit in baseline["candidates"]]
    displaced = {f"top_{k}": [{"chunk_id": key, "original_rank": i + 1,
                              "merged_rank": ids.index(key) + 1 if key in ids else None}
                             for i, key in enumerate(original[:k]) if key not in ids[:k]] for k in (5, 20)}
    channel_ids = {name: [hit["chunk_id"] for hit in rows] for name, rows in channels.items()}
    checks = []
    for hit in hits:
        key = hit["chunk_id"]
        if key not in lookup:
            lookup[key] = verify_chunk(conn, user, hit, documents)
        checks.append({"chunk_id": key, "ok": lookup[key]["ok"]})
    citation_checks = []
    for citation in result["evidence"]:
        source = rag.read_chunk(conn, user, citation["chunk_id"])
        ok = (source is not None and all(source[key] == citation[key] for key in (*SCOPE, "document_id", "filename"))
              and any(loc["complete"] and all(loc[key] == citation[key] for key in loc) for loc in source["locations"])
              and citation["quote"] == source["original"][citation["start_offset"]:citation["end_offset"]])
        citation_checks.append({"chunk_id": citation["chunk_id"], "location": citation["location"], "ok": ok})
    actual = [(item["document_id"], item["location"]) for item in result["evidence"]]
    gold = list(map(tuple, question["required"]))
    return {**question, "status": result["status"], "reason": result["reason"],
            "missing_sections": result.get("missing_sections", []), "source_ranks": ranks, "latency_ms": timings,
            "queries": result["queries"], "encoding_calls": result["encoding_calls"],
            "scoring_pool_count": len(result["pool"]), "candidate_chunk_ids": ids, "channel_chunk_ids": channel_ids,
            "channel_counts": {name: len(keys) for name, keys in channel_ids.items()},
            "unique_candidate_count": len({key for keys in channel_ids.values() for key in keys}),
            "displaced_original_candidates": displaced, "scope_checks": scope_checks,
            "leaks": {key: sum(stage[key] for stage in scope_checks.values()) for key in SCOPE},
            "source_checks": checks, "evidence": result["evidence"], "citation_checks": citation_checks,
            "evidence_matches_gold": sorted(actual) == sorted(gold)}


def ranked(service, conn, user, question, repetitions):
    results = {group: search(service, conn, user, question, group, audit=True) for group in EXPANSIONS}
    timings = {group: [] for group in EXPANSIONS}
    for i in range(repetitions):
        offset = i % len(EXPANSIONS)
        for group in EXPANSIONS[offset:] + EXPANSIONS[:offset]:
            start = perf_counter()
            timed = search(service, conn, user, question, group, audit=False)
            timings[group].append(round((perf_counter() - start) * 1000, 3))
            audited = results[group]
            assert timed == (None if audited is None else {k: v for k, v in audited.items() if k not in rag.AUDIT_FIELDS}), (
                question["id"], group, "unstable ranking")
    return results, timings


def changes(rows, baseline):
    result = {"new_citations": [], "still_missing_top_20": [], "ranks_6_to_20": []}
    for k in (5, 20):
        result[f"new_top_{k}"] = []
        result[f"lost_top_{k}"] = []
    for row, base in zip(rows, baseline):
        assert row["id"] == base["id"]
        old_cites = {(cite["document_id"], cite["location"], cite["chunk_id"]) for cite in base["evidence"]}
        result["new_citations"].extend({"id": row["id"], **cite} for cite in row["evidence"]
                                       if (cite["document_id"], cite["location"], cite["chunk_id"]) not in old_cites)
        for new, old in zip(row.get("source_ranks", []), base.get("source_ranks", [])):
            assert new["source"] == old["source"]
            before, after = old["complete_rank"], new["complete_rank"]
            change = {"id": row["id"], "source": new["source"], "baseline_rank": before, "rank": after}
            for k in (5, 20):
                if after is not None and after <= k < (before or 21):
                    result[f"new_top_{k}"].append(change)
                if before is not None and before <= k < (after or 21):
                    result[f"lost_top_{k}"].append(change)
            if after is None:
                result["still_missing_top_20"].append(change)
            elif after > 5:
                result["ranks_6_to_20"].append(change)
    return result


def group_summary(rows, baseline):
    summary = summarize(rows)
    summary.update(changes(rows, baseline))
    summary["mean_encoding_calls"] = round(sum(row["encoding_calls"] for row in rows if row["access"])
                                           / sum(row["access"] for row in rows), 3)
    summary["mean_unique_candidates"] = round(sum(row["unique_candidate_count"] for row in rows if row["access"])
                                               / sum(row["access"] for row in rows), 3)
    summary["citation_readback_failures"] = [row["id"] for row in rows for check in row.get("citation_checks", [])
                                            if not check["ok"]]
    return summary


def split_cost(groups):
    split_ids = {row["id"] for row in groups["parts"] if "part_1" in row["queries"]}
    result = {"question_ids": sorted(split_ids)}
    for group, rows in groups.items():
        split = [row for row in rows if row["id"] in split_ids]
        if split:
            result[group] = {"p95_ms": p95([ms for row in split for ms in row["latency_ms"]]),
                             "encoding_calls": sum(row["encoding_calls"] for row in split),
                             "unique_candidates": sum(row["unique_candidate_count"] for row in split)}
    return result


def evaluate(stage, output, repetitions):
    frozen = stage == "frozen"
    data = (FIXTURES / f"{stage}.jsonl").read_bytes()
    digest = sha256(data).hexdigest()
    questions = [json.loads(line) for line in data.splitlines()]
    if frozen:
        assert digest == FROZEN_SHA256, ("unexpected frozen SHA-256", digest)
        assert len(questions) == 54 and sum(len(q["required"]) for q in questions) == 52
    else:
        questions = [{"principal": "owner", "access": True, "has_answer": bool(q["required"]), **q} for q in questions]
    assert len({q["id"] for q in questions}) == len(questions)
    output.mkdir(parents=True)
    use_sandbox(output / "evaluation.sqlite3")
    database.init_db()
    documents = {path.name: path.read_bytes().decode("utf-8") for path in (FIXTURES / "sources").iterdir()}
    policies = {"selected": rag.R1_EXPANSION_POLICY} if frozen else {
        f"{slots}-{'en' if english else 'raw'}": ExpansionPolicy(slots, english)
        for slots in (0, 3, 5, 8, 10) for english in (False, True)
    }
    reports = {name: {group: [] for group in EXPANSIONS} for name in policies}
    lookup = {}
    with closing(database.get_db()) as writer:
        users, builds = import_pack(writer, FIXTURES / "manifest.json", seed=True)
        assert all(build["inserted"] for build in builds)
        for question in questions:
            rag.activate(writer, question["target"], question["version"])
            user = users[question["principal"]]
            with closing(database.get_db(read_only=True)) as conn:
                conn.execute("BEGIN")
                for name, policy in policies.items():
                    service = rag.ProductEvidence(expansion_policy=policy)
                    results, timings = ranked(service, conn, user, question, repetitions)
                    for group, result in results.items():
                        assert (result is not None) == question["access"], (question["id"], group)
                        if result is not None:
                            assert result["version"] == question["version"], (question["id"], group)
                        row = report_row(question, result, results["none"], timings[group], conn, user, lookup, documents)
                        reports[name][group].append(row)
    summary = {"stage": stage, "input_sha256": digest, "questions": len(questions), "model": rag.BGE_ID,
               "python": sys.version.split()[0], "repetitions": repetitions, "candidate_k": rag.RECALL_K,
               "builds": builds, "policies": {}}
    failed = False
    for name, groups in reports.items():
        summaries = {group: group_summary(rows, groups["none"]) for group, rows in groups.items()}
        summary["policies"][name] = {"policy": asdict(policies[name]), "groups": summaries, "split_cost": split_cost(groups)}
        for group, rows in groups.items():
            failed |= any(row["status"] == "supported" and not row["evidence_matches_gold"] for row in rows)
            metrics = summaries[group]
            failed |= (any(metrics["leaks"].values()) or metrics["lookup"][0] != metrics["lookup"][1]
                       or bool(metrics["no_answer_false_citations"] or metrics["citation_readback_failures"]))
            if frozen:
                write_json(output / f"{group}.json", rows)
            print(name, group, *(metrics[key] for key in ("evidence_at_20", "evidence_at_5", "multi_all_at_20",
                                                          "multi_all_at_5", "p95_ms")), flush=True)
    if frozen:
        previous = json.loads((FIXTURES / "results/C.json").read_text(encoding="utf-8"))
        baseline = reports["selected"]["none"]
        mismatches = [new["id"] for old, new in zip(previous, baseline)
                      if (old["id"], old.get("candidate_chunk_ids", [])) != (new["id"], new["candidate_chunk_ids"])]
        summary["baseline_ranking_matches_task044"] = [len(baseline) - len(mismatches), len(baseline)]
        summary["baseline_mismatches"] = mismatches
        failed |= bool(mismatches)
        write_json(output / "originals.json", lookup)
    else:
        write_json(output / "development-ranks.json", reports)
    write_json(output / "summary.json", summary)
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("development", "frozen"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=range(1, 51), default=5)
    args = parser.parse_args()
    return evaluate(args.stage, args.output, args.repetitions)


if __name__ == "__main__":
    raise SystemExit(main())
