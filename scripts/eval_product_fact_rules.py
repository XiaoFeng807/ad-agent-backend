"""Replay R4 fact admission through authenticated product SSE with GPU disabled."""

import argparse
import json
import re
from contextlib import ExitStack, closing
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch
from uuid import uuid4

from backend import observability
from backend.agent import product_evidence_reply
from backend.database import database, listing_operations, product_documents
from backend.rag import product_evidence as rag
from scripts import eval_product_generalization_t1 as t1, eval_product_recall_r3 as r3
from scripts.eval_product_query_expansion_r4 import digest
from scripts.eval_product_r5 import loopback
from scripts.product_evidence import import_pack, use_sandbox, write_json
from scripts.verify_product_evidence import CHUNKS, login

PACK = t1.PACK / "r4"
CONFIG = PACK / "drift-fact-config.json"
TRANSFER = PACK / "transfer"
FOCUS = {
    "M05": ("reservoir_rated_volume", "continuous_spray_duration", None, None),
    "M06": ("reservoir_rated_volume", "continuous_spray_duration", "连续喷洗时长", r"\d+\s*(?:minutes?|分钟)"),
    "M09": ("pump_inlet_temperature", "replacement_hose_order_code", None, None),
    "M10": ("pump_inlet_temperature", "replacement_hose_order_code", "替换软管订货号", r"DP-H08|Service hose assembly"),
}


class FixedEmbedding:
    terms = ("Reservoir rated volume\t", "Continuous spray duty cycle",
             "Working water temperature", "Replacement hose order code\t",
             "Wide nozzle, pump running", "Bike rinse distance")

    def __init__(self, *, device):
        assert device == "cpu"
        self._device = device

    def __call__(self, texts):
        return [[int(term in text) for term in self.terms] for text in texts]

    def embed_query(self, texts):
        return [[1] * len(self.terms) for _ in texts]


def transfer_freeze():
    paths = [TRANSFER / "manifest.json", TRANSFER / "questions.json", *sorted((TRANSFER / "sources").iterdir())]
    return {"files": {path.relative_to(TRANSFER).as_posix(): digest(path) for path in paths},
            "model": rag.BGE_ID, "device": "cpu", "gpu": False, "top_k": rag.RECALL_K}


def focus_checks(row, spec):
    first, second, missing, forbidden = spec
    checks = row["retrieval_trace"].get("fact_checks", [])
    cites = t1.ids(row["http_readback"])
    saved = row["committed_history"][-1]["content"]
    prefix = f"证据不足。本次检索缺少以下依据：{missing}。\n\n" if missing else ""
    return {
        "facts_in_question_order": [check["facts"] for check in checks] == [[first], [second]],
        "fact_outcomes": [(check["status"], check["reason"]) for check in checks] == [
            ("verified", None), ("missing", "source_not_configured") if missing else ("verified", None)],
        "citation_ids": ([cid for check in checks for cid in check["chunk_ids"]]
                         == row["retrieval_trace"]["citation_ids"] == t1.CITATION.findall(saved) == cites),
        "ordered_originals": len(cites) == len(row["input"]["required"]) and all(
            source["ok"] and any(all(source[k] == v for k, v in ref.items()) for ref in group)
            for source, group in zip(row["http_readback"], row["input"]["required"])
        ),
        "status": row["retrieval_trace"]["status"] == ("partial_support" if missing else "supported"),
        "missing_prompt": row["reply"].startswith(prefix) and saved.startswith(prefix),
        "missing_value_absent": not forbidden or all(not re.search(forbidden, text, re.I) for text in (row["reply"], saved)),
    }


def comparison(rows, reference):
    result = []
    for row in rows:
        before = reference[row["id"]]
        assert row["input"] == before["input"], row["id"]
        old, new = [(item["ranked"] or {}).get("merged", []) for item in (before, row)]
        old_http, new_http = [[{k: v for k, v in check.items() if k != "http_body"}
                               for check in item["http_readback"]] for item in (before, row)]
        result.append({"id": row["id"], "r3_top20": old, "r4_top20": new, "same_top20": old == new,
                       "r3_passed": before["passed"], "r4_passed": row["passed"],
                       "r3_failure": before["first_deviation"], "r4_failure": row["first_deviation"],
                       "r3_admission": before["retrieval_trace"].get("admission_reason"),
                       "r4_admission": row["retrieval_trace"].get("admission_reason"),
                       "r3_citations": t1.ids(old_http), "r4_citations": t1.ids(new_http),
                       "same_gold_ranks": before["gold_ranks"] == row["gold_ranks"],
                       "same_behavior": row["passed"] and old_http == new_http and row["reply"] == before["reply"]
                       and [entry["content"] for entry in row["committed_history"]]
                       == [entry["content"] for entry in before["committed_history"]]})
    return result


def evaluate(stage, output, reference):
    fixed = stage == "fixed"
    assert database.settings.PRODUCT_R4_CPU_ENABLED and not database.settings.PRODUCT_RERANK_ENABLED
    runtime = {"embedding": "fixed", "device": "cpu"} if fixed else t1.runtime()
    if stage == "transfer":
        assert t1.read(TRANSFER / "freeze.json") == transfer_freeze()
        manifest = TRANSFER / "manifest.json"
        questions = t1.read(TRANSFER / "questions.json")
    else:
        manifest, questions = r3.inputs("heldout" if fixed else stage)
    by_id = {q["id"]: q for q in questions}
    output.mkdir(parents=True)
    write_json(output / "run-config.json", {"runtime": runtime, "options": r3.chat._CPU_OPTIONS,
               "model": rag.BGE_ID, "gpu": False, "manifest_sha256": digest(manifest),
               "fact_config_sha256": digest(CONFIG) if stage in ("heldout", "fixed") else None})
    use_sandbox(output / "evaluation.sqlite3")
    database.settings.JWT_SECRET = token_urlsafe(48)
    database.init_db()
    from fast_server import app

    originals = {path.name: path.read_bytes().decode("utf-8") for path in (manifest.parent / "sources").iterdir()}
    conflicts = t1.read(t1.PACK / "sku-conflict-r1.json")
    with ExitStack() as stack:
        writer = stack.enter_context(closing(database.get_db()))
        stack.enter_context(patch.object(observability, "TRACE_LOG_FILE", output / "trace.jsonl"))
        if fixed:
            stack.enter_context(patch.object(rag, "BGEEmbedding", FixedEmbedding))
        audit = t1.RetrievalAudit()
        def capture(service, *args, **kwargs):
            return audit.capture(service, *args, **kwargs)

        def count(encoder, texts):
            return audit.count(encoder, texts)

        stack.enter_context(patch.object(rag.ProductEvidence, "retrieve", capture))
        stack.enter_context(patch.object(rag.BGEEmbedding, "embed_query", count))
        users, _ = import_pack(writer, manifest, seed=True, fact_config=CONFIG if stage in ("heldout", "fixed") else None)
        client = stack.enter_context(loopback(app, lifespan="off"))
        auth = {principal: login(client, principal) for principal in users}
        traces = t1.TraceReader(observability.TRACE_LOG_FILE)

        def activate(q):
            if q["target"] and q["version"]:
                rag.activate(writer, q["target"], q["version"])

        def request(q, **kwargs):
            principal = q["principal"]
            return t1.sse(client, auth[principal], q, traces, writer, users[principal], audit,
                          refresh_history=True, capture_wire=True, **kwargs)

        def run(q, *, injected_id=None, **kwargs):
            row = request(q, **kwargs)
            principal = q["principal"]
            checks = t1.readback(client, auth[principal], row, originals, capture_wire=True)
            case = conflicts["cases"].get(q["id"])
            groups = t1.baseline_groups(q) if stage == "baseline" else q.get("required", [])
            t1.score(row, checks, groups, conflict=(case, conflicts["acceptance"]) if case else None)
            row["scope_leaks"] = r3.scope_findings(row)
            if injected_id:
                row["injected_candidate_outside_scope"] = injected_id in row["scope_leaks"]["merged"]
                row["scope_leaks"]["merged"] = [cid for cid in row["scope_leaks"]["merged"] if cid != injected_id]
            refresh = row["refreshed_history"]
            if isinstance(refresh, dict):
                run_id = row["sse_events"][-1]["run_id"]
                messages = [(e["kind"], e["content"]) for e in refresh["entries"] if e["run_id"] == run_id]
            else:
                messages = [(e["role"], e["content"]) for e in refresh[-2:]]
            row["refresh_matches_commit"] = messages == [(e["role"], e["content"]) for e in row["committed_history"]]
            spec = FOCUS.get(q["id"]) or q.get("fact_expectations")
            if spec:
                row["fact_assertions"] = focus_checks(row, spec)
                row["passed"] &= row["all_top20"] and all(row["fact_assertions"].values())
                if not row["all_top20"] and not row["first_deviation"]:
                    row["first_deviation"] = "merge_top20"
            row["passed"] &= row["refresh_matches_commit"] and not any(row["scope_leaks"].values())
            return row

        rows = []
        with (output / "rows.jsonl").open("w", encoding="utf-8") as journal:
            for q in ([by_id[key] for key in FOCUS] if fixed else questions):
                activate(q)
                row = run(q)
                rows.append(row)
                r3.write_row(journal, row)
                print(q["id"], "pass" if row["passed"] else row["first_deviation"] or "assertions", flush=True)

        probes = {}
        if fixed or stage == "heldout":
            group = client.post("/api/chat/conversations", headers=auth["owner"], json={"title": "R4 商品资料"})
            group_id = group.raise_for_status().json()["data"]["conversation_id"]
            n08 = by_id["N08"]
            activate(n08)
            view = uuid4().hex
            probes["N08_chat"] = run(n08)
            probes["N08_workgroup"] = run(n08, conversation_id=group_id, view=view)
            follow = {**by_id["V06"], "query": "DRIFT-P12 的宽喷头工作压力是多少？"}
            probes["N08_workgroup_followup"] = run(follow, conversation_id=group_id, view=view)
        if fixed:
            probes.update(fixed_probes(writer, users["owner"], by_id, request, run, activate, audit, group_id))
        elif stage == "heldout":
            probes.update(t1.permission_probes(client, auth, writer, users, run, questions))
        write_json(output / "probes.json", probes)

    summary = {"stage": stage, "passed": [row["id"] for row in rows if row["passed"]],
               "failures": {row["id"]: row["first_deviation"] for row in rows if not row["passed"]},
               "http_verified": [sum(check["ok"] for row in rows for check in row["http_readback"]),
                                 sum(len(row["http_readback"]) for row in rows)],
               "probes": {name: row["passed"] for name, row in probes.items()}}
    if stage in ("heldout", "baseline"):
        previous = r3.existing_rows(reference / stage / "rows.jsonl")
        compare = comparison(rows, previous)
        write_json(output / "comparison.json", compare)
        if stage == "heldout":
            saved = {key: previous[key] for key in FOCUS}
            write_json(output / "focus-before-after.json",
                       {row["id"]: {"r3": saved[row["id"]], "r4": row} for row in rows if row["id"] in FOCUS})
        summary.update(top20_unchanged=sum(row["same_top20"] for row in compare),
                       gold_ranks_unchanged=sum(row["same_gold_ranks"] for row in compare),
                       previous_passes_retained=sum(row["r3_passed"] and row["same_behavior"] for row in compare))
        expected = {key for key, row in previous.items() if row["passed"]} | (set(FOCUS) if stage == "heldout" else set())
        passed = (
            set(summary["passed"]) == expected
            and all(row["same_top20"] and row["same_gold_ranks"] for row in compare)
            and all(row["same_behavior"] for row in compare if row["r3_passed"])
            and all(row["r3_failure"] == row["r4_failure"] and row["r3_admission"] == row["r4_admission"]
                    for row in compare if row["id"] not in expected)
        )
        if stage == "baseline":
            for key, items in {
                "answerable": [r for r in rows if r["input"]["expectation"] == "supported"],
                "multi": [r for r in rows if len(r["gold_ranks"]) > 1],
                "no_answer": [r for r in rows if r["input"]["expectation"] == "insufficient"],
                "denied_before_encoding": [r for r in rows if r["input"]["expectation"] == "denied"],
            }.items():
                summary[key] = [sum(r["passed"] for r in items), len(items)]
    else:
        passed = all(row["passed"] for row in rows)
    summary["ok"] = passed and all(summary["probes"].values()) and len(set(summary["http_verified"])) == 1
    write_json(output / "summary.json", summary)
    assert summary["ok"], summary


def fixed_probes(writer, uid, questions, request, run, activate, audit, group_id):
    m05, m06, m09, m10 = (questions[key] for key in FOCUS)
    probes = {}

    def source(target, version, document, location):
        rag.activate(writer, target, version)
        hit, = [hit for hit in map(rag._hit, product_documents.scoped_chunks(
            writer, uid, rag._scope(target), "structured"
        )) if hit["document_id"] == document and any(loc["location"] == location for loc in hit["locations"])]
        return hit

    def inject(name, q, foreign):
        def retrieve(service, *args, **kwargs):
            result, snapshot = audit.capture(service, *args, **kwargs)
            candidates = [foreign, *(h for h in result["candidates"] if h["chunk_id"] != foreign["chunk_id"])]
            result["candidates"] = candidates[:rag.RECALL_K]
            audit.ranked["merged"] = t1.ids(result["candidates"])
            return result, snapshot

        activate(q)
        with patch.object(rag.ProductEvidence, "retrieve", retrieve):
            row = probes[name] = run(q, injected_id=foreign["chunk_id"])
        cid = foreign["chunk_id"]
        row["injected"] = foreign
        row["passed"] &= (cid in row["retrieval_trace"]["candidate_ids"]
                          and cid not in row["retrieval_trace"]["citation_ids"]
                          and cid not in t1.CITATION.findall(row["committed_history"][-1]["content"]))

    target = m05["target"]
    activate(m10)
    probes["partial_workgroup"] = run(m10, conversation_id=group_id)
    joined = {**m05, "query": "接满能装几升和能连续喷几分钟？"}
    activate(joined)
    probes["joined_question"] = run(joined)
    inject("old_version_duration", m06, source(target, "v1", "manual", "page:7"))
    inject("old_version_hose", m10, source(target, "v1", "specs", "row:4"))
    probe_dir = PACK / "probes"
    wrong = {**target, "sku": "DRIFT-P30"}
    near = t1.read(probe_dir / "manifest.json")
    with writer:
        listing_operations.seed_test_product(writer, uid, **wrong)
    rag.import_release(writer, near["wrong_sku"], probe_dir / "sources")
    inject("wrong_sku_hose", m10, source(wrong, m10["version"], "parts", "row:2"))

    rag.import_release(writer, near["near_parts"], probe_dir / "sources")
    partial = {**m10, "version": "v2-connectors-only"}
    for location in ("row:2", "row:3"):
        inject(f"near_part_{location}", partial, source(target, partial["version"], "parts", location))
        probes[f"near_part_{location}"]["passed"] &= not re.search(
            r"DP-C12|DP-E45", probes[f"near_part_{location}"]["reply"])

    # Repeat one fact and add an unsupported subquestion to the same request.
    activate(m05)
    repeated = request({**m05, "query": "接满能装几升？接满能装几升？高海拔运行电流是多少？"})
    checks = repeated["retrieval_trace"]["fact_checks"]
    cites = repeated["retrieval_trace"]["citation_ids"]
    repeated["passed"] = (
        repeated["done"] and repeated["reply"].startswith("证据不足。本次检索缺少以下依据：高海拔运行电流是多少。")
        and [c["status"] for c in checks] == ["verified", "verified", "missing"]
        and checks[-1]["reason"] == "unsupported_question" and len(cites) == 1
        and cites == checks[0]["chunk_ids"] == checks[1]["chunk_ids"] == t1.CITATION.findall(repeated["reply"])
        == t1.CITATION.findall(repeated["committed_history"][-1]["content"])
    )
    probes["dedup_and_unknown"] = repeated

    admit = rag.admit_evidence

    def ambiguous(result, snapshot, query, **options):
        legacy, facts = rag._rule_sets(snapshot[0]["evidence_rules"])
        temperature = next(f for f in facts if f["key"] == "pump_inlet_temperature")
        other = {**temperature, "key": "other_temperature"}
        snapshot = [{**dict(row), "evidence_rules": rag._json({"legacy": legacy, "facts": [*facts, other]})}
                    for row in snapshot]
        return admit(result, snapshot, query, **options)

    activate(m09)
    with patch.object(rag, "admit_evidence", ambiguous):
        conflict = request(m09)
    conflict["passed"] = (
        conflict["done"] and conflict["reply"].startswith("请澄清以下子问题具体指哪项规格：最多能接多热的水。")
        and [c["status"] for c in conflict["retrieval_trace"]["fact_checks"]] == ["ambiguous", "verified"]
        and "DP-H08" in conflict["reply"] and "35 °C" not in conflict["reply"]
        and t1.CITATION.findall(conflict["reply"]) == t1.CITATION.findall(conflict["committed_history"][-1]["content"])
    )
    probes["ambiguous_retains_verified"] = conflict

    search = product_evidence_reply.search

    def switch_before_commit(*args, **kwargs):
        result = search(*args, **kwargs)
        with closing(database.get_db()) as conn:
            rag.activate(conn, target, "v1")
        return result

    for label, options in (("chat", {}), ("workgroup", {"conversation_id": group_id})):
        activate(m06)
        with patch.object(product_evidence_reply, "search", switch_before_commit):
            row = probes[f"switch_before_commit_{label}"] = request(m06, **options)
        row["passed"] = (
            row["retrieval_trace"]["status"] == "partial_support"
            and row["sse_events"][-1].get("error_code") == "product_evidence_scope_changed"
            and not row["reply"] and all(entry["role"] != "assistant" for entry in row["committed_history"])
            and row["persistence_trace"]["evidence"]["final_citation_ids"] == []
        )
    return probes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("fixed", "heldout", "baseline", "freeze-transfer", "transfer"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    if args.stage == "freeze-transfer":
        with (TRANSFER / "freeze.json").open("x", encoding="utf-8") as file:
            json.dump(transfer_freeze(), file, ensure_ascii=False, indent=2)
            file.write("\n")
        return
    if args.output is None or (args.stage in ("heldout", "baseline") and args.reference is None):
        parser.error("evaluation needs --output, and regression stages also need --reference")
    evaluate(args.stage, args.output.resolve(), args.reference)


if __name__ == "__main__":
    main()
