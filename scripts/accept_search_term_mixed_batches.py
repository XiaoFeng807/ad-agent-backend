"""Run frozen multi-item reports through StorePilot's normal diagnosis and draft endpoints."""

import argparse
import csv
import io
import json
import os
import re
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from scripts.accept_search_term_generalization import (
    FIXTURES, GROUPS, SPEC_HEADER, business_snapshot, chunk_ids, csv_bytes, independent_metrics,
    response_json, sample_failures, sample_pairs, score_cases, seed,
)
from scripts.accept_search_terms import collect_trace, read, sha, write


LOCK = Path("output/search-term-mixed-batches/locked-protocol.json")
COLUMNS = ("attribute", "value", "unit", "part", "restriction")
BATCH_CODES = {"G": "unattributed", "S": "sample", "U": "unverified", "C": "fact_conflict",
               "A": "already_in_title", "T": "supported_missing", "L": "title_rules"}


def packet_sources(item):
    rows = item["sources"] or []
    return [dict(zip(("ref", *COLUMNS), row, strict=True)) if isinstance(row, list) else row for row in rows]


def response_row(row, *, compact):
    if not compact:
        return row if isinstance(row, dict) else None
    if isinstance(row, dict) and set(row) == {"id", "requested", "failed", "code", "refs"}:
        code = row["code"]
        return {"id": row["id"], "demand": row["requested"], "below_minimum": row["failed"], "wire_code": code,
                "decision": BATCH_CODES.get(code) if isinstance(code, str) else None, "refs": row["refs"]}
    if not isinstance(row, list) or len(row) != 6:
        return None
    item_id, demand, failed, code, type_refs, spec_refs = row
    entry = {"id": item_id, "wire_code": code, "below_minimum": failed,
             "decision": BATCH_CODES.get(code) if isinstance(code, str) else None,
             "refs": {"type": type_refs, "spec": spec_refs}}
    if isinstance(demand, list) and len(demand) == 6 and all(isinstance(value, str) for value in demand):
        product_type, attribute, value, unit, part, restriction = demand
        if attribute == "cleaning" or (part == restriction == ""):
            entry["demand"] = {"product_type": product_type, "attribute": attribute, "value": value, "unit": unit,
                               "conditions": {"part": part, "restriction": restriction} if attribute == "cleaning" else {}}
    return entry


def replay_row(response, *, named):
    demand = response["demand"]
    codes = {value: key for key, value in BATCH_CODES.items()}
    code = codes[response["decision"]]
    if named:
        return {"id": response["id"], "requested": demand, "failed": response["below_minimum"],
                "code": code, "refs": response["refs"]}
    values = None
    if demand is not None:
        values = [demand[key] for key in ("product_type", "attribute", "value", "unit")]
        values.extend(demand["conditions"].get(key, "") for key in ("part", "restriction"))
    return [response["id"], values, response["below_minimum"], code,
            response["refs"]["type"], response["refs"]["spec"]]


def freeze(inputs):
    frozen = {key: inputs[key] for key in ("label", "question", "rules", "membership")}
    frozen.update(batches={}, cases={})
    for batch in inputs["batches"]:
        name = batch["id"]
        original = SPEC_HEADER + "\n".join(batch["original"]) + "\n"
        csv_rows, split_rows, cases = [], [], {}

        for index, item in enumerate(batch["items"], 1):
            row = {
                **inputs["report_defaults"], **item.get("report_changes", {}),
                "Campaign Name": name, "Campaign ID": f"batch-{name}",
                "Ad Group Name": item["group"], "Ad Group ID": f"{name}-{item['group']}",
                "Customer Search Term": item["term"],
            }
            lines = [len(csv_rows) + 2]
            csv_rows.append(row)
            if "second_row" in item:
                assert item["group"] == "mixed"
                split_rows.append((lines, {**row, **item["second_row"]}))
            cases[item["id"]] = {
                "batch": name, "item_id": index, "intent": item["intent"], "group": item["group"],
                "sku": batch["sku"], "term": item["term"], "physical_lines": lines,
                "membership": [batch["sku"]] + ([inputs["membership"]["control_sku"]] if item["group"] == "mixed" else []),
            }
        for lines, row in split_rows:
            lines.append(len(csv_rows) + 2)
            csv_rows.append(row)

        raw = csv_bytes(inputs["report_header"], csv_rows)
        for case in cases.values():
            case["report_csv"] = csv_bytes(inputs["report_header"], [csv_rows[i - 2] for i in case["physical_lines"]]).decode()
        mixed = next(case for case in cases.values() if case["group"] == "mixed")
        assert len(mixed["physical_lines"]) == 2
        frozen["batches"][name] = {
            "sku": batch["sku"], "asin": batch["asin"], "title": batch["title"],
            "original_tsv": original, "document_version": "frozen",
            "report_csv": raw.decode(), "report_sha256": sha(raw),
            "item_keys": list(cases), "linked_line": mixed["physical_lines"][0],
        }
        frozen["cases"].update(cases)
    return frozen


def assert_lock(lock, diagnosis, settings):
    actual = {
        "version": diagnosis.CLAIM_PROTOCOL, "prompt_sha256": sha(diagnosis.CLAIM_SYSTEM.encode()),
        "rules": diagnosis.CLAIM_RULES, "model": settings.llm_config("glm").model,
        "thinking": diagnosis.CLAIM_GLM_THINKING, "temperature": 0, "sdk_max_retries": 0,
        "single_item_max_tokens": diagnosis.claim_max_tokens(1), "sdk_timeout_seconds": diagnosis.CLAIM_TIMEOUT,
    }
    assert actual == lock["protocol"]
    if "batch_protocol" in lock:
        batch = lock["batch_protocol"]
        assert lock["batch_protocol"] == {
            "version": diagnosis.CLAIM_BATCH_PROTOCOL, "prompt_sha256": sha(diagnosis.CLAIM_BATCH_SYSTEM.encode()),
            **({"thinking": diagnosis.CLAIM_BATCH_GLM_THINKING} if "thinking" in batch else {}),
            **({"stream": diagnosis.CLAIM_BATCH_STREAM} if "stream" in batch else {}),
        }
    assert {str(n): diagnosis.claim_max_tokens(n) for n in range(1, 25)} == lock["sdk_max_tokens_by_item_count"]
    assert all(sha(Path(path).read_bytes()) == fingerprint for path, fingerprint in lock["source_sha256"].items())


def seed_batch(conn, sandbox, frozen, batch):
    member = frozen["membership"]
    product = {**member, "sku": batch["sku"], "asin": batch["asin"]}
    # Reuse the fixture catalog importer, then populate the report's distinct ad groups.
    seed(conn, sandbox, {**batch, "membership": [product]})
    rows = list(csv.DictReader(io.StringIO(batch["report_csv"])))
    groups = {row["Ad Group ID"]: row for row in rows}
    control = {**member, "sku": member["control_sku"], "asin": member["control_asin"]}
    shop, market = member["shop"], member["marketplace"]
    with conn:
        conn.execute("DELETE FROM search_term_amazon_membership")
        conn.execute("DELETE FROM search_term_amazon_row_links")
        for group, row in groups.items():
            members = [product, control] if row["Ad Group Name"] == "mixed" else [product]
            fingerprint = sha(json.dumps([row["Campaign ID"], group, members], sort_keys=True).encode())
            conn.executemany("INSERT INTO search_term_amazon_membership VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                (shop, market, entry["advertiser"], row["Campaign ID"], row["Campaign Name"], group,
                 row["Ad Group Name"], entry["from"], entry["to"], entry["sku"], entry["asin"],
                 fingerprint, f"batch-membership:{group}:{index}") for index, entry in enumerate(members, 1)
            ])
        line = batch["linked_line"]
        linked = rows[line - 2]
        proof = sha(json.dumps([batch["report_sha256"], line, product["sku"]]).encode())
        conn.execute("INSERT INTO search_term_amazon_row_links VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (shop, market, batch["report_sha256"], "", line, linked["Campaign ID"], linked["Ad Group ID"],
                      product["sku"], proof, "batch-row-link:1"))


class CallAudit:
    """Check the actual outgoing batch and preserve its first response without recording reasoning."""

    def __init__(self, frozen, scores, batch, lock, path, completion, collector, *, inject=False):
        self.frozen = frozen
        self.scores = scores
        self.batch = batch
        self.lock = lock
        self.path = path
        self.completion = completion
        self.collector = collector
        self.inject = inject
        self.calls = 0
        self.verified = False

    def __call__(self, provider, budget, **kwargs):
        self.calls += 1
        assert self.calls == 1
        messages = kwargs["messages"]
        evidence = json.loads(messages[-1]["content"])
        config = {key: kwargs[key] for key in ("model", "temperature", "sdk_max_retries", "max_tokens", "timeout",
                                               "response_format", "stream")}
        config["thinking"] = kwargs.get("extra_body", {}).get("thinking", {}).get("type")
        write(self.path / "sdk-input.json", {"config": config, "evidence": evidence})
        assert sha(messages[0]["content"].encode()) == self.lock.get("batch_protocol", self.lock["protocol"])["prompt_sha256"]
        assert messages[1]["content"] == self.frozen["question"]
        assert config["sdk_max_retries"] == config["temperature"] == 0
        assert config["timeout"] == self.lock["protocol"]["sdk_timeout_seconds"]
        assert config["max_tokens"] == self.lock["sdk_max_tokens_by_item_count"][str(len(self.batch["item_keys"]))]
        assert config["response_format"] == {"type": "json_object"}
        assert config["stream"] is self.lock.get("batch_protocol", {}).get("stream", False)
        if self.completion:
            batch_thinking = self.lock.get("batch_protocol", {}).get("thinking", self.lock["protocol"]["thinking"])
            assert config["model"] == self.lock["protocol"]["model"]
            assert config["thinking"] == ("enabled" if batch_thinking else "disabled")
        assert set(evidence) == {"receipt", "rules", "items"} and evidence["rules"] == self.lock["protocol"]["rules"]
        responses = {}
        original = self.batch["original_tsv"].splitlines()
        version = self.lock.get("batch_protocol", {}).get("version", "")
        named = version.startswith("batch-named-")
        compact = named or version.startswith("batch-rows-")
        for key, item in zip(self.batch["item_keys"], evidence["items"], strict=True):
            case, expected = self.frozen["cases"][key], self.scores[key]
            assert item["id"] == case["item_id"] and item["term"] == case["term"]
            assert not {"demand", "below_minimum", "requested_vesa_pair"}.intersection(item)
            for literal in item.get("query_literals", {}).values():
                assert literal in item["term"].casefold()
            if "requested" in item:
                demand = expected["demand"]
                requested = demand
                if demand is not None and not named:
                    requested = [demand[field] for field in ("product_type", "attribute", "value", "unit")]
                    requested.extend([demand["conditions"].get(field, "") for field in ("part", "restriction")])
                assert item["requested"] == requested
            verified = case["group"] != "mixed"
            assert item["sample_scope"] == ("verified_product" if verified else "ad_group_only")
            stats = independent_metrics(case)
            if compact:
                rules = self.frozen["rules"]
                comparisons = {
                    "impressions": [stats["impressions"], rules["min_impressions"]],
                    "clicks": [stats["clicks"], rules["min_clicks"]],
                    "orders": [stats["orders"], rules["title_min_orders"]],
                    "cvr": [100 * stats["orders"], stats["clicks"] * rules["title_min_cvr_pct"]],
                }
                if named and version != "batch-named-evidence-v19":
                    comparisons = {field: dict(zip(("actual", "minimum"), pair, strict=True))
                                   for field, pair in comparisons.items()}
                assert item["tests"] == comparisons
            else:
                assert item["product_attribution_verified"] is verified
                assert all(item["metrics"][metric] == stats[metric] for metric in ("impressions", "clicks", "orders"))
            below = sample_failures(sample_pairs(stats, self.frozen["rules"]))
            visible = expected["offered_rows"] is not None
            assert item["original_title"] == (self.batch["title"] if visible else None)
            if "source_access" in item:
                assert item["source_access"] == ("withheld_ownership" if not verified else
                                                 "available_for_review" if visible else "withheld_base_sample")
            assert (item["sources"] is not None) == visible
            packet = packet_sources(item)
            if named:
                for source in packet:
                    if source["attribute"].casefold() == "compatible models":
                        assert source["model_entries"] == [value.strip() for value in source["value"].split("|")]
                        assert source["complete_list"] is (source["restriction"].casefold() == "complete list")
                    if "part_scope" in source:
                        requested_part = expected["demand"]["conditions"]["part"]
                        source_part = source["part"].casefold()
                        relation = ("same_part" if source_part == requested_part else
                                    "whole_scope" if "whole" in (source_part, requested_part) else "other_part")
                        assert source["part_scope"] == relation
            rows = {"\t".join(source[column] for column in COLUMNS): source["ref"] for source in packet}
            assert set(rows) == {original[line - 1] for line in (expected["offered_rows"] or [])}, key
            if "allowed_ref_ids" in item:
                by_role = {"type": [], "spec": []}
                for line, ref in rows.items():
                    if ref is not None:
                        by_role["type" if line.startswith("Product type\t") else "spec"].append(ref)
                assert item["allowed_ref_ids"] == by_role
            if "batch_protocol" in self.lock:
                assert all(ref is None or (isinstance(ref, str) and ref.startswith(f"{item['id']}r")) for ref in rows.values())
            refs = {"type": [], "spec": []}
            for line in expected["source_rows"]:
                text = original[line - 1]
                refs["type" if text.startswith("Product type\t") else "spec"].append(rows[text])
            responses[key] = {"id": case["item_id"], "demand": expected["demand"], "refs": refs,
                              "below_minimum": below, "decision": expected["code"]}
        self.verified = True
        if self.completion:
            response = self.completion(provider, budget, **kwargs)
        else:
            if self.inject:
                title, conflict, missing, mixed = (self.inject[code] for code in (
                    "supported_missing", "fact_conflict", "unverified", "unattributed",
                ))
                for key in (mixed, missing):
                    responses[key]["refs"] = responses[title]["refs"]
                responses[mixed]["decision"] = responses[title]["decision"]
                responses[title]["demand"] = responses[conflict]["demand"]
            items = list(responses.values())
            payload = {"receipt": evidence["receipt"],
                       "items": [replay_row(row, named=named) for row in items] if compact else items}
            content = json.dumps(payload)
            if config["stream"]:
                midpoint = len(content) // 2
                return iter(
                    SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part), finish_reason=finish)])
                    for part, finish in ((content[:midpoint], None), (content[midpoint:], "stop"))
                )
            response = SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop"),
            ])
        return response if config["stream"] else self.capture(response)

    def collect(self, stream):
        return self.capture(self.collector(stream))

    def capture(self, response):
        choice = response.choices[0]
        write(self.path / "sdk-first-response.json",
              {"content": choice.message.content, "finish_reason": getattr(choice, "finish_reason", None)})
        return response


def captured_items(path):
    if not path.exists():
        return []
    content = read(path)["content"]
    if not isinstance(content, str):
        return []
    content = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
    try:
        answer = json.loads(fenced[1] if fenced else content)
    except ValueError:
        return []
    return answer.get("items", []) if isinstance(answer, dict) else []


def result_item(client, path, case, batch, before, trace_id, http_status):
    metrics = independent_metrics(case)
    outcome = {"batch": case["batch"], "intent": case["intent"], "http_status": http_status, "trace_id": trace_id,
               "metrics_sql": metrics, "business_sql_before": before}
    if http_status != 200:
        outcome.update(result_http=client.get(path).status_code,
                       copy_http=client.get(path + "/draft/copy?revision=1").status_code,
                       edit_http=client.put(path + "/draft", json={"title": batch["title"], "revision": 1}).status_code)
        return outcome
    detail = response_json(client.get(path), 200)
    item, draft = detail["item"], detail["draft"]
    expected_rows = list(csv.DictReader(io.StringIO(case["report_csv"])))
    expected_group = expected_rows[0]["Ad Group ID"]
    copy = client.get(path + "/draft/copy?revision=1")
    edit = client.put(path + "/draft", json={"title": draft["title"] if draft else batch["title"], "revision": 1})
    outcome.update(
        decision=item["model_validation"], action=item["action"], code=item["code"],
        attribution=item["attribution"], draft=draft, review=item.get("review"),
        copy_http=copy.status_code, edit_http=edit.status_code,
        draft_roundtrip_verified=not draft or (copy.status_code == edit.status_code == 200 and copy.json() == edit.json() == draft),
        original_row_verified=(detail["report"]["sha256"] == batch["report_sha256"]
                               and item["id"] == case["item_id"] and item["term"] == case["term"]
                               and item["scope"]["ad_group_id"] == expected_group
                               and [row["line"] for row in detail["rows"]] == case["physical_lines"]
                               and [row["values"] for row in detail["rows"]] == expected_rows),
        metrics_verified=all(item["metrics"][key] == value for key, value in metrics.items()),
        source_rows=[{"chunk_id": chunk["chunk_id"], "source": chunk["content"], "sku": chunk["sku"],
                      "location": chunk["locations"][0]["location"]} for chunk in item.get("source_chunks", [])],
        ownership=item["ownership"],
    )
    if item.get("review"):
        outcome["original_row_verified"] &= (
            item["review"]["scope"]["ad_group_id"] == expected_group and item["review"]["term"] == case["term"]
        )
    if case["group"] != "mixed":
        outcome["original_row_verified"] &= ([entry["line"] for entry in item["ownership"]] == case["physical_lines"]
                                              and all(entry["ad_group_id"] == expected_group for entry in item["ownership"])
                                              and detail["product"]["target"]["sku"] == case["sku"])
    return outcome


def run_batch(client, conn, diagnosis, sandbox, frozen, scores, lock, name, folder, *, live, inject=False):
    batch = frozen["batches"][name]
    seed_batch(conn, sandbox, frozen, batch)
    before = business_snapshot(sandbox / "sandbox.sqlite3")
    created = response_json(client.post(GROUPS, json={"title": name}), 201)
    conversation_id = created["data"]["conversation_id"]
    api = f"{GROUPS}/{conversation_id}/search-terms"
    imported = response_json(client.post(api + "/reports", params={"filename": f"{name}.csv"},
                                        content=batch["report_csv"].encode()), 201)["report"]
    assert imported["sha256"] == batch["report_sha256"] and imported["batch_count"] == 1
    assert imported["term_count"] == len(batch["item_keys"]) and imported["row_count"] == len(batch["item_keys"]) + 1
    folder.mkdir()
    audit = CallAudit(frozen, scores, batch, lock, folder, diagnosis.create_completion if live else None,
                      diagnosis.collect_claim_batch_response, inject=inject)
    with (patch.object(diagnosis, "create_completion", side_effect=audit),
          patch.object(diagnosis, "collect_claim_batch_response", side_effect=audit.collect)):
        response = client.post(api + "/runs", json={"import_id": imported["import_id"], "sku": batch["sku"],
                                                   "message": frozen["question"]})
    assert audit.calls == 1 and audit.verified
    run = conn.execute("SELECT run_id, trace_id FROM chat_group_runs WHERE conversation_id=?", (conversation_id,)).fetchone()
    raw = captured_items(folder / "sdk-first-response.json")
    compact = lock.get("batch_protocol", {}).get("version", "").startswith(("batch-rows-", "batch-named-"))
    outcomes = {}
    for key in batch["item_keys"]:
        case = frozen["cases"][key]
        path = f"{api}/runs/{run['run_id']}/items/{case['item_id']}"
        outcome = result_item(client, path, case, batch, before, run["trace_id"], response.status_code)
        outcome["business_sql_after"] = business_snapshot(sandbox / "sandbox.sqlite3")
        if response.status_code == 200:
            expected_chunks = set(chunk_ids(conn, batch["sku"], batch["document_version"], scores[key]["source_rows"]))
            actual = set(outcome["decision"]["authorized_citations"])
            outcome["authorized_sources_verified"] = expected_chunks == actual
            index = case["item_id"] - 1
            entry = response_row(raw[index], compact=compact) if isinstance(raw, list) and index < len(raw) else None
            if entry is not None:
                observed = outcome["decision"]
                assert observed["raw_refs"] == entry.get("refs")
                assert observed["model"].get("demand") == entry.get("demand")
                assert observed["model"].get("code") == entry.get("decision")
        outcomes[key] = outcome
    if response.status_code != 200:
        write(folder / "http-error.json", {"status": response.status_code, "response": response.json()})
    return outcomes


def summarize(outcomes, scores, traces, frozen):
    # Transport stops count at the SDK request level. Keep the affected row count separately.
    first = score_cases(outcomes, scores, traces, frozen)
    for key, outcome in outcomes.items():
        if outcome["http_status"] != 200:
            continue
        draft = outcome.get("draft")
        if draft and draft["title"] != scores[key].get("draft_title") and outcome["assessment"]["model_correct"]:
            first["server"]["wrong_drafts"] += 1
            outcome["assessment"]["server_valid"] = False
        outcome["assessment"]["server_valid"] &= outcome["authorized_sources_verified"] and outcome["draft_roundtrip_verified"]
        first["server_passed"] &= outcome["assessment"]["server_valid"]
    calls = []
    for trace in traces:
        steps = {step["stage"]: step for step in trace["steps"]}
        completion = steps["model_result"]
        shape = steps.get("model_contract", {}).get("evidence", {})
        calls.append({
            "trace_id": trace["trace_id"], "seconds": completion["duration"],
            "finish_reason": shape.get("finish_reason"), "sdk_error": completion.get("error_code"),
            "items": steps["model_input"]["evidence"]["item_count"],
        })
    first["sdk"] = {"calls": len(calls), "timeouts": sum(c["sdk_error"] == "APITimeoutError" for c in calls),
                    "truncations": sum(c["finish_reason"] == "length" for c in calls), "runs": calls}
    first["model"]["items_affected_by_timeout"] = first["model"].pop("timeouts", 0)
    first["model"]["items_affected_by_truncation"] = first["model"].pop("length_stops", 0)
    first["server"]["items_affected_by_provider_failure"] = first["server"].pop("provider_failures", 0)
    return first


def audit_bindings(folder, frozen, scores, outcomes):
    """Resolve raw reference IDs to physical TSV rows using the captured outgoing item only."""
    audit = {}
    for name, batch in frozen["batches"].items():
        path = folder / name
        request = read(path / "sdk-input.json")["evidence"]["items"]
        response = captured_items(path / "sdk-first-response.json")
        lines = batch["original_tsv"].splitlines()
        original = {line: index for index, line in enumerate(lines, 1)}
        for key, item in zip(batch["item_keys"], request, strict=True):
            outcome = outcomes[key]
            index = item["id"] - 1
            refs = {row["ref"]: original["\t".join(row[column] for column in COLUMNS)]
                    for row in packet_sources(item) if type(row["ref"]) in (int, str)}
            check = {"item_id": item["id"], "local_reference_to_tsv_row": refs,
                     "expected_tsv_rows": scores[key]["source_rows"], "trace_id": outcome["trace_id"]}
            entry = response_row(response[index], compact="tests" in item) if (
                isinstance(response, list) and index < len(response)
            ) else None
            if isinstance(entry, dict) and "decision" in outcome:
                reported = entry.get("refs")
                measured = []
                if isinstance(reported, dict) and set(reported) == {"type", "spec"} and all(
                    isinstance(value, list) for value in reported.values()
                ):
                    for role in ("type", "spec"):
                        for ref in reported[role]:
                            row = refs.get(ref) if type(ref) in (int, str) else None
                            is_type = row is not None and lines[row - 1].startswith("Product type\t")
                            measured.append(row if is_type == (role == "type") else None)
                else:
                    measured = None
                chunks = {int(row["location"].split(":")[1]): row["chunk_id"] for row in outcome["source_rows"]}
                expected_projection = [chunks.get(row) for row in measured] if measured is not None else None
                check.update(
                    raw_id=entry.get("id"),
                    raw_refs=reported,
                    cited_tsv_rows=measured,
                    same_as_server_projection=(expected_projection == outcome["decision"]["model"].get("citations")),
                    citation_correct=(measured is not None and None not in measured
                                      and sorted(measured) == sorted(scores[key]["source_rows"])),
                )
            audit[key] = check
    write(folder / "binding-audit.json", audit)
    return audit


def run(app, database, diagnosis, observability, frozen, scores, lock, folder, sandbox, live):
    from fastapi.testclient import TestClient

    database.DB_PATH = str(sandbox / "sandbox.sqlite3")
    database.init_db()
    observability.TRACE_LOG_FILE = sandbox / "trace.jsonl"
    observability.ERROR_LOG_FILE = sandbox / "errors.jsonl"
    observability.FUNC_LOG_FILE = sandbox / "functions.jsonl"
    outcomes = {}
    with closing(database.get_db()) as conn, closing(TestClient(app)) as client:
        password = token_urlsafe(20)
        with conn:
            conn.execute("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                         (17, "sp-mixed-batch", database.hash_password(password)))
        login = response_json(client.post("/api/login", json={"username": "sp-mixed-batch", "password": password}), 200)
        client.headers["Authorization"] = "Bearer " + login["data"]["token"]
        for name in frozen["batches"]:
            outcomes.update(run_batch(client, conn, diagnosis, sandbox, frozen, scores, lock, name,
                                      folder / name, live=live))
            write(folder / "cases.json", outcomes)
        attack = None
        if not live:
            attack_batch = next(reversed(frozen["batches"]))
            target = {scores[key]["code"]: key for key in frozen["batches"][attack_batch]["item_keys"]}
            attack = run_batch(client, conn, diagnosis, sandbox, frozen, scores, lock, attack_batch,
                               folder / "cross-item-replay", live=False, inject=target)
            write(folder / "cross-item-replay" / "cases.json", attack)
    traces, requests = collect_trace(folder, "live" if live else "fixed", source=observability.TRACE_LOG_FILE)
    primary_ids = {outcome["trace_id"] for outcome in outcomes.values()}
    result = summarize(outcomes, scores, [trace for trace in traces if trace["trace_id"] in primary_ids], frozen)
    bindings = audit_bindings(folder, frozen, scores, outcomes)
    result["server_passed"] &= all(row.get("same_as_server_projection", True) for row in bindings.values())
    assert len(primary_ids) == len(frozen["batches"]) and requests == len(primary_ids) + int(not live)
    if attack is not None:
        required_rejections = {
            target["supported_missing"]: {"demand_mismatch"},
            target["unattributed"]: {"unattributed", "citation_mismatch"},
            target["unverified"]: {"citation_mismatch"},
        }
        attack_ok = all(
            row["http_status"] == 200 and row["original_row_verified"] and row["metrics_verified"]
            and row["authorized_sources_verified"] and row["business_sql_before"] == row["business_sql_after"]
            and row["copy_http"] == row["edit_http"] == 404 and not row["draft"]
            and row["attribution"] == ("ad_group" if frozen["cases"][key]["group"] == "mixed" else "product")
            and (row["code"] == "model_rejected" and required_rejections[key].issubset(row["decision"]["rejections"])
                 if key in required_rejections else not row["decision"]["rejections"])
            for key, row in attack.items()
        )
        result["cross_item_replay"] = {"passed": attack_ok,
                                       "injections_blocked": len(required_rejections) if attack_ok else 0}
        result["server_passed"] &= attack_ok
    result["passed"] = result["server_passed"] and result["model"]["correct"] == len(outcomes)
    write(folder / "cases.json", outcomes)
    write(folder / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fixed", "live"))
    parser.add_argument("--scenario", choices=("baseline", "development", "holdout", "transfer-v2"), default="baseline")
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    root = args.output_dir.resolve()
    live = args.mode == "live"
    lock = read(args.lock)
    name = {"baseline": "mixed_batch_holdout", "development": "mixed_transfer_development",
            "holdout": "mixed_transfer_holdout", "transfer-v2": "mixed_transfer_v2_holdout"}[args.scenario]
    frozen = freeze(read(FIXTURES / f"{name}.json"))
    scores = read(FIXTURES / f"{name}_scores.json")
    assert set(frozen["cases"]) == set(scores)
    if live:
        assert read(root / "fixed" / "result.json")["passed"]
        assert read(root / "fixed" / "frozen.json") == frozen
        assert read(root / "fixed" / "scores.json") == scores
        assert read(root / "fixed" / "protocol.json") == lock
    folder = root / args.mode
    folder.mkdir(parents=True, exist_ok=False)
    write(folder / "frozen.json", frozen)
    write(folder / "scores.json", scores)
    write(folder / "protocol.json", lock)
    os.environ.update(LLM_PROVIDER="glm" if live else "mock", JWT_SECRET=token_urlsafe(48),
                      DIAGNOSIS_MYSQL_ENABLED="false", DIAGNOSIS_SANDBOX_DB_PATH="")
    if not live:
        os.environ["AD_OFFLINE_ACCEPTANCE"] = "1"
    with TemporaryDirectory(prefix="sandbox-", dir=folder) as directory:
        sandbox = Path(directory)
        os.environ["RAG_DB_PATH"] = str(sandbox / "rag")
        from backend import observability
        from backend.agent import search_term_diagnosis as diagnosis
        from backend.database import database
        from fast_server import app, settings

        assert_lock(lock, diagnosis, settings)
        result = run(app, database, diagnosis, observability, frozen, scores, lock, folder, sandbox, live)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
