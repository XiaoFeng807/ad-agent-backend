"""Freeze and evaluate R3 product attribute retrieval through authenticated SSE."""

import argparse
import json
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

from backend import observability
from backend.database import database
from backend.rag import product_chat_search as chat, product_evidence as rag
from scripts import eval_product_generalization_t1 as t1
from scripts.eval_product_query_expansion_r4 import check_inputs, digest
from scripts.eval_product_r5 import loopback
from scripts.product_evidence import FIXTURES, import_pack, p95, use_sandbox, write_json
from scripts.verify_product_evidence import CHUNKS, login

PACK = t1.PACK / "r3"
ROOT = FIXTURES.parents[1]
FOCUS = {"S02": 0, "S04": 0, "S05": 0, "M05": 0, "M09": 1}
VARIANTS = {"r2": "none", "aliases": "aliases", "split": "parts", "r3": "both"}


def freeze(stage):
    directory = PACK / stage
    pack = t1.read(directory / "manifest.json")
    questions = t1.read(directory / "questions.json")
    refs = {}
    for release in pack["releases"]:
        by_region = {}
        for doc in release["documents"]:
            raw = (directory / "sources" / doc["file"]).read_bytes().decode("utf-8")
            for region in rag.source_regions(doc["kind"], raw):
                ref = {key: region[key] for key in ("location", "start_offset", "end_offset")}
                ref.update(document_id=doc["id"], filename=doc["file"],
                           quote=raw[ref["start_offset"]:ref["end_offset"]])
                by_region[doc["id"], ref["location"]] = ref
        refs[rag._scope(release["target"]), release["version"]] = by_region
    gold = {
        q["id"]: [[refs[rag._scope(q["target"]), q["version"]][tuple(ref)]] for ref in q["required"]]
        for q in questions
    }
    paths = [directory / name for name in ("manifest.json", "questions.json")]
    paths.extend(sorted((directory / "sources").iterdir()))
    config_paths = [ROOT / "backend/rag" / name for name in
                    ("product_attribute_queries.py", "product_query_expansion.py",
                     "product_evidence.py", "product_chat_search.py")]
    return {
        "sources_and_questions": {str(path.relative_to(directory).as_posix()): digest(path) for path in paths},
        "config_code": {str(path.relative_to(ROOT).as_posix()): digest(path) for path in config_paths},
        "model": rag.BGE_ID, "variants": VARIANTS if stage == "development" else {"r3": VARIANTS["r3"]},
        "base": chat._R4_OPTIONS, "production": chat._CPU_OPTIONS, "device": "cpu", "gpu": False,
        "original_slots": rag.DEFAULT_EXPANSION_POLICY.original_slots, "top_k": rag.RECALL_K,
        "rubric": "Exact complete original regions at K=5,20. All required groups for multi. "
                  "Near-part negatives have no gold; check expansion and final citations separately.",
        "gold": gold,
    }


def inputs(stage):
    if stage in ("development", "transfer"):
        directory = PACK / stage
        frozen = t1.read(directory / "freeze.json")
        assert frozen == freeze(stage), f"{stage} freeze mismatch"
        questions = [{**q, "required": frozen["gold"][q["id"]]} for q in t1.read(directory / "questions.json")]
        return directory / "manifest.json", questions
    if stage == "heldout":
        _, questions = t1.check_pack()
        return t1.PACK / "manifest.json", questions
    questions, _, _ = check_inputs("frozen")
    return FIXTURES / "manifest.json", [
        {**q, "expectation": ("denied" if not q["access"] else "supported" if q["has_answer"] else "insufficient")}
        for q in questions
    ]


def existing_rows(path):
    return {row["id"]: row for row in map(json.loads, path.read_text(encoding="utf-8").splitlines())}


def write_row(file, row):
    file.write(json.dumps(row, ensure_ascii=False) + "\n")
    file.flush()


def scope_findings(row):
    ranked = row["ranked"] or {}
    target = {**(row["input"]["target"] or {}), "version": row["input"]["version"]}
    pool = ranked.get("pool", {})
    authorized = {cid for cid, hit in pool.items() if all(hit.get(key) == v for key, v in target.items())}
    committed = row["committed_history"]
    ids_in_order = {
        **ranked.get("full_orders", {}),
        "merged": ranked.get("merged", []),
        "sse": t1.CITATION.findall(row["reply"]),
        "history": t1.CITATION.findall(committed[-1]["content"]) if committed else [],
    }
    return {
        "pool": sorted(set(pool) - authorized),
        **{name: sorted(set(order) - authorized) for name, order in ids_in_order.items()},
    }


def check_candidate(client, auth, row, index, originals):
    ranked = row["ranked"]
    group = row["gold_ranks"][index]["gold"]
    rank = row["gold_ranks"][index]["merged"]
    if rank is None:
        return {"gold_index": index, "ok": False, "reason": "missing_from_top20"}
    cid = ranked["merged"][rank - 1]
    start = perf_counter()
    response = client.get(CHUNKS + cid, headers=auth)
    elapsed = (perf_counter() - start) * 1000
    if response.status_code != 200:
        return {"gold_index": index, "chunk_id": cid, "http": response.status_code, "ms": elapsed, "ok": False}
    actual = response.json()
    original = originals[actual["filename"]]
    chunk_start, chunk_end = actual["start_offset"], actual["end_offset"]
    regions = [{**r, "complete": chunk_start <= r["start_offset"] and r["end_offset"] <= chunk_end}
               for r in rag.source_regions(actual["kind"], original)
               if r["start_offset"] < chunk_end and chunk_start < r["end_offset"]]
    target = row["input"]["target"]
    ok = (cid == actual["chunk_id"] and actual["version"] == row["input"]["version"] and actual["active"]
          and all(actual[key] == val for key, val in target.items()) and actual["original"] == original
          and ranked["pool"][cid] == {key: val for key, val in actual.items() if key != "original"}
          and actual["content"] == original[chunk_start:chunk_end] and regions == actual["locations"]
          and t1.ref_rank(group, {cid: actual}, [cid]) == 1)
    return {"gold_index": index, "rank": rank, "http": response.status_code, "ms": elapsed, "ok": ok, **actual}


def retrieval_table(rows):
    result = {}
    for k in (5, 20):
        ranks = [hit for row in rows for hit in row["gold_ranks"]]
        multi = [row["gold_ranks"] for row in rows if len(row["gold_ranks"]) > 1]

        def found(hit):
            return hit["merged"] is not None and hit["merged"] <= k

        result[f"necessary_at_{k}"] = [sum(map(found, ranks)), len(ranks)]
        result[f"multi_all_at_{k}"] = [sum(all(map(found, group)) for group in multi), len(multi)]
    return result


def compare(rows, reference):
    regressions, changes = [], []
    for row in rows:
        before = reference[row["id"]]
        assert row["input"] == before["input"], row["id"]
        old_ids = (before["ranked"] or {}).get("merged", [])
        new_ids = (row["ranked"] or {}).get("merged", [])
        if old_ids[:5] != new_ids[:5]:
            regressions.append({"id": row["id"], "kind": "original_top5"})
        old_ranks, new_ranks = before["gold_ranks"], row["gold_ranks"]
        for i, (old, new) in enumerate(zip(old_ranks, new_ranks, strict=True)):
            assert old["gold"] == new["gold"]
            for k in (5, 20):
                if old["merged"] and old["merged"] <= k and (not new["merged"] or new["merged"] > k):
                    regressions.append({"id": row["id"], "gold": i, "k": k, "ranks": [old["merged"], new["merged"]]})
        old_checks = [{k: v for k, v in check.items() if k != "http_body"} for check in before["http_readback"]]
        new_checks = [{k: v for k, v in check.items() if k != "http_body"} for check in row["http_readback"]]
        if before["passed"] and (not row["passed"] or row["reply"] != before["reply"] or new_checks != old_checks):
            regressions.append({"id": row["id"], "kind": "behavior_or_citation"})
        changes.append({"id": row["id"], "r2": old_ids, "r3": new_ids,
                        "r2_gold": old_ranks, "r3_gold": new_ranks,
                        "r2_passed": before["passed"], "r3_passed": row["passed"]})
    return regressions, changes


def timed(client, auth, q, traces, audit, variant):
    with patch.object(chat, "_CPU_OPTIONS", {**chat._R4_OPTIONS, "attribute_expansion": VARIANTS[variant]}):
        audit.reset()
        body = {"message": t1.sse_message(q), "chat_view_id": uuid4().hex}
        start = perf_counter()
        with client.stream("POST", "/api/chat/stream", headers=auth, json=body) as response:
            response.raise_for_status()
            lines = response.iter_lines()
            for line in lines:
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event["type"] in t1.TERMINALS:
                        elapsed = (perf_counter() - start) * 1000
                        break
            else:
                raise AssertionError(f"{q['id']}: missing SSE terminal")
            assert event["type"] == "done", event
            for _ in lines:
                pass
        trace = traces.read(event["trace_id"], full=True)
        step = next((s["evidence"] for s in trace["steps"] if s["action"] == "chat_retrieval"), {})
        assert not step.get("eligible") and not step.get("scored_chunks")
        return {"variant": variant, "trace_id": event["trace_id"], "retrieval_ms": step.get("retrieval_ms"),
                "sse_done_ms": elapsed, "query_encodings": audit.encoding}


def timing_summary(samples):
    result = {}
    for field in ("retrieval_ms", "sse_done_ms"):
        paired = [pair for pair in samples if pair["r2"][field] is not None and pair["r3"][field] is not None]
        result[field] = {name: p95([pair[name][field] for pair in paired]) for name in ("r2", "r3")}
        result[field].update(paired_added=p95([p["r3"][field] - p["r2"][field] for p in paired]), n=len(paired))
    result["query_encodings"] = {
        name: sum(pair[name]["query_encodings"] for pair in samples) for name in ("r2", "r3")
    }
    return result


def evaluate(stage, output, reference_dir):
    manifest, questions = inputs(stage)
    output.mkdir(parents=True)
    write_json(output / "run-config.json", {"runtime": t1.runtime(), "model": rag.BGE_ID,
               "cpu_options": chat._CPU_OPTIONS, "r2_options": chat._R4_OPTIONS, "concurrency": 1,
               "manifest_sha256": digest(manifest)})
    use_sandbox(output / "evaluation.sqlite3")
    database.settings.JWT_SECRET = token_urlsafe(48)
    database.init_db()
    from fast_server import app

    audit = t1.RetrievalAudit()
    audit_rankings = True
    contract = t1.read(t1.PACK / "sku-conflict-r1.json")
    originals = {path.name: path.read_bytes().decode("utf-8") for path in (manifest.parent / "sources").iterdir()}

    def capture(service, *args, **kwargs):
        if audit_rankings:
            return audit.capture(service, *args, **kwargs)
        return audit.retrieve(service, *args, **kwargs)

    def count(encoder, texts):
        return audit.count(encoder, texts)

    with (
        closing(database.get_db()) as writer,
        patch.object(observability, "TRACE_LOG_FILE", output / "trace.jsonl"),
        patch.object(rag.ProductEvidence, "retrieve", capture),
        patch.object(rag.BGEEmbedding, "embed_query", count),
        (output / "rows.jsonl").open("w", encoding="utf-8") as journal,
    ):
        users, builds = import_pack(writer, manifest, seed=True)
        write_json(output / "import.json", builds)
        with loopback(app, lifespan="off") as client:
            traces = t1.TraceReader(observability.TRACE_LOG_FILE)
            headers = {principal: login(client, principal) for principal in users}

            def run(q, variant="r3", **kwargs):
                auth = headers[q["principal"]]
                focus = stage == "heldout" and q["id"] in t1.MULTI_INTENT_FOCUS
                opts = {**chat._R4_OPTIONS, "attribute_expansion": VARIANTS[variant]}
                with patch.object(chat, "_CPU_OPTIONS", opts):
                    row = t1.sse(client, auth, q, traces, writer, users[q["principal"]], audit,
                                 refresh_history=True, capture_wire=focus, **kwargs)
                http_samples = []
                get = client.get

                def time_get(path, **options):
                    start = perf_counter()
                    response = get(path, **options)
                    http_samples.append({"path": path, "status": response.status_code,
                                         "ms": (perf_counter() - start) * 1000})
                    return response

                with patch.object(client, "get", time_get):
                    checks = t1.readback(client, auth, row, originals, capture_wire=focus)
                row["citation_http_samples"] = http_samples
                case = contract["cases"].get(q["id"]) if stage == "heldout" else None
                groups = t1.baseline_groups(q) if stage == "baseline" else q.get("required", [])
                t1.score(row, checks, groups, conflict=(case, contract["acceptance"]) if case else None)
                if focus:
                    row["multi_intent_checks"] = t1.focused_checks(row)
                    row["passed"] &= all(row["multi_intent_checks"].values())
                history = row["refreshed_history"]
                row["refresh_matches_commit"] = isinstance(history, dict) or [
                    (e["role"], e["content"]) for e in history[-2:]
                ] == [(e["role"], e["content"]) for e in row["committed_history"]]
                row["scope_leaks"] = scope_findings(row)
                row["passed"] &= row["refresh_matches_commit"] and not any(row["scope_leaks"].values())
                row["variant"] = variant
                if stage == "heldout" and q["id"] in FOCUS:
                    row["candidate_http"] = check_candidate(client, auth, row, FOCUS[q["id"]], originals)
                return row

            def activate(q):
                if q["target"] and q["version"]:
                    rag.activate(writer, q["target"], q["version"])

            runs = {variant: [] for variant in (VARIANTS if stage == "development" else ("r3",))}
            for q in questions:
                activate(q)
                for variant, rows in runs.items():
                    row = run(q, variant)
                    rows.append(row)
                    write_row(journal, row)
                    print(q["id"], variant, [hit["merged"] for hit in row["gold_ranks"]],
                          row["first_deviation"] or "pass", flush=True)

            probes = {}
            if stage == "heldout":
                probes = t1.permission_probes(client, headers, writer, users, run, questions)
                by_id = {q["id"]: q for q in questions}
                group_id = client.post("/api/chat/conversations", headers=headers["owner"],
                                       json={"title": "R3 SKU boundary"}).raise_for_status().json()["data"]["conversation_id"]
                n08 = by_id["N08"]
                activate(n08)
                view = uuid4().hex
                probes["N08_workgroup"] = run(n08, conversation_id=group_id, view=view)
                next_q = {**by_id["V06"], "query": "DRIFT-P12 的宽喷头工作压力是多少？"}
                activate(next_q)
                probes["bound_workgroup_followup"] = run(next_q, conversation_id=group_id, view=view)
                arc = by_id["S04"]
                activate(arc)
                wrong = {**arc, "id": "wrong_same_name_sku", "query": "ARC-480 " + arc["query"],
                         "expectation": "insufficient", "required": []}
                row = probes["same_name_wrong_sku"] = run(wrong)
                row["passed"] = (
                    row["retrieval_trace"].get("status") == "sku_conflict"
                    and row["retrieval_trace"].get("bound_sku") == arc["target"]["sku"]
                    and row["retrieval_trace"].get("mentioned_skus") == ["ARC-480"]
                    and row["retrieval_calls"] == row["encoding_calls"] == 0 and row["history_ok"]
                    and row["done"] and row["refresh_matches_commit"]
                    and "请确认" in row["reply"] and not row["http_readback"]
                )
                write_json(output / "probes.json", probes)
            if stage in ("heldout", "baseline"):
                audit_rankings = False
                samples = []
                with (output / "p95-samples.jsonl").open("w", encoding="utf-8") as sample_file:
                    for q in questions:
                        activate(q)
                        auth = headers[q["principal"]]
                        for variant in ("r2", "r3"):
                            timed(client, auth, q, traces, audit, variant)
                        for repeat in range(5):
                            order = ("r2", "r3") if repeat % 2 == 0 else ("r3", "r2")
                            sample = {"id": q["id"], "round": repeat + 1, "order": order}
                            for variant in order:
                                sample[variant] = timed(client, auth, q, traces, audit, variant)
                            write_row(sample_file, sample)
                            samples.append(sample)
                write_json(output / "p95.json", timing_summary(samples))

    summary = {variant: retrieval_table(rows) for variant, rows in runs.items()}
    rows = runs["r3"]
    summary["final"] = {row["id"]: {
        "complete_citation": row["final_complete"], "missing_explained": row.get("specific_missing_reported"),
        "missing_answers": row["reply"].split("\n\n")[0] if not row["final_complete"] else None,
        "first_failure": row["first_deviation"], "admission_reason": row["retrieval_trace"].get("admission_reason"),
        "passed": row["passed"],
    } for row in rows}
    summary["http_readback"] = [sum(check["ok"] for row in rows for check in row["http_readback"]),
                                sum(len(row["http_readback"]) for row in rows)]
    http_times = [sample["ms"] for row in rows for sample in row["citation_http_samples"]]
    if http_times:
        summary["citation_http_p95_ms"] = p95(http_times)
    passed = all(row["ranking_attested"] and row["done"] and row["history_ok"] and row["refresh_matches_commit"]
                 and not any(row["scope_leaks"].values()) and all(c["ok"] for c in row["http_readback"]) for row in rows)
    if stage in ("development", "transfer"):
        summary["by_category"] = {
            category: {variant: retrieval_table([row for row in batch if row["input"]["category"] == category])
                       for variant, batch in runs.items()}
            for category in dict.fromkeys(q["category"] for q in questions)
        }
        summary["near_part_negatives"] = {variant: {row["id"]: {
            "queries": row["ranked"]["queries"], "citation_ids": t1.ids(row["http_readback"])}
            for row in batch if not row["gold_ranks"]} for variant, batch in runs.items()}
    else:
        reference = existing_rows(reference_dir / stage / "rows.jsonl")
        summary["r2"] = retrieval_table(reference.values())
        old_passes = [row for row in rows if reference[row["id"]]["passed"]]
        summary["previously_passing_still_pass"] = [sum(row["passed"] for row in old_passes), len(old_passes)]
        summary["regressions"], compare_rows = compare(rows, reference)
        write_json(output / "comparison.json", compare_rows)
        passed &= not summary["regressions"]
        if stage == "heldout":
            summary["new_candidates"] = {row["id"]: row["candidate_http"]["ok"] for row in rows if "candidate_http" in row}
            summary["probes"] = {name: probe["passed"] for name, probe in probes.items()}
            summary["other_m05_m09_preserved"] = {
                row["id"]: row["gold_ranks"][1 - FOCUS[row["id"]]]["merged"] is not None
                for row in rows if row["id"] in {"M05", "M09"}
            }
            passed &= all(all(summary[key].values()) for key in ("new_candidates", "probes", "other_m05_m09_preserved"))
        else:
            answers = [row for row in rows if row["input"]["expectation"] == "supported"]
            multi = [row for row in answers if len(row["gold_ranks"]) > 1]
            missing = [row for row in rows if row["input"]["expectation"] == "insufficient"]
            denied = [row for row in rows if row["input"]["expectation"] == "denied"]
            summary["regression_gate_counts"] = {
                "answerable": [sum(r["final_complete"] for r in answers), len(answers)],
                "multi": [sum(r["final_complete"] for r in multi), len(multi)],
                "no_answer_zero_citations": [sum(r["passed"] and not r["http_readback"] for r in missing), len(missing)],
                "denied_before_encoding": [sum(r["passed"] for r in denied), len(denied)],
            }
            passed &= all(got == total for got, total in summary["regression_gate_counts"].values())
    summary["passed"] = passed
    write_json(output / "summary.json", summary)
    return int(not passed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("development", "transfer", "heldout", "baseline"))
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reference-dir", type=Path)
    args = parser.parse_args()
    if args.freeze and args.stage in ("development", "transfer"):
        path = PACK / args.stage / "freeze.json"
        if path.exists():
            parser.error(f"freeze already exists: {path}")
        write_json(path, freeze(args.stage))
        return 0
    if not args.output or (args.stage in ("heldout", "baseline") and not args.reference_dir):
        parser.error("supply --output and, for seen questions, --reference-dir")
    return evaluate(args.stage, args.output.resolve(), args.reference_dir)


if __name__ == "__main__":
    raise SystemExit(main())
