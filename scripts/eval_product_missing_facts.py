"""Replay R6 missing-fact cases and compare ordered CPU candidates with R5."""

import argparse
import re
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.database import listing_operations, product_documents
from backend.rag import product_evidence as rag
from scripts import eval_product_generalization_t1 as t1, eval_product_sku_clarification as r5
from scripts.eval_product_fact_rules import CONFIG as R5_CONFIG, FixedEmbedding as R4Embedding
from scripts.product_evidence import write_json
from scripts.verify_product_evidence import CHUNKS

CONFIG = t1.PACK / "r6/drift-fact-config.json"
REFERENCE = t1.PACK / "sku-clarification-r5-results.zip"
FOCUS = {
    "V10": ("柠檬酸除垢浓度", r"\d+(?:\.\d+)?\s*%", ("citric_acid_descaling_concentration",)),
    "V12": ("过冬储存时进水滤篮的处理方法", r"remove the inlet basket|inspecting the screen",
            ("winter_storage_inlet_basket",)),
    "M02": ("替换软管订货号", r"DP-H08|Service hose assembly|Replacement hose order code\t",
            ("replacement_hose_connector_fit", "replacement_hose_order_code")),
    "M12": ("喷头与自行车链罩的最小距离", r"\d+(?:\.\d+)?\s*(?:cm|厘米)",
            ("bike_rinse_distance", "wide_nozzle_pressure")),
}
CONTROLS = {"V09": "V10", "V11": "V12", "M01": "M02", "M11": "M12"}


class FixedEmbedding(R4Embedding):
    terms = (*R4Embedding.terms, "Drain additive", "Filter receptacle", "Cold-storage preparation",
             "Shape alone is insufficient")


def fact_checks(row):
    qid = row["id"]
    if qid not in FOCUS and qid not in CONTROLS:
        return {}
    label, forbidden, facts = FOCUS[CONTROLS.get(qid, qid)]
    missing = qid in FOCUS
    trace = row["retrieval_trace"]
    checks = trace.get("fact_checks", [])
    cites = t1.ids(row["http_readback"])
    saved = row["committed_history"][-1]["content"]
    lead = f"证据不足。{'当前版本' if len(facts) == 1 else '本次检索'}缺少以下依据：{label}。"
    unavailable = {"citric_acid_descaling_concentration", "winter_storage_inlet_basket",
                   "replacement_hose_order_code", "bike_rinse_distance"}
    outcomes = [("missing", "source_not_configured", label) if missing and fact in unavailable
                else ("verified", None, None) for fact in facts]
    required = row["input"]["required"]
    return {
        "version": trace.get("version") == row["input"]["version"],
        "facts_in_question_order": [check["facts"] for check in checks] == [[fact] for fact in facts],
        "outcomes": [(c["status"], c["reason"], c.get("missing")) for c in checks] == outcomes,
        "fact_sources": all(bool(c["required_sources"]) == (c["status"] == "verified")
                            and len(c["chunk_ids"]) == (c["status"] == "verified") for c in checks),
        "final_ids": ([cid for c in checks for cid in c["chunk_ids"]] == trace.get("citation_ids")
                      == row["persistence_trace"]["evidence"]["final_citation_ids"]
                      == row["refreshed_citation_ids"] == cites),
        "http_originals": len(cites) == len(required) and all(
            any(item["ok"] and all(item.get(k) == v for k, v in ref.items())
                for item in row["http_readback"] for ref in group) for group in required),
        "current_status": trace.get("status") == (
            ("partial_support" if required else "insufficient_evidence") if missing else "supported"),
        "same_missing_lead": not missing or all(text.split("\n\n")[0] == lead for text in (row["reply"], saved)),
        "no_missing_value": not missing or all(not re.search(forbidden, text, re.I)
                                               for text in (row["reply"], saved)),
    }


def version_switch(run, activate, before, after, writer, uid, client, auth):
    scope = rag._scope(before["target"])
    activate(before)
    old_chunks = t1.ids(t1.current_pool(writer, uid, before["target"]))
    old_before = {cid: client.get(CHUNKS + cid, headers=auth).status_code for cid in old_chunks}
    activate(after)
    row = run(after)
    new_chunks = t1.ids(t1.current_pool(writer, uid, after["target"]))
    old_after = {cid: client.get(CHUNKS + cid, headers=auth).status_code for cid in old_chunks}
    new_after = {cid: client.get(CHUNKS + cid, headers=auth).status_code for cid in new_chunks}
    row["version_switch"] = {"scope": scope, "from": before["version"], "to": after["version"],
                             "old_before": old_before, "old_after": old_after, "new_after": new_after}
    row["passed"] &= set(old_before.values()) == {200} and set(old_after.values()) == {404} and set(new_after.values()) == {200}
    return row


def extra_probes(fixed, run, activate, questions, writer, users, client, auth, audit):
    if "N08" not in questions:
        return {"version_switch": version_switch(run, activate, questions["Q04"], questions["Q05"],
                                                  writer, users["owner"], client, auth["owner"])}
    group_id = client.post("/api/chat/conversations", headers=auth["owner"], json={"title": "R6 商品资料"}
                           ).raise_for_status().json()["data"]["conversation_id"]
    n08 = questions["N08"]
    activate(n08)
    view = uuid4().hex
    probes = {"N08_workgroup": run(n08, conversation_id=group_id, view=view)}
    follow = {**questions["V06"], "query": "DRIFT-P12 的宽喷头工作压力是多少？"}
    activate(follow)
    follow_row = probes["N08_explicit_followup"] = run(follow, conversation_id=group_id, view=view)
    bindings = [dict(row) for row in writer.execute(
        """SELECT e.id, e.details FROM chat_group_entries e JOIN chat_group_runs r USING (run_id)
           WHERE r.conversation_id=? AND e.details IS NOT NULL""", (group_id,)
    )]
    follow_row["assistant_details"] = bindings
    follow_row["passed"] &= not bindings
    probes["version_switch"] = version_switch(run, activate, questions["V09"], questions["M01"],
                                              writer, users["owner"], client, auth["owner"])
    if not fixed:
        return probes

    def read_candidate(target, version, ref):
        rag.activate(writer, target, version)
        hit, = [row for row in product_documents.scoped_chunks(writer, users["owner"], rag._scope(target), "structured")
                if row["document_id"] == ref["document_id"] and
                any(loc["location"] == ref["location"] and loc["complete"] for loc in rag._hit(row)["locations"])]
        return hit

    def inject(qid, name, foreign, *, outside=True):
        q = questions[qid]
        hit = rag._hit(foreign)
        cid = hit["chunk_id"]

        def retrieve(service, *args, **kwargs):
            result, snapshot = audit.capture(service, *args, **kwargs)
            result["candidates"] = [hit, *(h for h in result["candidates"] if h["chunk_id"] != cid)][:rag.RECALL_K]
            audit.ranked["merged"] = t1.ids(result["candidates"])
            return result, [*snapshot, foreign]

        activate(q)
        with patch.object(rag.ProductEvidence, "retrieve", retrieve):
            row = probes[name] = run(q, injected=[cid], conversation_id=group_id)
        row["injected"] = hit
        row["injected_http"] = client.get(CHUNKS + cid, headers=auth["owner"]).status_code
        row["passed"] &= (row["injected_scope_leaks"] == ([cid] if outside else [])
                          and row["retrieval_trace"]["candidate_ids"][0] == cid
                          and cid not in row["retrieval_trace"]["citation_ids"])
        return row

    missing_to_control = {missing: control for control, missing in CONTROLS.items()}
    manifest = t1.read(t1.PACK / "manifest.json")
    for qid, control_id in missing_to_control.items():
        q, control = questions[qid], questions[control_id]
        target = q["target"]
        ref = q.get("removed_source") or control["required"][0][0]
        old = read_candidate(target, control["version"], ref)
        row = inject(qid, f"{qid}_old_version", old)
        row["passed"] &= row["injected_http"] == 404

        wrong = {**target, "sku": "DRIFT-P30"}
        with writer:
            listing_operations.seed_test_product(writer, users["owner"], **wrong)
        source_release = next(r for r in manifest["releases"] if r["target"] == target and r["version"] == control["version"])
        wrong_version = q["version"]
        rag.import_release(writer, {**source_release, "target": wrong, "version": wrong_version}, t1.PACK / "sources")
        row = inject(qid, f"{qid}_wrong_sku", read_candidate(wrong, wrong_version, ref))
        row["passed"] &= row["injected_http"] == 200

    target = questions["V12"]["target"]
    activate(questions["V12"])
    near = read_candidate(target, "v2", {"document_id": "manual", "location": "page:8"})
    row = inject("V12", "V12_current_filter_inspection", near, outside=False)
    row["passed"] &= row["injected_http"] == 200 and "before inspecting the screen" in near["content"]

    return probes


def compare(rows, previous):
    for row in rows:
        old = previous[row["id"]]
        assert row["input"] == old["input"] and row["request"]["message"] == old["request"]["message"]
        old_top20, new_top20 = [(r["ranked"] or {}).get("merged", []) for r in (old, row)]
        yield {
            "id": row["id"], "r5_passed": old["passed"], "r6_passed": row["passed"],
            "r5_failure": old["first_deviation"], "r6_failure": row["first_deviation"],
            "r5_top20": old_top20, "r6_top20": new_top20, "same_top20": old_top20 == new_top20,
            "same_gold_ranks": row["gold_ranks"] == old["gold_ranks"],
            "same_calls": all(old[k] == row[k] for k in ("retrieval_calls", "encoding_calls")),
            "same_reply_and_history": old["reply"] == row["reply"] and
            [e["content"] for e in old["committed_history"]] == [e["content"] for e in row["committed_history"]],
            "same_http_originals": old["http_readback"] == row["http_readback"],
        }


def evaluate(stage, output, reference):
    fixed = stage == "fixed"
    options = dict(fact_config=CONFIG, previous_fact_config=R5_CONFIG, fixed_embedding=FixedEmbedding,
                   fixed_ids=(*FOCUS, *CONTROLS), acceptance_checks=fact_checks, extra_probes=extra_probes)
    if fixed:
        before, _, _ = r5.evaluate(stage, output / "before", reference, **{**options, "fixed_ids": FOCUS}, fixed_before=True)
        assert all(not row["passed"] and row["first_deviation"] == "citation_admission" for row in before)
    rows, probes, previous = r5.evaluate(stage, output / "after" if fixed else output, reference, **options)
    by_id = {r["id"]: r for r in rows}
    if fixed:
        write_json(output / "before-after.json", {r["id"]: {"before": r, "after": by_id[r["id"]]} for r in before})
        comparison = list(compare([by_id[r["id"]] for r in before], {r["id"]: r for r in before}))
    else:
        comparison = list(compare(rows, previous))
    write_json(output / "comparison.json", comparison)
    retained = [r for r in comparison if r["r5_passed"]]
    retained_count = sum(all(r[key] for key in ("r6_passed", "same_reply_and_history", "same_http_originals", "same_calls"))
                         for r in retained)
    checks = [c for row in rows for c in row["http_readback"]]
    probe_checks = [c for row in probes.values() for c in row["http_readback"]]
    summary = {
        "passed": [sum(r["passed"] for r in rows), len(rows)],
        "r5_passes_retained": [retained_count, len(retained)] if not fixed else None,
        "same_ordered_top20": [sum(r["same_top20"] for r in comparison), len(comparison)],
        "same_gold_ranks": [sum(r["same_gold_ranks"] for r in comparison), len(comparison)],
        "http_verified": [sum(check["ok"] for check in checks), len(checks)],
        "probe_http_verified": [sum(check["ok"] for check in probe_checks), len(probe_checks)],
        "probes": {name: row["passed"] for name, row in probes.items()},
    }
    if stage != "baseline":
        summary["repaired"] = [sum(by_id[qid]["passed"] for qid in FOCUS), len(FOCUS)]
    else:
        categories = {
            "answerable": [r for r in rows if r["input"]["expectation"] == "supported"],
            "multi_evidence": [r for r in rows if len(r["gold_ranks"]) > 1],
            "no_answer_zero_citations": [r for r in rows if r["input"]["expectation"] == "insufficient"],
            "denied_before_encoding": [r for r in rows if r["input"]["expectation"] == "denied"],
        }
        summary.update({name: [sum(r["passed"] for r in items), len(items)] for name, items in categories.items()})
        assert [len(items) for items in categories.values()] == [44, 7, 8, 2]
    summary["ok"] = (summary["passed"][0] == summary["passed"][1] == {"fixed": 8, "heldout": 48, "baseline": 54}[stage]
                     and all(summary["probes"].values()) and all(c["ok"] for c in [*checks, *probe_checks])
                     and all(r["same_top20"] and r["same_gold_ranks"] for r in comparison)
                     and (fixed or retained_count == len(retained) == (44 if stage == "heldout" else 54)))
    write_json(output / "summary.json", summary)
    assert summary["ok"], summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("fixed", "heldout", "baseline"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    args = parser.parse_args()
    evaluate(args.stage, args.output.resolve(), args.reference)
