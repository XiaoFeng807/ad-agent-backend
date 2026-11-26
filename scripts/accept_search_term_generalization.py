"""Replay frozen search-term cases through StorePilot HTTP routes and score the SDK separately."""

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from contextlib import closing, nullcontext
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from scripts.accept_search_terms import collect_trace, read, serve, sha, write


FIXTURES = Path("tests/fixtures/search_terms_generalization")
SPEC_HEADER = "Attribute\tValue\tUnit\tPart\tRestriction\n"
SQL_TABLES = (
    "listing_test_shops", "listing_test_products", "listing_test_facts", "listing_test_sales",
    "listing_change_proposals", "listing_change_receipts", "search_term_ad_groups", "ad_plans",
    "search_term_amazon_membership", "search_term_amazon_row_links", "product_document_releases",
    "product_documents", "product_document_chunks",
)
GROUPS = "/api/chat/conversations"


def csv_bytes(header, rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=header.split(","), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def freeze(inputs, scores):
    products = {product["id"]: product for product in inputs["products"]}
    definitions = inputs.get("cases")
    if definitions is None:
        definitions = [{"id": f"{product}-{variant}", "product": product, "variant": variant}
                       for product in products for variant in inputs["cases_per_product"]]
        definitions.extend(inputs.get("extra_development_cases", []))
    frozen = {"label": inputs["label"], "question": inputs["question"], "rules": inputs["rules"], "cases": {}}
    oracle = {}
    for case in [*definitions, *inputs.get("attacks", [])]:
        product = products[case["product"]]
        variant, identifier = case["variant"], case["id"]
        document_version = case.get("document_version", variant if variant in {"conflict", "missing"} else "support")
        term = case.get("term", product["term"])
        report = {**inputs["report_defaults"], **case.get("report_changes", {}),
                  "Campaign Name": "Synthetic SP", "Ad Group Name": product["id"],
                  "Campaign ID": "SYN-SP", "Ad Group ID": product["sku"], "Customer Search Term": term}
        if variant == "low":
            report.update(inputs["low_report_changes"])
        raw = csv_bytes(inputs["report_header"], [report])
        member = inputs["membership"]
        membership = [{**member, "sku": product["sku"], "asin": product["asin"]}]
        if variant == "mixed":
            membership.append({**member, "sku": member["control_sku"], "asin": member["control_asin"]})
        frozen["cases"][identifier] = {
            "intent": product["intent"], "sku": product["sku"],
            "title": product["covered_title"] if variant == "covered" else product["title"],
            "report_csv": raw.decode(), "report_sha256": sha(raw), "membership": membership, "row_mappings": [],
            "original_tsv": SPEC_HEADER + "\n".join(product[document_version]) + "\n",
            "document_version": document_version, "term": term,
        }
        if case.get("control_source"):
            frozen["cases"][identifier]["control_tsv"] = SPEC_HEADER + "\n".join(case["control_source"]) + "\n"
        if "boundary" in case:
            frozen["cases"][identifier]["boundary"] = case["boundary"]
        if not case.get("normal_case", identifier not in scores.get("attacks", {})):
            oracle[identifier] = {"blocked_on": scores["attacks"][identifier]}
        elif "cases" in scores:
            oracle[identifier] = scores["cases"][identifier]
        else:
            oracle[identifier] = {**scores["variants"][variant], "demand": scores["demands"][product["id"]]}
    assert set(frozen["cases"]) == set(oracle)
    return frozen, oracle


def business_snapshot(path):
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        snapshot = {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in SQL_TABLES}
    return sha(json.dumps(snapshot, default=bytes.hex, ensure_ascii=False).encode())


def independent_metrics(case):
    columns = ("Impressions", "Clicks", "Purchases", "Spend", "Sales")
    rows = [[row[key] for key in columns] for row in csv.DictReader(io.StringIO(case["report_csv"]))]
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE original (impressions INT, clicks INT, purchases INT, spend, sales)")
        conn.executemany("INSERT INTO original VALUES (?, ?, ?, ?, ?)", rows)
        return dict(zip(("impressions", "clicks", "orders", "spend_cents", "sales_cents"), conn.execute("""
            SELECT sum(impressions), sum(clicks), sum(purchases),
                   sum(round(spend * 100)), sum(round(sales * 100)) FROM original
        """).fetchone()))


def sample_pairs(metrics, rules):
    return {
        "impressions": (metrics["impressions"], rules["min_impressions"]),
        "clicks": (metrics["clicks"], rules["min_clicks"]),
        "orders": (metrics["orders"], rules["title_min_orders"]),
        "cvr": (Fraction(100 * metrics["orders"], metrics["clicks"]) if metrics["clicks"] else 0,
                rules["title_min_cvr_pct"]),
    }


def sample_failures(pairs):
    return [name for name, (actual, minimum) in pairs.items() if actual < minimum]


def seed(conn, directory, case):
    from backend.rag import product_evidence

    first = case["membership"][0]
    shop, market = first["shop"], first["marketplace"]
    target = {"shop_id": shop, "marketplace_id": market, "sku": first["sku"]}
    with conn:
        conn.execute("INSERT OR IGNORE INTO listing_test_shops VALUES (?, ?, 17)", (shop, market))
        for sku, title in ((first["sku"], case["title"]), (first["control_sku"], "Synthetic control")):
            conn.execute("""INSERT INTO listing_test_products VALUES (?, ?, ?, 1, ?, '[]', 2499, 24)
                            ON CONFLICT (shop_id, marketplace_id, sku) DO UPDATE SET title=excluded.title""",
                         (shop, market, sku, title))
        conn.execute("DELETE FROM search_term_amazon_membership")
        report = next(csv.DictReader(io.StringIO(case["report_csv"])))
        fingerprint = sha(json.dumps(case["membership"], sort_keys=True).encode())
        conn.executemany("INSERT INTO search_term_amazon_membership VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (shop, market, first["advertiser"], report["Campaign ID"], report["Campaign Name"],
             report["Ad Group ID"], report["Ad Group Name"], entry["from"], entry["to"], entry["sku"], entry["asin"],
             fingerprint, f"synthetic-membership:{line}") for line, entry in enumerate(case["membership"], 2)
        ])

    def release(scope, version, original):
        file = "specifications.tsv"
        (directory / file).write_bytes(original.encode())
        product_evidence.import_release(
            conn, {"target": scope, "version": version, "documents": [{"id": "spec", "kind": "specs", "file": file}]},
            directory, embedding=lambda contents: [[1.0] for _ in contents], model_id="synthetic-fixture-non-vector",
        )
        product_evidence.activate(conn, scope, version)

    if "control_tsv" in case:
        release({**target, "sku": first["control_sku"]}, "control", case["control_tsv"])
    release(target, case["document_version"], case["original_tsv"])
    return target, release


def chunk_ids(conn, sku, version, lines):
    # Join the frozen physical rows directly to the source offsets, independent of retrieval.
    rows = conn.execute("""SELECT c.chunk_id, d.original, c.start_offset
        FROM product_document_chunks c JOIN product_documents d ON c.source_id=d.id
        JOIN product_document_releases r ON r.id=d.release_id
        WHERE r.sku=? AND r.version=? AND c.strategy='structured'""", (sku, version))
    indexed = {row["original"].count("\n", 0, row["start_offset"]) + 1: row["chunk_id"] for row in rows}
    return [indexed[line] for line in lines]


class Replay:
    name = "Fixed SDK replay"
    model = "frozen-search-term-generalization"

    def __init__(self, case, question, score):
        self.case = case
        self.question = question
        self.score = score
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, messages, **_):
        assert messages[1]["content"] == self.question
        evidence = json.loads(messages[-1]["content"])
        [item] = evidence["items"]
        assert item["term"] == self.case["term"]
        assert set(evidence) == {"receipt", "rules", "items"} and not {"demand", "below_minimum"}.intersection(item)
        below = sample_failures(sample_pairs(independent_metrics(self.case), evidence["rules"]))
        if len(self.case["membership"]) > 1:
            assert item["product_attribution_verified"] is False and item["original_title"] is None
            assert item["sample_scope"] == "ad_group_only"
            assert item["source_access"] == "withheld_ownership" and item["sources"] is None
        elif {"impressions", "clicks"}.intersection(below):
            assert item["source_access"] == "withheld_base_sample" and item["sources"] is None
        else:
            assert item["source_access"] == "available_for_review" and isinstance(item["sources"], list)
        if below:
            assert item["original_title"] is None
        columns = ("attribute", "value", "unit", "part", "restriction")
        refs = {"\t".join(row[key] for key in columns): row["ref"] for row in (item["sources"] or [])}
        responses = {"type": [], "spec": []}
        for row in self.score["source_texts"]:
            category = "type" if row.split("\t", 1)[0].strip().casefold() == "product type" else "spec"
            responses[category].append(refs[row])
        if "foreign_chunk" in self.score:
            responses["spec"][-1] = self.score["foreign_chunk"]
        answer = {"receipt": evidence["receipt"], "items": [
            {"id": item["id"], "below_minimum": [] if self.score.get("fake_sample_audit") else below,
             "decision": self.score["code"], "demand": self.score["demand"], "refs": responses},
        ]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))])


def expected_for(conn, name, case, oracle, all_scores):
    if "blocked_on" not in oracle:
        decision = oracle.copy()
    else:
        variant = "low" if name in {"low-sample-title", "fake-sample-audit"} else "support"
        product = "cleaning" if name == "widened-condition" else "size"
        decision = {**all_scores[f"{product}-{variant}"], "action": "title", "code": "supported_missing"}
    lines = case["original_tsv"].splitlines()
    decision["source_texts"] = [lines[line - 1] for line in decision["source_rows"]]
    if name == "wrong-sku":
        decision["foreign_chunk"] = chunk_ids(conn, case["membership"][0]["control_sku"], "control", [3])[0]
    if name == "widened-condition":
        decision["demand"] = {**decision["demand"], "conditions": {"part": "whole", "restriction": ""}}
    if name == "fake-sample-audit":
        decision["fake_sample_audit"] = True
    return decision


def response_json(response, expected):
    assert response.status_code == expected, (response.status_code, response.text[:500])
    return response.json()


def stale_check(client, path, detail, target, release, original):
    draft = detail["draft"]
    edit = client.put(path + "/draft", json={"title": draft["title"].replace(", ", " ", 1), "revision": 1})
    assert edit.status_code == 200, edit.text
    saved = edit.json()
    assert response_json(client.get(path + "/draft/copy", params={"revision": saved["revision"]}), 200) == saved
    release(target, "switched", original)
    refreshed = response_json(client.get(path), 200)
    assert refreshed["draft"] == saved and refreshed["rows"] == detail["rows"]
    assert refreshed["item"]["source_chunks"] == detail["item"]["source_chunks"]
    copy = client.get(path + "/draft/copy", params={"revision": saved["revision"]})
    edit = client.put(path + "/draft", json=saved)
    assert copy.status_code == edit.status_code == 409
    assert "facts_changed" in copy.json()["detail"]["reasons"]
    return {"copy": copy.status_code, "edit": edit.status_code, "old_card": 200}


def run_cases(app, database, diagnosis, observability, frozen, scores, folder, sandbox, replay):
    from fastapi.testclient import TestClient

    password = token_urlsafe(20)
    database.DB_PATH = str(path := sandbox / "sandbox.sqlite3")
    database.init_db()
    observability.TRACE_LOG_FILE = sandbox / "trace.jsonl"
    observability.ERROR_LOG_FILE = sandbox / "errors.jsonl"
    observability.FUNC_LOG_FILE = sandbox / "functions.jsonl"
    client = TestClient(app)
    outputs = {}
    with closing(database.get_db()) as conn:
        with conn:
            conn.execute("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                         (17, "sp-generalization", database.hash_password(password)))
        login = response_json(client.post("/api/login", json={"username": "sp-generalization", "password": password}), 200)
        client.headers["Authorization"] = "Bearer " + login["data"]["token"]
        for name, case in frozen["cases"].items():
            target, release = seed(conn, sandbox, case)
            expected = expected_for(conn, name, case, scores[name], scores)
            before = business_snapshot(path)
            created = response_json(client.post(GROUPS, json={"title": name}), 201)
            conversation_id = created["data"]["conversation_id"]
            api = f"{GROUPS}/{conversation_id}/search-terms"
            uploaded = response_json(client.post(api + "/reports", params={"filename": "report.csv"},
                                                 content=case["report_csv"].encode()), 201)
            fixture = Replay(case, frozen["question"], expected)
            mocked = patch.object(diagnosis.ProviderFactory, "create", return_value=fixture) if replay else nullcontext()
            with mocked:
                response = client.post(api + "/runs", json={"import_id": uploaded["report"]["import_id"],
                                      "sku": case["sku"], "message": frozen["question"]})
            outcome = {"intent": case["intent"], "http_status": response.status_code,
                       "expected": scores[name],
                       "metrics_sql": independent_metrics(case), "business_sql_before": before,
                       "business_sql_after": business_snapshot(path)}
            if response.status_code == 200:
                receipt = response.json()
                item_path = f"{api}/runs/{receipt['run_id']}/items/1"
                detail = response_json(client.get(item_path), 200)
                item = detail["item"]
                edit_payload = {"title": detail["draft"]["title"] if detail["draft"] else case["title"], "revision": 1}
                outcome.update(
                    trace_id=receipt["trace_id"], decision=item["model_validation"], action=item["action"],
                    code=item["code"], reason=item["reason"], attribution=item["attribution"],
                    draft=detail["draft"], review=item.get("review"),
                    copy_http=client.get(item_path + "/draft/copy", params={"revision": 1}).status_code,
                    edit_http=client.put(item_path + "/draft", json=edit_payload).status_code,
                    original_row_verified=(detail["report"]["sha256"] == case["report_sha256"]
                                           and detail["rows"][0]["values"] == next(csv.DictReader(io.StringIO(case["report_csv"])))),
                    metrics_verified=all(item["metrics"][key] == value for key, value in outcome["metrics_sql"].items()),
                    source_rows=[{"chunk_id": chunk["chunk_id"], "source": chunk["content"],
                                  "sku": chunk["sku"], "location": chunk["locations"][0]["location"]}
                                 for chunk in item.get("source_chunks", [])],
                    ownership=item["ownership"],
                )
                outcome["business_sql_after"] = business_snapshot(path)
                if name == "size-support" and replay and detail["draft"]:
                    outcome["stale_version"] = stale_check(client, item_path, detail, target, release, case["original_tsv"])
            else:
                outcome["error"] = response.json()
                failed = conn.execute("SELECT run_id, trace_id FROM chat_group_runs WHERE conversation_id=?",
                                      (conversation_id,)).fetchone()
                if failed:
                    retry_path = f"{api}/runs/{failed['run_id']}/items/1"
                    outcome.update(trace_id=failed["trace_id"], result_http=client.get(retry_path).status_code,
                                   copy_http=client.get(retry_path + "/draft/copy?revision=1").status_code,
                                   edit_http=client.put(retry_path + "/draft",
                                                        json={"title": case["title"], "revision": 1}).status_code)
            outputs[name] = outcome
            write(folder / "cases.json", outputs)
    return outputs


def grade_demand(raw, wanted):
    if "demand" not in raw:
        return False
    actual = raw["demand"]
    if not isinstance(actual, dict) or not isinstance(wanted, dict):
        return actual == wanted
    actual = {key: " ".join(value.lower().replace("×", "x").split())
              if key in {"product_type", "attribute", "value"} and isinstance(value, str) else value
              for key, value in actual.items()}
    if wanted["attribute"] in {"size", "width", "length", "height", "depth", "diameter"}:
        value, unit = actual.get("value"), actual.get("unit")
        if type(value) in (int, float):
            value = str(value)
        units = {"in": "25.4", "inch": "25.4", "inches": "25.4", '"': "25.4",
                 "cm": "10", "centimeter": "10", "centimeters": "10",
                 "mm": "1", "millimeter": "1", "millimeters": "1"}
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,4})?", value) and isinstance(unit, str):
            if factor := units.get(unit.lower().strip()):
                actual.update(unit="mm", value=format((Decimal(value) * Decimal(factor)).normalize(), "f"))
    if actual.get("attribute") == "vesa" and isinstance(actual.get("value"), str):
        value = actual["value"]
        if re.fullmatch(r"[0-9]{2,4}\s*x\s*[0-9]{2,4}", value):
            actual["value"] = "".join(value.split())
    return actual == wanted


def score_cases(outputs, scores, traces, frozen):
    by_intent, by_action = defaultdict(Counter), defaultdict(Counter)
    observe_reasons = defaultdict(Counter)
    by_boundary = defaultdict(lambda: defaultdict(Counter))
    model, server = Counter(), Counter()
    by_trace = {trace["trace_id"]: trace for trace in traces}
    passed = True
    for index, (name, outcome) in enumerate(outputs.items()):
        check = outcome.get("decision", {})
        raw = check.get("model", {})
        expected = scores[name]
        trace = by_trace.get(outcome.get("trace_id"))
        if trace is None and len(traces) == len(outputs) and outcome["http_status"] != 200:
            # Older runs logged the failed SDK call before the HTTP route returned a run receipt.
            trace = traces[index]
            outcome["trace_id"] = trace["trace_id"]
        stages = trace.get("steps", []) if trace else []
        calls = [step for step in stages if step["stage"] == "model_call"]
        sdk_error = next((step["error_code"] for step in stages if step["stage"] == "model_result"
                          and step["status"] == "error"), None)
        finish_reason = next((step["evidence"].get("finish_reason") for step in stages
                              if step["stage"] == "model_contract"), None)
        timed_out = sdk_error == "APITimeoutError"
        truncated = finish_reason == "length"
        if truncated:
            raw = {}
        case = frozen["cases"][name]
        attribution_ok = outcome.get("attribution") == ("product" if len(case["membership"]) == 1 else "ad_group")
        ownership_ok = bool(outcome.get("ownership")) == (outcome.get("attribution") == "product") and all(
            row["sku"] == case["sku"] for row in outcome.get("ownership", [])
        )
        pairs = sample_pairs(outcome["metrics_sql"], frozen["rules"])
        below = sample_failures(pairs)
        product_scoped = len(case["membership"]) == 1
        visible = product_scoped and not {"impressions", "clicks"}.intersection(below)
        source_access = ("withheld_ownership" if not product_scoped else
                         "available_for_review" if visible else "withheld_base_sample")
        scope_verified = (check.get("below_minimum") == below
                          and check.get("sample_scope") == ("verified_product" if product_scoped else "ad_group_only")
                          and check.get("title_exposed") == bool(visible and not below)
                          and check.get("source_access") == source_access
                          and check.get("source_count_exposed") == (len(outcome.get("source_rows", [])) if visible else 0))
        server["wrong_product_attributions"] += int(
            outcome.get("attribution") == "product" and not (attribution_ok and ownership_ok)
        )
        guard_ok = (
            outcome["business_sql_before"] == outcome["business_sql_after"]
            and outcome.get("original_row_verified") and outcome.get("metrics_verified")
            and outcome.get("copy_http") == outcome.get("edit_http") == (200 if outcome.get("draft") else 404)
            and ownership_ok and scope_verified
        )
        if "blocked_on" in expected:
            blocked = expected["blocked_on"] in check.get("rejections", []) and outcome.get("draft") is None
            server["injections_blocked"] += int(blocked)
            server["wrong_drafts"] += int(outcome.get("draft") is not None)
            outcome["assessment"] = {"stage": "服务端校验", "blocked": blocked}
            passed &= bool(blocked and guard_ok)
            continue
        wanted = expected["action"]
        issued = raw.get("action")
        right_action = issued == wanted
        right_reason = raw.get("code") == expected["code"]
        right_demand = grade_demand(raw, expected["demand"])
        reported_demand = raw.get("demand")
        reported_type = reported_demand.get("product_type") if isinstance(reported_demand, dict) else None
        wanted_type = expected["demand"].get("product_type") if expected["demand"] is not None else None
        type_correct = "demand" in raw and (
            isinstance(reported_type, str) and " ".join(reported_type.lower().split()) == wanted_type
            if wanted_type is not None else reported_demand is None
        )
        located = {row["chunk_id"]: (row["sku"], row["location"]) for row in outcome.get("source_rows", [])}
        necessary = {(frozen["cases"][name]["sku"], f"row:{row}") for row in expected["source_rows"]}
        refs = raw.get("citations")
        cited = [located.get(ref, ("?", "?")) for ref in refs if isinstance(ref, str)] if isinstance(refs, list) else []
        right_citations = isinstance(refs, list) and len(refs) == len(cited) == len(necessary) and set(cited) == necessary
        retrieved = all((frozen["cases"][name]["sku"], f"row:{row}") in located.values()
                        for row in expected["source_rows"])
        audit = raw.get("below_minimum")
        audit_correct = (isinstance(audit, list) and all(isinstance(field, str) for field in audit)
                         and sorted(audit) == sorted(below))
        bound = ("item_id" not in case or (
            type(raw.get("id")) is int and raw["id"] == case["item_id"]
            and not {"invalid_item_alignment", "receipt_mismatch", "batch_stream_unfinished"}.intersection(
                check.get("rejections", [])
            )
        ))
        correct = right_action and right_reason and right_demand and right_citations and audit_correct and bound
        if "boundary" in case:
            metric, position = case["boundary"]
            actual, minimum = pairs[metric]
            assert position == ("below" if actual < minimum else "at" if actual == minimum else "above")
            outcome["boundary"] = {"metric": metric, "position": position,
                                   "actual": float(actual), "minimum": minimum, "failed": below}
            by_boundary[metric][position].update(cases=1, correct=int(correct), action_correct=int(right_action),
                                                 sample_audit_correct=int(audit_correct),
                                                 timeouts=int(timed_out), truncations=int(truncated))
        if wanted == "observe":
            observe_reasons[expected["code"]].update(
                cases=1, correct=correct, action_correct=right_action, citation_correct=right_citations,
                reason_correct=right_reason, timeouts=timed_out, truncations=truncated,
                wrong_responses=not correct and not sdk_error and not truncated,
            )
        accepted = bool(check) and not check["rejections"]
        title_ok = (outcome.get("draft") is not None) == (wanted == "title" and correct)
        published = (outcome.get("action"), outcome.get("code"), bool(outcome.get("review")))
        expected_published = (wanted, expected["code"], wanted == "review") if correct else ("observe", "model_rejected", False)
        model.update({"cases": 1, "correct": correct, "action_correct": right_action,
                      "reason_correct": right_reason, "product_type_correct": type_correct,
                      "citation_correct": right_citations, "demand_correct": right_demand,
                      "sample_audit_correct": audit_correct,
                      "length_stops": int(truncated), "timeouts": int(timed_out),
                      "cited_chunks": len(cited), "required_chunks": len(necessary),
                      "correct_chunks": len(set(cited) & necessary)})
        by_intent[outcome["intent"]].update({"cases": 1, "correct": correct, "citation_correct": right_citations,
                                           "action_correct": right_action, "timeouts": timed_out,
                                           "truncations": truncated})
        by_action[wanted].update({"expected": 1, "correct": correct, "missed": not correct,
                                  "citation_correct": right_citations, "action_correct": right_action,
                                  "action_missed": not right_action})
        if isinstance(issued, str) and not correct:
            by_action[issued]["incorrect_issued"] += 1
            by_intent[outcome["intent"]]["incorrect_issued"] += 1
            if not right_action:
                by_action[issued]["action_incorrect_issued"] += 1
                by_intent[outcome["intent"]]["action_incorrect_issued"] += 1
        if not correct:
            by_intent[outcome["intent"]]["missed"] += 1
        server.update({"correct_admitted": correct and accepted,
                       "wrong_blocked": not correct and not accepted and not sdk_error and not truncated,
                       "truncated_blocked": truncated and not accepted,
                       "correct_blocked": correct and not accepted, "wrong_admitted": not correct and accepted,
                       "wrong_drafts": (not correct or wanted != "title") and outcome.get("draft") is not None})
        valid = bool(guard_ok and title_ok and retrieved and published == expected_published
                     and len(calls) == 1 and correct == accepted
                     and attribution_ok)
        if sdk_error:
            server["provider_failures"] += 1
            valid = (outcome["http_status"] == 409 and outcome["business_sql_before"] == outcome["business_sql_after"]
                     and not outcome.get("draft") and all(outcome.get(key) == 404
                                                        for key in ("result_http", "copy_http", "edit_http")))
        passed &= valid
        failure = ("证据检索" if not sdk_error and not retrieved else "服务端校验" if not valid else
                   "SDK 超时" if timed_out else "SDK 输出截断" if truncated else "SDK 请求失败" if sdk_error else
                   "模型判断" if not raw else "意图识别" if not right_demand else "模型判断" if not correct else None)
        outcome["assessment"] = {"stage": failure, "action_correct": right_action, "demand_correct": right_demand,
                                 "reason_correct": right_reason, "product_type_correct": type_correct,
                                 "citation_correct": right_citations, "sample_audit_correct": audit_correct,
                                 "model_correct": correct, "server_valid": valid}
        if "item_id" in case:
            outcome["assessment"]["item_binding_correct"] = bound
        if sdk_error:
            outcome["assessment"]["sdk_error"] = sdk_error
        if finish_reason:
            outcome["assessment"]["response_finish_reason"] = finish_reason
    return {"server_passed": passed, "model": model, "server": server,
            "by_intent": by_intent, "by_action": by_action, "observe_by_reason": observe_reasons,
            "by_boundary": by_boundary}


def verify_workbench(app, database, diagnosis, observability, frozen, scores, folder, sandbox):
    case = frozen["cases"]["cleaning-support"]
    database.DB_PATH = str(sandbox / "workbench.sqlite3")
    database.init_db()
    password = token_urlsafe(20)
    with closing(database.get_db()) as conn:
        with conn:
            conn.execute("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                         (17, "sp-generalization", database.hash_password(password)))
        seed(conn, sandbox, case)
        expected = expected_for(conn, "cleaning-support", case, scores["cleaning-support"], scores)
    (folder / "report.csv").write_bytes(case["report_csv"].encode())
    observability.TRACE_LOG_FILE = sandbox / "workbench-trace.jsonl"
    replay = Replay(case, frozen["question"], expected)
    environment = {key: value for key, value in os.environ.items()
                   if not any(marker in key for marker in ("API_KEY", "SECRET", "PASSWORD", "TOKEN"))}
    environment.update(AD_BROWSER_CHANNEL="msedge", SEARCH_TERMS_PASSWORD=password)
    with patch.object(diagnosis.ProviderFactory, "create", return_value=replay), serve(app) as origin:
        subprocess.run(["node", "tests/search_term_generalization_browser.cjs", origin, str(folder)],
                       env=environment, timeout=120, check=True)
    collect_trace(folder, "workbench", source=observability.TRACE_LOG_FILE)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("development", "development-live", "fixed", "live", "workbench"))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cohort", choices=("formal", "confirmation", "observe_holdout", "sample_holdout",
                                            "click_holdout", "evidence_holdout"),
                        default="formal")
    parser.add_argument("--dev-case", action="append", default=[], help="Restrict development-live to these case IDs.")
    args = parser.parse_args()
    mode, root = args.mode, args.output_dir.resolve()
    real = mode in {"live", "development-live"}
    name = "development" if mode in {"development", "development-live", "workbench"} else args.cohort
    inputs, grades = read(FIXTURES / f"{name}.json"), read(FIXTURES / f"{name}_scores.json")
    frozen, scores = freeze(inputs, grades)
    if args.dev_case and mode != "development-live":
        parser.error("--dev-case only applies to development-live")
    if mode == "development-live":
        scores = {key: value for key, value in scores.items() if "blocked_on" not in value}
        if args.dev_case:
            if set(args.dev_case) - scores.keys():
                parser.error("--dev-case must name a frozen, non-injection development case")
            scores = {key: value for key, value in scores.items() if key in args.dev_case}
        frozen["cases"] = {key: case for key, case in frozen["cases"].items() if key in scores}
    if mode == "live":
        assert read(root / "fixed" / "result.json")["passed"]
        assert read(root / "fixed" / "frozen.json") == frozen
        assert read(root / "fixed" / "scores.json") == scores
    folder = root / mode
    folder.mkdir(parents=True, exist_ok=False)
    write(folder / "frozen.json", frozen)
    write(folder / "scores.json", scores)
    os.environ.update(LLM_PROVIDER="glm" if real else "mock", JWT_SECRET=token_urlsafe(48),
                      DIAGNOSIS_MYSQL_ENABLED="false", DIAGNOSIS_SANDBOX_DB_PATH="")
    if not real:
        os.environ["AD_OFFLINE_ACCEPTANCE"] = "1"
    with TemporaryDirectory(prefix="sandbox-", dir=folder) as sandbox:
        os.environ["RAG_DB_PATH"] = str(Path(sandbox) / "rag")
        from backend import observability
        from backend.agent import search_term_diagnosis as diagnosis
        from backend.database import database
        from fast_server import app, settings

        assert all(diagnosis.RULES[key] == value for key, value in frozen["rules"].items())
        protocol = {
            "version": diagnosis.CLAIM_PROTOCOL, "prompt_sha256": sha(diagnosis.CLAIM_SYSTEM.encode()),
            "rules": diagnosis.CLAIM_RULES, "model": settings.llm_config("glm").model,
            "thinking": diagnosis.CLAIM_GLM_THINKING, "temperature": 0, "sdk_max_retries": 0,
            "single_item_max_tokens": diagnosis.claim_max_tokens(1), "sdk_timeout_seconds": diagnosis.CLAIM_TIMEOUT,
        }
        if mode == "live":
            assert read(root / "fixed" / "protocol.json") == protocol
        write(folder / "protocol.json", protocol)
        if real and not settings.llm_config("glm").api_key:
            raise SystemExit("GLM_API_KEY is required for live acceptance.")
        if mode == "workbench":
            return verify_workbench(app, database, diagnosis, observability, frozen, scores, folder, Path(sandbox))
        outputs = run_cases(app, database, diagnosis, observability, frozen, scores, folder, Path(sandbox), not real)
        traces, requests = collect_trace(folder, mode, source=observability.TRACE_LOG_FILE)
    result = score_cases(outputs, scores, traces, frozen)
    result.update(passed=(result["server_passed"] and requests == len(outputs)
                          and result["model"]["correct"] == result["model"]["cases"]),
                  mode=mode, sdk_requests=requests, sdk_seconds=[
                      step["duration"] for trace in traces for step in trace["steps"] if step["stage"] == "model_result"
                  ], total_seconds=[trace["total_seconds"] for trace in traces])
    write(folder / "cases.json", outputs)
    write(folder / "result.json", result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
