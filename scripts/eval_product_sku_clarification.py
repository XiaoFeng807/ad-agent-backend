"""Replay R5 SKU clarification and compare CPU BGE results with the R4 archive."""

import argparse
import json
import re
import subprocess
from contextlib import ExitStack, closing
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from backend import observability
from backend.database import database, listing_operations
from backend.rag import product_evidence as rag
from scripts import eval_product_generalization_t1 as t1, eval_product_recall_r3 as r3
from scripts.eval_product_fact_rules import CONFIG, FOCUS, focus_checks
from scripts.eval_product_r5 import loopback
from scripts.product_evidence import import_pack, use_sandbox, write_json
from scripts.verify_product_evidence import CHUNKS, login

CLARIFICATIONS = ("S03", "S06", "S09", "S12")
TITLE = "ArcTrail 折叠工作灯"
ARC_OPTIONS = [{"title": TITLE, "sku": sku} for sku in ("ARC-220", "ARC-480")]
CATALOG_OPTIONS = [*ARC_OPTIONS, {"title": "DriftPod 旅行冲洗泵", "sku": "DRIFT-P12"}]
EMPTY = "当前范围没有可选商品，请检查店铺和站点。"


class FixedEmbedding:
    def __init__(self, *, device):
        assert device == "cpu"
        self._device = device

    def __call__(self, texts):
        return [[int("Internal battery capacity\t" in text or "Maximum working pressure\t" in text)]
                for text in texts]

    def embed_query(self, texts):
        return [[1] for _ in texts]


def clarification(row, options):
    trace = row["retrieval_trace"]
    reply = row["reply"]
    lead, _, listed = reply.partition("\n\n")
    messages = row["committed_history"]
    return (
        row["done"] and row["refresh_matches_commit"] and row["history_ok"]
        and (lead.startswith("请选择一个 SKU") if options else reply == EMPTY)
        and listed.splitlines() == [f"- {item['title']} · {item['sku']}" for item in options]
        and messages[-1]["content"] == reply
        and trace.get("status") == "sku_clarification" and trace.get("admission_reason") == "sku_missing"
        and trace.get("blocked_at") == "before_retrieval" and trace.get("sku_options") == options
        and "selected_sku" in trace and trace["selected_sku"] is None
        and trace.get("citation_ids") == trace.get("candidate_ids") == []
        and row["persistence_trace"]["evidence"]["final_citation_ids"] == []
        and not row["http_readback"] and row["ranked"] is None
        and row["encoding_calls"] == row["retrieval_calls"] == 0
    )


def compare(rows, previous):
    comparisons = {}
    for row in rows:
        old = previous[row["id"]]
        assert row["input"] == old["input"] and row["request"]["message"] == old["request"]["message"]
        old_ids, new_ids = [t1.ids(item["http_readback"]) for item in (old, row)]
        comparisons[row["id"]] = {
            "r4_passed": old["passed"], "r5_passed": row["passed"],
            "r4_failure": old["first_deviation"], "r5_failure": row["first_deviation"],
            "same_gold_ranks": row["gold_ranks"] == old["gold_ranks"],
            "same_top20": (row["ranked"] or {}).get("merged", []) == (old["ranked"] or {}).get("merged", []),
            "r4_citations": old_ids, "r5_citations": new_ids,
            "same_behavior": row["reply"] == old["reply"] and old_ids == new_ids
            and [e["content"] for e in row["committed_history"]] == [e["content"] for e in old["committed_history"]]
            and row["retrieval_calls"] == old["retrieval_calls"] and row["encoding_calls"] == old["encoding_calls"],
        }
    return comparisons


def fixed_probes(run, activate, questions, writer, users, client, auth, audit, originals, output, group):
    rows = {}
    unbound = questions["S03"]
    shop, site = (unbound["unbound_scope"][key] for key in ("shop_id", "marketplace_id"))
    owner, other = users["owner"], users["other"]
    for principal, other_shop, other_site in ((owner, shop, "CA"), (other, "t1-outlet", "US")):
        with writer:
            for sku in ("ARC-220", "ARC-999"):
                listing_operations.seed_test_product(writer, principal, shop_id=other_shop,
                                                     marketplace_id=other_site, sku=sku)
                writer.execute("UPDATE listing_test_products SET title=? WHERE shop_id=? AND marketplace_id=? AND sku=?",
                               (TITLE, other_shop, other_site, sku))
    rows["foreign_shop_and_site"] = run(unbound)
    rows["unauthorized_scope"] = run({**unbound, "principal": "other"}, options=[])
    rows["missing_scope"] = run({**unbound, "unbound_scope": {"shop_id": shop, "marketplace_id": "JP"}}, options=[])

    with writer:
        listing_operations.seed_test_product(writer, owner, shop_id=shop, marketplace_id="GB", sku="ARC-220")
        writer.execute("UPDATE listing_test_products SET title=? WHERE shop_id=? AND marketplace_id='GB'", (TITLE, shop))
    rows["one_option_waits"] = run({**unbound, "unbound_scope": {"shop_id": shop, "marketplace_id": "GB"}},
                                   options=ARC_OPTIONS[:1])

    selected = {}
    for qid, value in (("S01", "2200"), ("S02", "4800")):
        q = {**questions[qid], "query": unbound["query"]}
        sku = q["target"]["sku"]
        selected[sku] = q
        activate(q)
        view = uuid4().hex
        rows[f"prompt_{sku}"] = run(unbound, conversation_id=group, view=view)
        row = rows[f"select_{sku}"] = run(q, conversation_id=group, view=view)
        opposite = "4800" if value == "2200" else "2200"
        row["passed"] &= (row["ranked"]["query"] == unbound["query"]
                          and row["ranked"]["target"] == q["target"]
                          and len(row["http_readback"]) == 1
                          and re.search(rf"{value}\s+mAh", row["http_readback"][0]["quote"]) is not None
                          and re.search(rf"{opposite}\s+mAh", row["reply"]) is None)

    arc220 = selected["ARC-220"]
    view = uuid4().hex
    rows["prompt_before_version_change"] = run(unbound, view=view)
    release = next(r for r in t1.read(t1.PACK / "manifest.json")["releases"] if r["target"] == arc220["target"])
    filename = "arc220-r5-version-probe.tsv"
    originals[filename] = originals["arc220-specs.tsv"].replace("2200\tmAh", "2300\tmAh")
    (output / filename).write_bytes(originals[filename].encode("utf-8"))
    rag.import_release(writer, {**release, "version": "r5-version-probe",
                               "documents": [{"id": "specs", "kind": "specs", "file": filename}],
                               "evidence_rules": release["evidence_rules"][:1]}, output)
    newer = {**arc220, "version": "r5-version-probe", "required": [[{"document_id": "specs", "location": "row:2"}]]}
    activate(newer)
    row = rows["select_after_version_change"] = run(newer, view=view)
    old_id = rows["select_ARC-220"]["http_readback"][0]["chunk_id"]
    row["previous_citation_http"] = client.get(CHUNKS + old_id, headers=auth["owner"]).status_code
    row["passed"] &= (row["ranked"]["version"] == newer["version"] and "2300\tmAh" in row["reply"]
                      and "2200\tmAh" not in row["reply"] and row["previous_citation_http"] == 404)

    rows["prompt_before_transfer"] = run(unbound, conversation_id=group, view=view)
    with writer:
        writer.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?", (other, shop, site))
    denied = {**arc220, "expectation": "denied", "required": []}
    rows["select_after_transfer"] = run(denied, conversation_id=group, view=view)
    with writer:
        writer.execute("UPDATE listing_test_shops SET user_id=? WHERE shop_id=? AND marketplace_id=?", (owner, shop, site))
    rows["explicit_unknown"] = run({**denied, "target": {**denied["target"], "sku": "UNKNOWN"}})
    rows["explicit_unauthorized"] = run({**denied, "target": {**denied["target"], "shop_id": "t1-outlet"}})

    rows["prompt_before_revocation"] = run(unbound, view=view)
    stale = auth["owner"]
    auth["owner"] = login(client, "owner")
    audit.reset()
    body = {"message": t1.sse_message(arc220), "chat_view_id": view}
    response = client.post("/api/chat/stream", headers=stale, json=body)
    rows["select_after_revocation"] = {
        "request": body, "sse_http": response.status_code, "body": response.text,
        "encoding_calls": audit.encoding, "retrieval_calls": audit.retrievals,
        "passed": response.status_code == 401 and audit.encoding == audit.retrievals == 0,
    }

    n08 = questions["N08"]
    activate(n08)
    for label, conversation_id in (("chat", None), ("workgroup", group)):
        rows[f"N08_{label}"] = run(n08, conversation_id=conversation_id)
        follow = {**questions["V06"], "query": "DRIFT-P12 的宽喷头工作压力是多少？"}
        rows[f"N08_bound_{label}"] = run(follow, conversation_id=conversation_id)

    history = client.get(f"/api/chat/conversations/{group}/history", headers=auth["owner"]).raise_for_status().json()["data"]
    bindings = [dict(entry) for entry in writer.execute(
        """SELECT e.id, e.details FROM chat_group_entries e JOIN chat_group_runs r USING (run_id)
           WHERE r.conversation_id=? AND e.details IS NOT NULL""", (group,)
    )]
    rows["no_auto_binding"] = {"history": history, "assistant_details": bindings, "passed": not bindings}
    return rows


def evaluate(stage, output, reference, *, browser=False, fact_config=CONFIG, fixed_embedding=FixedEmbedding,
             fixed_ids=CLARIFICATIONS, acceptance_checks=None, extra_probes=None, previous_fact_config=None,
             fixed_before=False):
    fixed = stage == "fixed"
    dataset = "heldout" if fixed else stage
    assert database.settings.PRODUCT_R4_CPU_ENABLED and not database.settings.PRODUCT_RERANK_ENABLED
    runtime = {"embedding": "fixed", "device": "cpu"} if fixed else t1.runtime()
    manifest, questions = r3.inputs(dataset)
    with ZipFile(reference) as archive:
        previous = {row["id"]: row for row in map(json.loads, archive.read(f"{dataset}/rows.jsonl").splitlines())}
    by_id = {q["id"]: q for q in questions}
    output.mkdir(parents=True)
    write_json(output / "run-config.json", {"stage": stage, "runtime": runtime, "model": rag.BGE_ID,
               "gpu": False, "manifest_sha256": t1.digest(manifest), "reference_sha256": t1.digest(reference),
               "fact_config_sha256": t1.digest(fact_config) if dataset == "heldout" else None,
               "previous_fact_config_sha256": t1.digest(previous_fact_config) if previous_fact_config else None,
               "fixed_before": fixed_before})
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
            stack.enter_context(patch.object(rag, "BGEEmbedding", fixed_embedding))
        audit = t1.RetrievalAudit()

        def capture(service, *args, **kwargs):
            return audit.capture(service, *args, **kwargs)

        def count(encoder, texts):
            return audit.count(encoder, texts)

        stack.enter_context(patch.object(rag.ProductEvidence, "retrieve", capture))
        stack.enter_context(patch.object(rag.BGEEmbedding, "embed_query", count))
        gpu = stack.enter_context(patch.object(r3.chat, "get_queue"))
        config = fact_config if dataset == "heldout" else None
        if previous_fact_config is not None and config is not None:
            users, _ = import_pack(writer, manifest, seed=True, fact_config=previous_fact_config)
            if not fixed_before:
                import_pack(writer, manifest, fact_config=config, previous_fact_config=previous_fact_config)
        else:
            users, _ = import_pack(writer, manifest, seed=True, fact_config=config)
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

        def run(q, *, options=CATALOG_OPTIONS, injected=(), **kwargs):
            row = request(q, **kwargs)
            checks = t1.readback(client, auth[q["principal"]], row, originals, capture_wire=True)
            case = conflicts["cases"].get(q["id"])
            groups = t1.baseline_groups(q) if stage == "baseline" else q["required"]
            t1.score(row, checks, groups, conflict=(case, conflicts["acceptance"]) if case else None)
            refresh = row["refreshed_history"]
            if isinstance(refresh, dict):
                run_id = row["sse_events"][-1]["run_id"]
                refreshed = [(e["kind"], e["content"]) for e in refresh["entries"] if e["run_id"] == run_id]
            else:
                refreshed = [(e["role"], e["content"]) for e in refresh[-2:]]
            row["refresh_matches_commit"] = refreshed == [(e["role"], e["content"]) for e in row["committed_history"]]
            row["refreshed_citation_ids"] = [cid for _, content in refreshed for cid in t1.CITATION.findall(content)]
            row["scope_leaks"] = r3.scope_findings(row)
            if injected:
                row["injected_scope_leaks"] = row["scope_leaks"]["merged"]
                row["scope_leaks"]["merged"] = [cid for cid in row["injected_scope_leaks"] if cid not in injected]
            row["passed"] &= (row["refresh_matches_commit"] and not any(row["scope_leaks"].values())
                              and row["refreshed_citation_ids"] == t1.ids(checks))
            if q["id"] in FOCUS:
                row["fact_assertions"] = focus_checks(row, FOCUS[q["id"]])
                row["passed"] &= row["all_top20"] and all(row["fact_assertions"].values())
            if q["expectation"] == "clarify":
                row["passed"] = clarification(row, options)
            elif q["expectation"] != "denied":
                row["passed"] &= row["retrieval_trace"].get("selected_sku") == q["target"]["sku"]
            if acceptance_checks is not None:
                row["acceptance_checks"] = acceptance_checks(row)
                row["passed"] &= all(row["acceptance_checks"].values())
            return row

        rows = []
        with (output / "rows.jsonl").open("w", encoding="utf-8") as journal:
            for q in ([by_id[key] for key in fixed_ids] if fixed else questions):
                activate(q)
                row = run(q)
                rows.append(row)
                r3.write_row(journal, row)
                print(q["id"], "pass" if row["passed"] else row["first_deviation"] or "assertions", flush=True)

        probes = {}
        if extra_probes is not None and not fixed_before:
            probes.update(extra_probes(fixed, run, activate, by_id, writer, users, client, auth, audit))
        if fixed and extra_probes is None:
            group = client.post("/api/chat/conversations", headers=auth["owner"], json={"title": "R5 商品资料"})
            group_id = group.raise_for_status().json()["data"]["conversation_id"]
            for qid in CLARIFICATIONS:
                probes[f"{qid}_workgroup"] = run(by_id[qid], conversation_id=group_id)
            probes.update(fixed_probes(run, activate, by_id, writer, users, client, auth, audit,
                                       originals, output, group_id))
            if browser:
                browser_group = client.post("/api/chat/conversations", headers=auth["owner"],
                                            json={"title": "R5 浏览器回放"}).raise_for_status().json()
                subprocess.run(["node", "tests/product_sku_clarification_browser.cjs", str(client.base_url),
                                str(output / "browser.json"), browser_group["data"]["conversation_id"]],
                               check=True, timeout=90)
        gpu.assert_not_called()
        write_json(output / "probes.json", probes)

    if acceptance_checks is not None:
        return rows, probes, previous

    comparisons = compare(rows, previous)
    write_json(output / "comparison.json", comparisons)
    if dataset == "heldout":
        focus = {row["id"]: {"r4": previous[row["id"]], "r5": row} for row in rows if row["id"] in CLARIFICATIONS}
        if fixed:
            for qid, pair in focus.items():
                pair["r5_workgroup"] = probes[f"{qid}_workgroup"]
        write_json(output / "clarification-before-after.json", focus)
    retained = [row for row in comparisons.values() if row["r4_passed"]]
    expected_count = {"fixed": 0, "heldout": 40, "baseline": 54}[stage]
    expected_passes = {key for key, row in comparisons.items() if row["r4_passed"]} | (set(CLARIFICATIONS) & comparisons.keys())
    passed = {row["id"] for row in rows if row["passed"]}
    http_checks = [check for row in rows for check in row["http_readback"]]
    summary = {
        "stage": stage, "passed": sorted(passed), "failures": {
            row["id"]: row["first_deviation"] or "assertions" for row in rows if not row["passed"]},
        "http_verified": [sum(check["ok"] for check in http_checks), len(http_checks)],
        "previous_passes_retained": [sum(row["r5_passed"] and row["same_behavior"] for row in retained), expected_count],
        "top20_unchanged": sum(row["same_top20"] for row in comparisons.values()),
        "gold_ranks_unchanged": sum(row["same_gold_ranks"] for row in comparisons.values()),
        "probes": {name: row["passed"] for name, row in probes.items()},
    }
    if dataset == "heldout":
        summary["clarifications"] = [len(passed.intersection(CLARIFICATIONS)), len(CLARIFICATIONS)]
    if stage == "baseline":
        categories = {
            "answerable": [r for r in rows if r["input"]["expectation"] == "supported"],
            "multi": [r for r in rows if len(r["gold_ranks"]) > 1],
            "no_answer_zero_citations": [r for r in rows if r["input"]["expectation"] == "insufficient"],
            "denied_before_encoding": [r for r in rows if r["input"]["expectation"] == "denied"],
        }
        for name, items in categories.items():
            summary[name] = [sum(r["passed"] and (not r["http_readback"] or name in ("answerable", "multi"))
                                 for r in items), len(items)]
        assert [summary[name][1] for name in categories] == [44, 7, 8, 2]
    summary["ok"] = (passed == expected_passes and all(summary["probes"].values())
                     and len(retained) == expected_count == summary["previous_passes_retained"][0]
                     and summary["top20_unchanged"] == summary["gold_ranks_unchanged"] == len(rows)
                     and all(item["r4_failure"] == item["r5_failure"] for key, item in comparisons.items()
                             if key not in expected_passes)
                     and all(check["ok"] for check in http_checks))
    write_json(output / "summary.json", summary)
    assert summary["ok"], summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("fixed", "heldout", "baseline"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, default=t1.PACK / "fact-admission-r4-results.zip")
    parser.add_argument("--browser", action="store_true", help="Also check the shared chat composer in the fixed stage")
    args = parser.parse_args()
    if args.browser and args.stage != "fixed":
        parser.error("--browser requires the fixed stage")
    evaluate(args.stage, args.output.resolve(), args.reference, browser=args.browser)


if __name__ == "__main__":
    main()
