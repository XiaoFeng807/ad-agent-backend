"""Import or activate local catalog releases, or run the frozen evidence comparison."""

import argparse
import json
import math
import sys
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from backend.database import database, listing_operations
from backend.rag import product_evidence as rag

FIXTURES = Path(__file__).resolve().parents[1] / "docs/product-evidence"
GROUPS = {"A": ("fixed", "vector"), "B": ("fixed", "hybrid"),
          "C": ("structured", "vector"), "D": ("structured", "hybrid")}
PASSWORD = "local-product-evidence-fixture"


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def use_sandbox(path):
    database.settings.DIAGNOSIS_MYSQL_ENABLED = False
    database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(path.resolve())


def setup_fixture(conn, pack):
    users = {}
    with conn:
        for principal in ("owner", "other"):
            username = f"product-evidence-{principal}"
            conn.execute("INSERT OR IGNORE INTO users (username, password) VALUES (?, ?)",
                         (username, database.hash_password(PASSWORD)))
            users[principal] = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
        for row in pack["catalog"]:
            listing_operations.seed_test_product(conn, users[row["principal"]], **row["target"])
            conn.execute("UPDATE listing_test_products SET title=? WHERE shop_id=? AND marketplace_id=? AND sku=?",
                         (row["title"], *rag._scope(row["target"])))
    return users


def _read_fact_config(path, releases):
    configured = {}
    if path is None:
        return configured
    for entry in json.loads(path.read_bytes())["releases"]:
        key = rag._scope(entry["target"]), entry["version"]
        if key in configured or key not in releases:
            raise ValueError("fact configuration needs unique releases from the manifest")
        configured[key] = entry["fact_rules"]
    return configured


def import_pack(conn, manifest_path, *, seed=False, fact_config=None, previous_fact_config=None):
    pack = json.loads(manifest_path.read_text(encoding="utf-8"))
    releases = {(rag._scope(r["target"]), r["version"]) for r in pack["releases"]}
    configured = _read_fact_config(fact_config, releases)
    previous = _read_fact_config(previous_fact_config, configured)
    users = setup_fixture(conn, pack) if seed else {}
    built = []
    for release in pack["releases"]:
        begin = perf_counter()
        key = rag._scope(release["target"]), release["version"]
        if key in configured:
            if "fact_rules" in release:
                raise ValueError("release has both embedded and separate fact rules")
            release = {**release, "fact_rules": configured[key]}
        written = rag.import_release(
            conn, {**release, "evidence_rules": release.get("evidence_rules", pack.get("evidence_rules", []))},
            manifest_path.parent / "sources", previous_facts=previous.get(key),
        )
        built.append({"target": release["target"], "version": release["version"],
                      "inserted": written, "build_ms": round((perf_counter() - begin) * 1000, 3)})
    return users, built


def p95(values):
    return round(sorted(values)[math.ceil(.95 * len(values)) - 1], 3)


def rank_locations(hits, ref, *, complete):
    doc_id, location = ref
    return next((rank for rank, hit in enumerate(hits, 1) if hit["document_id"] == doc_id
                 and any(loc["location"] == location and (not complete or loc["complete"])
                         for loc in hit["locations"])), None)


def score_question(question, result, latencies, source_checks):
    if result is None:
        return {**question, "status": "denied", "latency_ms": latencies}
    hits = result["candidates"]
    ranks = []
    for ref in question["required"]:
        complete_rank = rank_locations(hits, ref, complete=True)
        vector_rank = rank_locations(result["channels"]["vector"], ref, complete=True)
        keyword_rank = rank_locations(result["channels"]["keyword"], ref, complete=True)
        failure = None
        if complete_rank is None:
            if tuple(ref) not in result["complete_sources"]:
                failure = "split_source"
            elif vector_rank is None and keyword_rank is None:
                failure = "missed_by_recall"
            else:
                failure = "displaced_by_rrf"
        elif complete_rank > 5:
            failure = "rank_6_to_20"
        ranks.append({"source": ref, "complete_rank": complete_rank,
                      "overlap_rank": rank_locations(hits, ref, complete=False),
                      "vector_rank": vector_rank, "keyword_rank": keyword_rank, "failure": failure})
    expected = (question["target"], question["version"])
    leaks = dict.fromkeys(("cross_shop", "wrong_site", "wrong_sku", "inactive_version"), 0)
    for hit in [*result["pool"], *result["evidence"]]:
        for key, mismatch in zip(leaks, (
            hit["shop_id"] != expected[0]["shop_id"], hit["marketplace_id"] != expected[0]["marketplace_id"],
            hit["sku"] != expected[0]["sku"], hit["version"] != expected[1],
        )):
            leaks[key] += int(mismatch)
    actual_refs = [(row["document_id"], row["location"]) for row in result["evidence"]]
    required = {tuple(ref) for ref in question["required"]}
    evidence_ok = set(actual_refs) == required and len(actual_refs) == len(required)
    return {**question, "status": result["status"], "reason": result["reason"], "latency_ms": latencies,
            "pool_size": len(result["pool"]), "leaks": leaks, "source_ranks": ranks,
            "candidate_chunk_ids": [row["chunk_id"] for row in hits],
            "channel_chunk_ids": {name: [row["chunk_id"] for row in rows]
                                  for name, rows in result["channels"].items()},
            "evidence": result["evidence"], "evidence_matches_gold": evidence_ok,
            "source_checks": source_checks}


def verify_chunk(conn, user_id, candidate, documents):
    source = rag.read_chunk(conn, user_id, candidate["chunk_id"])
    original = documents[candidate["filename"]]
    start, end = candidate["start_offset"], candidate["end_offset"]
    expected_locations = [{**region, "complete": start <= region["start_offset"] and end >= region["end_offset"]}
                          for region in rag.source_regions(candidate["kind"], original)
                          if start < region["end_offset"] and end > region["start_offset"]]
    ok = (source is not None and source == {**candidate, "original": original}
          and source["content"] == original[start:end] and source["locations"] == expected_locations)
    return {"chunk_id": candidate["chunk_id"], "principal_id": user_id,
            **{key: candidate[key] for key in ("shop_id", "marketplace_id", "sku", "version", "document_id", "filename",
                                               "kind", "start_offset", "end_offset", "locations", "content")},
            "original_sha256": sha256(original.encode()).hexdigest(), "ok": ok}


def summarize(rows):
    authorized = [row for row in rows if row["access"]]
    all_ranks = [rank for row in authorized for rank in row["source_ranks"]]
    multi = [row for row in authorized if len(row["required"]) > 1]
    unknown = [row for row in authorized if not row["has_answer"]]
    checks = [check for row in authorized for check in row["source_checks"]]
    result = {}
    for k in (5, 20):
        def found(rank):
            return rank["complete_rank"] is not None and rank["complete_rank"] <= k

        result[f"evidence_at_{k}"] = [sum(map(found, all_ranks)), len(all_ranks)]
        result[f"multi_all_at_{k}"] = [sum(all(map(found, row["source_ranks"])) for row in multi), len(multi)]
    result.update(no_answer_false_citations=sum(bool(row["evidence"]) for row in unknown),
                  no_answer_abstentions=[sum(row["status"] == "insufficient_evidence" for row in unknown), len(unknown)],
                  citations_match_gold=sum(row["evidence_matches_gold"] for row in authorized if row["has_answer"]),
                  answerable_questions=sum(row["has_answer"] for row in authorized),
                  lookup=[sum(check["ok"] for check in checks), len(checks)],
                  p95_ms=p95([ms for row in authorized for ms in row["latency_ms"]]),
                  leaks={key: sum(row["leaks"][key] for row in authorized) for key in authorized[0]["leaks"]},
                  denials=[sum(row["status"] == "denied" for row in rows if not row["access"]),
                           sum(not row["access"] for row in rows)])
    return result


def _search(service, conn, user_id, question, group, *, audit=False):
    strategy, mode = GROUPS[group]
    try:
        return service.search(conn, user_id, question["target"], question["query"],
                              strategy=strategy, mode=mode, audit=audit)
    except listing_operations.ListingNotFound:
        if question["access"]:
            raise
        return None


def _activate_question(conn, question):
    rag.activate(conn, question["target"], question["version"])


def evaluate(output, repetitions):
    if not 1 <= repetitions <= 50:
        raise ValueError("repetitions must be between 1 and 50")
    output.mkdir(parents=True)
    use_sandbox(output / "evaluation.sqlite3")
    database.init_db()
    frozen = (FIXTURES / "frozen.jsonl").read_bytes()
    questions = [json.loads(line) for line in frozen.splitlines()]
    assert 40 <= len(questions) <= 60 and len({q["id"] for q in questions}) == len(questions)
    service = rag.ProductEvidence()
    source_text = {path.name: path.read_bytes().decode("utf-8") for path in (FIXTURES / "sources").iterdir()}
    reports = {group: [] for group in GROUPS}
    lookup = {}
    with closing(database.get_db()) as writer:
        users, builds = import_pack(writer, FIXTURES / "manifest.json", seed=True)
        _, repeat_builds = import_pack(writer, FIXTURES / "manifest.json")
        assert all(row["inserted"] for row in builds) and not any(row["inserted"] for row in repeat_builds)
        for question in questions:
            _activate_question(writer, question)
            user_id = users[question["principal"]]
            with closing(database.get_db(read_only=True)) as conn:
                conn.execute("BEGIN")
                results = {group: _search(service, conn, user_id, question, group, audit=True) for group in GROUPS}
                timings = {group: [] for group in GROUPS}
                for repetition in range(repetitions):
                    # Rotate group order. The query and both keyword indexes were warmed above.
                    order = list(GROUPS)
                    shift = repetition % len(order)
                    for group in order[shift:] + order[:shift]:
                        start = perf_counter()
                        timed = _search(service, conn, user_id, question, group)
                        timings[group].append(round((perf_counter() - start) * 1000, 3))
                        expected = results[group]
                        if expected is not None:
                            expected = {key: value for key, value in expected.items()
                                        if key not in rag.AUDIT_FIELDS}
                        assert timed == expected, (question["id"], group, "unstable ranking")
                for group, result in results.items():
                    if (result is None) == question["access"]:
                        raise AssertionError((question["id"], group, "wrong authorization"))
                    if result is not None and result["version"] != question["version"]:
                        raise AssertionError((question["id"], group, "wrong release"))
                    checks = []
                    for candidate in result["candidates"] if result is not None else ():
                        key = candidate["chunk_id"]
                        if key not in lookup:
                            lookup[key] = verify_chunk(conn, user_id, candidate, source_text)
                        checks.append({"chunk_id": key, "ok": lookup[key]["ok"]})
                    reports[group].append(score_question(question, result, timings[group], checks))
    for group, rows in reports.items():
        write_json(output / f"{group}.json", rows)
    write_json(output / "originals.json", lookup)
    summary = {"model": rag.BGE_ID, "python": sys.version.split()[0], "questions": len(questions),
               "frozen_sha256": sha256(frozen).hexdigest(), "repetitions": repetitions,
               "fixed": {"length": rag.FIXED_LENGTH, "overlap": rag.FIXED_OVERLAP, "unit": "Unicode code points"},
               "candidate_k": rag.RECALL_K, "rrf_k": rag.RRF_K, "builds": builds, "repeat_imports": repeat_builds,
               "groups": {group: summarize(rows) for group, rows in reports.items()}}
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(any(any(g["leaks"].values()) or g["lookup"][0] != g["lookup"][1]
                   or g["no_answer_false_citations"] or g["denials"][0] != g["denials"][1]
                   for g in summary["groups"].values())
               or any(row["status"] == "supported" and not row["evidence_matches_gold"]
                      for rows in reports.values() for row in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    importer = actions.add_parser("import")
    importer.add_argument("--db", type=Path, required=True)
    importer.add_argument("--manifest", type=Path, required=True)
    importer.add_argument("--fact-config", type=Path)
    importer.add_argument("--previous-fact-config", type=Path,
                          help="Expected installed rules when replacing a release's fact configuration")
    importer.add_argument("--seed-demo", action="store_true")
    activator = actions.add_parser("activate")
    activator.add_argument("--db", type=Path, required=True)
    for key in ("shop_id", "marketplace_id", "sku"):
        activator.add_argument("--" + key.replace("_", "-"), required=True)
    activator.add_argument("--version", required=True)
    runner = actions.add_parser("evaluate")
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.action == "evaluate":
        return evaluate(args.output, args.repetitions)
    if args.action == "import" and args.seed_demo and args.db.exists():
        parser.error("--seed-demo requires a new isolated database")
    if args.action == "import" and args.previous_fact_config and not args.fact_config:
        parser.error("--previous-fact-config requires --fact-config")
    if args.action == "activate" and not args.db.is_file():
        parser.error("the database must exist")
    use_sandbox(args.db)
    if args.action == "import":
        database.init_db()
    with closing(database.get_db()) as conn:
        if args.action == "import":
            _, builds = import_pack(conn, args.manifest, seed=args.seed_demo, fact_config=args.fact_config,
                                    previous_fact_config=args.previous_fact_config)
            print(json.dumps(builds, ensure_ascii=False, indent=2))
        else:
            target = {key: getattr(args, key) for key in ("shop_id", "marketplace_id", "sku")}
            rag.activate(conn, target, args.version)
            print(json.dumps({"target": target, "active_version": args.version}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
