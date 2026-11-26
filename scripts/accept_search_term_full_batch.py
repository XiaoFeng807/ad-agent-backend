"""Freeze and reconcile two 24-item v22 runs through the search-term HTTP endpoints."""

import argparse
import csv
import io
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from scripts.accept_search_term_generalization import FIXTURES, GROUPS, business_snapshot, chunk_ids, response_json
from scripts.accept_search_term_mixed_batches import (
    COLUMNS, assert_lock, freeze, packet_sources, replay_row, response_row, seed_batch,
)
from scripts.accept_search_terms import collect_trace, read, sha, write


BASE = FIXTURES / "full_batch24"
PREVIOUS_LOCK = Path("output/search-term-mixed-transfer-v2/locked-protocol.json")
METRICS = ("impressions", "clicks", "orders", "spend_cents", "sales_cents")
TESTS = ("impressions", "clicks", "orders", "cvr")
FAULTS = ("cross_item", "read_timeout", "length", "missing_stop", "child_failure",
          "version_change", "ownership_change", "time_budget", "call_budget")


def sql_oracle(conn, batch):
    rows = list(csv.DictReader(io.StringIO(batch["report_csv"])))
    columns = ("Campaign ID", "Ad Group ID", "Customer Search Term", "Impressions", "Clicks", "Purchases", "Spend", "Sales")
    with closing(sqlite3.connect(":memory:")) as sql:
        sql.row_factory = sqlite3.Row
        sql.executescript("""
            CREATE TABLE csv_rows(line INT, campaign TEXT, ad_group TEXT, term TEXT, impressions INT,
                                  clicks INT, purchases INT, spend NUMERIC, sales NUMERIC);
            CREATE TABLE membership(ad_group TEXT, sku TEXT);
            CREATE TABLE row_links(line INT, ad_group TEXT, sku TEXT);
        """)
        sql.executemany("INSERT INTO csv_rows VALUES (?,?,?,?,?,?,?,?,?)",
                        [(line, *(row[key] for key in columns)) for line, row in enumerate(rows, 2)])
        sql.executemany("INSERT INTO membership VALUES (?,?)", conn.execute(
            "SELECT ad_group_id, sku FROM search_term_amazon_membership ORDER BY ad_group_id, sku"))
        sql.executemany("INSERT INTO row_links VALUES (?,?,?)", conn.execute(
            "SELECT line, ad_group_id, sku FROM search_term_amazon_row_links WHERE report_sha256=?",
            (batch["report_sha256"],)))
        result = [dict(row) for row in sql.execute(BASE.with_suffix(".sql").read_text("utf-8"), {"sku": batch["sku"]})]
    return result


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_response(content):
    content = content.strip()
    if fenced := re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL):
        content = fenced[1]
    try:
        return json.loads(content)
    except ValueError:
        return None


class SDKLedger:
    """Audit the SDK boundary and store only synthetic inputs and visible response content."""

    def __init__(self, batch, cases, scores, sql, lock, question, folder, *, sdk=None, fault=None, mutate=None,
                 serial=False):
        self.batch, self.cases, self.scores = batch, cases, scores
        self.sql, self.lock, self.question, self.folder = sql, lock, question, folder
        self.sdk, self.fault, self.mutate = sdk, fault, mutate
        self.calls, self.errors, self.receipts = [], [], set()
        self.local_refs = set()
        self.clock_offset = 0
        slices = [(0, batch["item_keys"])] if not serial else []
        for attempt in range(1, 5):
            keys = batch["item_keys"][(attempt - 1) * 6:attempt * 6]
            slices.append((attempt, keys))
        self.slices = slices

    def __call__(self, provider, budget, **kwargs):
        attempt, keys = self.slices[len(self.calls)]
        record = {"attempt": attempt}
        self.calls.append(record)
        folder = self.folder / f"sdk-{attempt}"
        folder.mkdir()
        messages = kwargs["messages"]
        evidence = json.loads(messages[-1]["content"])
        config = {key: kwargs[key] for key in ("model", "temperature", "sdk_max_retries", "max_tokens", "timeout",
                                               "response_format", "stream")}
        config["thinking"] = kwargs.get("extra_body", {}).get("thinking", {}).get("type")
        write(folder / "input.json", {"messages": messages, "config": config})
        try:
            mapping, expected = self.check_input(attempt, keys, messages, evidence, config)
        except Exception as error:
            self.errors.append(repr(error))
            raise
        write(folder / "binding.json", mapping)
        record["original_ids"] = [row["original_id"] for row in mapping]
        record["started_at"] = utc_now()
        start = perf_counter()
        try:
            if self.sdk:
                stream = self.sdk(provider, budget, **kwargs)
            else:
                stream = self.replay(attempt, evidence["receipt"], expected)
        except Exception as error:
            self.finish(folder, record, start, error=error)
            raise
        return self.capture(folder, record, start, stream)

    def check_input(self, attempt, keys, messages, evidence, config):
        assert 0 <= attempt <= 4
        assert sha(messages[0]["content"].encode()) == self.lock["batch_protocol"]["prompt_sha256"]
        assert messages[1]["content"] == self.question
        assert set(evidence) == {"receipt", "rules", "items"} and evidence["rules"] == self.lock["protocol"]["rules"]
        assert evidence["receipt"] not in self.receipts
        self.receipts.add(evidence["receipt"])
        assert config["temperature"] == config["sdk_max_retries"] == 0
        assert config["stream"] and config["response_format"] == {"type": "json_object"}
        assert 0 < config["timeout"] <= self.lock["protocol"]["sdk_timeout_seconds"]
        assert config["max_tokens"] == self.lock["sdk_max_tokens_by_item_count"][str(len(keys))]
        if self.sdk:
            assert config["thinking"] == "enabled" and config["model"] == self.lock["protocol"]["model"]
        source = self.batch["original_tsv"].splitlines()
        mapping, replay = [], []
        for local_id, (key, item) in enumerate(zip(keys, evidence["items"], strict=True), 1):
            case, score = self.cases[key], self.scores[key]
            row = self.sql[case["item_id"] - 1]
            withheld = bool(attempt >= 3 and scope_fault(self.fault, row))
            attribution = "ad_group" if withheld else row["attribution"]
            assert item["id"] == local_id and item["term"] == case["term"] and item["requested"] == score["demand"], key
            assert item["sample_scope"] == ("verified_product" if attribution == "product" else "ad_group_only"), key
            assert item["tests"] == {
                "impressions": {"actual": row["impressions"], "minimum": 100},
                "clicks": {"actual": row["clicks"], "minimum": 20},
                "orders": {"actual": row["orders"], "minimum": 2},
                "cvr": {"actual": 100 * row["orders"], "minimum": 10 * row["clicks"]},
            }
            below = [test for test in TESTS if row[f"failed_{test}"]]
            allowed = None if withheld else score["offered_rows"]
            assert item["original_title"] == (self.batch["title"] if allowed and not below else None)
            assert item["source_access"] == ("withheld_ownership" if attribution == "ad_group" else
                                             "available_for_review" if allowed else "withheld_base_sample")
            assert (item["sources"] is not None) == (allowed is not None)
            by_ref = {}
            for packet in packet_sources(item):
                line = source.index("\t".join(packet[col] for col in COLUMNS)) + 1
                ref = packet["ref"]
                assert isinstance(ref, str) and ref.startswith(f"{local_id}r") and ref not in self.local_refs
                self.local_refs.add(ref)
                by_ref[ref] = {"line": line, "role": "type" if line == 2 else "spec", "sku": case["sku"],
                               "version": self.batch["document_version"]}
            assert sorted(entry["line"] for entry in by_ref.values()) == sorted(allowed or []), key
            assert item["allowed_ref_ids"] == {
                role: [ref for ref, source in by_ref.items() if source["role"] == role] for role in ("type", "spec")
            }
            mapping.append({"case": key, "original_id": case["item_id"], "local_id": local_id,
                            "report_sha256": self.batch["report_sha256"], "report_lines": case["physical_lines"],
                            "source_rows": [[self.batch["report_sha256"], None, line] for line in case["physical_lines"]],
                            "references": by_ref, "evidence_withheld": withheld})
            refs = {role: [ref for ref, entry in by_ref.items() if entry["line"] in lines]
                    for role, lines in score["required_references"].items()}
            replay.append(replay_row({"id": local_id, "demand": score["demand"], "below_minimum": below,
                                      "decision": "unattributed" if withheld else score["code"], "refs": refs}, named=True))
        return mapping, replay

    def replay(self, attempt, receipt, items):
        fault = self.fault
        if fault == "cross_item":
            items[0]["requested"] = items[1]["requested"]
            items[5]["refs"] = items[4]["refs"]
            items[6].update(refs=items[0]["refs"], code=items[0]["code"])
        content = json.dumps({"receipt": receipt, "items": items})

        def chunk(text="", finish=None):
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish)])

        if attempt == 0 and fault and fault != "cross_item":
            yield chunk(content)
            if fault == "read_timeout":
                raise httpx.ReadTimeout("fixed read timeout")
            if fault != "missing_stop":
                yield chunk(finish="length")
            return
        if attempt == 2 and fault == "child_failure":
            yield chunk(content)
            raise httpx.RemoteProtocolError("fixed child disconnect")
        if attempt == 2 and fault == "time_budget":
            self.clock_offset = 301
        if attempt == 2 and self.mutate:
            self.mutate()
        yield chunk(content, "stop")

    def capture(self, folder, record, start, stream):
        visible, usage, finish, error = [], None, None, None
        try:
            for chunk in stream:
                if "first_chunk_at" not in record:
                    record.update(first_chunk_at=utc_now(), first_chunk_seconds=perf_counter() - start)
                usage = getattr(chunk, "usage", None) or usage
                if chunk.choices:
                    choice = chunk.choices[0]
                    finish = choice.finish_reason or finish
                    text = getattr(choice.delta, "content", None)
                    if text:
                        visible.append(text)
                yield chunk
        except Exception as exception:
            error = exception
            raise
        finally:
            try:
                if close := getattr(stream, "close", None):
                    close()
            finally:
                self.finish(folder, record, start, content="".join(visible), usage=usage, finish=finish, error=error)

    def finish(self, folder, record, start, *, content="", usage=None, finish=None, error=None):
        record.update(ended_at=utc_now(), sdk_seconds=perf_counter() - start, finish_reason=finish,
                      first_chunk_at=record.get("first_chunk_at"), first_chunk_seconds=record.get("first_chunk_seconds"),
                      sdk_error=type(error).__name__ if error else None,
                      http_status=getattr(error, "status_code", None),
                      usage={k: getattr(usage, k, None) for k in ("prompt_tokens", "completion_tokens", "total_tokens")})
        write(folder / "response.json", {"content": content, **record})


def wire_scores(folder, scores, sql):
    outgoing = read(folder / "input.json")
    receipt = json.loads(outgoing["messages"][-1]["content"])["receipt"]
    response = read(folder / "response.json")
    answer = parse_response(response["content"])
    rows = answer.get("items") if isinstance(answer, dict) else None
    decoded = [response_row(row, compact=True) if isinstance(row, dict) else None
               for row in rows] if isinstance(rows, list) else []
    binding = read(folder / "binding.json")
    normal = response["finish_reason"] == "stop" and response["sdk_error"] is None
    envelope = isinstance(answer, dict) and answer.get("receipt") == receipt
    alignment = [row.get("id") if row else None for row in decoded] == [row["local_id"] for row in binding]
    results = {}
    for index, bound in enumerate(binding):
        key = bound["case"]
        score, stats = scores[key], sql[bound["original_id"] - 1]
        entry = decoded[index] if index < len(decoded) and decoded[index] else {}
        refs = entry.get("refs")
        lines = None
        if isinstance(refs, dict) and set(refs) == {"type", "spec"} and all(isinstance(v, list) for v in refs.values()):
            lines = {}
            for role, ids in refs.items():
                sources = [bound["references"].get(ref) if isinstance(ref, str) else None for ref in ids]
                lines[role] = [row["line"] if row and row["role"] == role else None for row in sources]
        needed = score["required_references"]
        refs_correct = lines is not None and all(Counter(needed[role]) == Counter(lines[role]) for role in needed)
        received = {(role, line) for role, found in (lines or {}).items() for line in found}
        required = {(role, line) for role, wanted in needed.items() for line in wanted}
        usable = normal and envelope and alignment
        code = entry.get("decision")
        failed = entry.get("below_minimum")
        checks = {
            "normal_stop": normal, "receipt": envelope, "alignment": alignment,
            "demand": entry.get("demand") == score["demand"],
            "failed": isinstance(failed, list) and all(isinstance(test, str) for test in failed)
            and Counter(failed) == Counter(test for test in TESTS if stats[f"failed_{test}"]),
            "code": code == score["code"], "references": refs_correct,
        }
        observed_action = ("title" if code == "supported_missing" else "review" if code == "fact_conflict" else
                           "observe" if code else None)
        results[key] = {"strict": all(checks.values()), "action_correct": usable and observed_action == score["action"],
                        "required_rows_complete": usable and required <= received, "checks": checks,
                        "required_physical_rows": len(required), "required_rows_hit": len(required & received) if usable else 0,
                        "unique_citations": len(received) if usable else 0,
                        "raw": entry, "resolved_lines": lines, "binding": {**bound, "attempt": response["attempt"]}}
    return results


def change_scope(database, sandbox, batch, frozen, fault):
    with closing(database.get_db()) as conn, conn:
        if fault == "ownership_change":
            first_row = next(csv.DictReader(io.StringIO(batch["report_csv"])))
            conn.execute("DELETE FROM search_term_amazon_membership WHERE ad_group_id=?",
                         (first_row["Ad Group ID"],))
        elif fault == "version_change":
            from backend.rag import product_evidence

            member = frozen["membership"]
            target = {"shop_id": member["shop"], "marketplace_id": member["marketplace"], "sku": batch["sku"]}
            file = "new-specifications.tsv"
            lines = batch["original_tsv"].splitlines(keepends=True)
            (sandbox / file).write_text("".join(lines[:2]), encoding="utf-8")
            product_evidence.import_release(
                conn, {"target": target, "version": "v-next", "documents": [{"id": "spec", "kind": "specs", "file": file}]},
                sandbox, embedding=lambda texts: [[1.0] for _ in texts], model_id="synthetic-fixture-non-vector",
            )
            product_evidence.activate(conn, target, "v-next")
    return business_snapshot(sandbox / "sandbox.sqlite3")


def scope_fault(fault, row):
    if fault == "version_change" and row["attribution"] == "product":
        return "product_version_changed"
    if fault == "ownership_change" and row["ad_group"].endswith("-verified"):
        return "ownership_changed"
    return None


def verify_saved(client, conn, url, batch, frozen, scores, sql, final_wire, expected_incomplete):
    results = {}
    report_rows = list(csv.DictReader(io.StringIO(batch["report_csv"])))
    for key, stats in zip(batch["item_keys"], sql, strict=True):
        case, score = frozen["cases"][key], scores[key]
        item_id = case["item_id"]
        path = f"{url}/items/{item_id}"
        detail = response_json(client.get(path), 200)
        item, draft = detail["item"], detail["draft"]
        copy = client.get(path + "/draft/copy?revision=1")
        edit = client.put(path + "/draft", json={"revision": 1, "title": draft["title"] if draft else batch["title"]})
        checked = final_wire.get(key, {})
        changes = expected_incomplete.get(key)
        incomplete = item["action"] == "incomplete"
        original = (
            detail["report"]["sha256"] == batch["report_sha256"] and item["id"] == item_id
            and item["term"] == case["term"] and item["scope"]["campaign_id"] == stats["campaign"]
            and item["scope"]["ad_group_id"] == stats["ad_group"] and stats["item_id"] == item_id
            and [row["line"] for row in detail["rows"]] == case["physical_lines"]
            and case["physical_lines"] == [int(line) for line in stats["lines"].split(",")]
            and [row["values"] for row in detail["rows"]] == [report_rows[line - 2] for line in case["physical_lines"]]
            and all(item["metrics"][field] == stats[field] for field in METRICS)
            and item["demand"] == score["demand"]
        )
        if changes != "ownership_changed":
            original &= item["attribution"] == stats["attribution"]
        if item["attribution"] == "product":
            original &= (detail["product"]["target"]["sku"] == batch["sku"]
                         and [proof["line"] for proof in item["ownership"]] == case["physical_lines"]
                         and all(proof["sku"] == batch["sku"] and proof["ad_group_id"] == stats["ad_group"]
                                 for proof in item["ownership"]))
        else:
            original &= not item["ownership"]
        chunks = item["citations"]
        source = batch["original_tsv"].splitlines()
        cited = []
        for chunk in chunks:
            row = int(chunk["locations"][0]["location"].rsplit(":", 1)[1])
            original &= (chunk["sku"] == batch["sku"] and chunk["version"] == batch["document_version"]
                         and chunk["version"] == detail["product"]["document_version"]
                         and chunk["content"] == source[row - 1]
                         and chunk_ids(conn, batch["sku"], batch["document_version"], [row]) == [chunk["chunk_id"]])
            cited.append(row)
        blocked = incomplete or item["code"] == "model_rejected"
        expected_pass = checked.get("strict", False) and not changes
        released = not blocked
        roundtrip = (copy.status_code == edit.status_code == 200 and copy.json() == edit.json() == draft if draft
                     else copy.status_code == edit.status_code == 404)
        valid = original and roundtrip
        if released:
            observed = item["model_validation"]
            binding = checked.get("binding", {})
            saved_binding = ({"attempt": binding.get("attempt"), "local_id": binding.get("local_id")}
                             if binding.get("attempt") else None)
            valid &= (
                expected_pass and item["action"] == score["action"] and item["code"] == score["code"]
                and sorted(cited) == sorted(score["source_rows"]) and bool(item.get("review")) == (score["action"] == "review")
                and (draft or {}).get("title") == score.get("draft_title")
                and item.get("batch_binding") == saved_binding and observed["model"].get("id") == binding["local_id"]
                and observed["model"].get("demand") == checked["raw"].get("demand")
                and observed["raw_refs"] == checked["raw"].get("refs") and not observed["rejections"]
                and observed["server_code"] == score["code"] and bool(item["reason"])
            )
        else:
            valid &= not expected_pass and not draft and not item.get("review")
            if incomplete:
                valid &= not chunks and not item.get("source_chunks") and "诊断未完成" in item["reason"]
            if changes:
                valid &= incomplete and item["incomplete_cause"] == changes
        results[key] = {"strict": bool(valid and released), "action_correct": released and item["action"] == score["action"],
                        "valid": bool(valid), "wrong_release": released and not expected_pass, "blocked": blocked,
                        "incomplete": incomplete, "original_verified": bool(original),
                        "cited_lines": cited, "copy_http": copy.status_code, "edit_http": edit.status_code,
                        "saved": {"item": item, "draft": draft, "entry_id": detail["entry_id"],
                                  "document_version": (detail["product"] or {}).get("document_version")}}
    return results


def summarize(rows, *fields):
    return {field: sum(row[field] for row in rows.values()) for field in fields}


def run_one(client, conn, diagnosis, database, sandbox, frozen, scores, lock, name, folder, *, live, fault=None,
            serial=False):
    batch = frozen["batches"][name]
    seed_batch(conn, sandbox, frozen, batch)
    sql = sql_oracle(conn, batch)
    folder.mkdir()
    write(folder / "independent-sql.json", sql)
    assert len(sql) == 24
    before = business_snapshot(sandbox / "sandbox.sqlite3")
    baseline = before
    created = response_json(client.post(GROUPS, json={"title": folder.name}), 201)
    conversation = f"{GROUPS}/{created['data']['conversation_id']}"
    api = conversation + "/search-terms"
    report = response_json(client.post(api + "/reports", params={"filename": name + ".csv"}, headers={"Content-Type": "text/csv"},
                                       content=batch["report_csv"].encode()), 201)["report"]
    assert report["sha256"] == batch["report_sha256"] and report["batch_count"] == 1
    assert (report["row_count"], report["term_count"]) == (25, 24)

    def mutate():
        nonlocal baseline
        baseline = change_scope(database, sandbox, batch, frozen, fault)

    audit = SDKLedger(batch, frozen["cases"], scores, sql, lock, frozen["question"], folder,
                      sdk=diagnosis.create_completion if live else None, fault=fault,
                      mutate=mutate if fault in ("version_change", "ownership_change") else None, serial=serial)
    infer = diagnosis.infer
    with (patch.object(diagnosis, "create_completion", side_effect=audit),
          patch.object(diagnosis, "infer", side_effect=lambda *args, **kwargs: infer(*args, **kwargs, serial=serial)),
          patch.object(diagnosis, "perf_counter", side_effect=lambda: perf_counter() + audit.clock_offset),
          patch.object(diagnosis, "CLAIM_RECOVERY_CALLS", 2 if fault == "call_budget" else diagnosis.CLAIM_RECOVERY_CALLS)):
        started = perf_counter()
        response = client.post(api + "/runs", json={"import_id": report["import_id"], "sku": batch["sku"],
                                                    "message": frozen["question"]})
        payload = response_json(response, 200)
        result_url = api + "/runs/" + payload["run_id"]
        first_detail = response_json(client.get(result_url + "/items/1"), 200)
        elapsed = perf_counter() - started
    assert not audit.errors, audit.errors
    assert 1 <= len(audit.calls) <= 5
    after = business_snapshot(sandbox / "sandbox.sqlite3")
    def score_calls(calls):
        return {key: row for call in calls
                for key, row in wire_scores(folder / f"sdk-{call['attempt']}", scores, sql).items()}

    first_wire = score_calls(audit.calls if serial else audit.calls[:1])
    final_wire = score_calls(audit.calls[1:]) if not serial and len(audit.calls) > 1 else first_wire
    incomplete = {}
    for key, stats in zip(batch["item_keys"], sql, strict=True):
        i = stats["item_id"]
        if fault == "child_failure" and 7 <= i <= 12:
            incomplete[key] = "stream_interrupted"
        if fault == "time_budget" and i > 6:
            incomplete[key] = "batch_deadline" if i <= 12 else "recovery_budget"
        if fault == "call_budget" and i > 12:
            incomplete[key] = "recovery_budget"
        if cause := scope_fault(fault, stats):
            incomplete[key] = cause
    saved = verify_saved(client, conn, result_url, batch, frozen, scores, sql, final_wire, incomplete)
    history = response_json(client.get(conversation + "/history"), 200)
    refreshed = response_json(client.get(conversation + "/history"), 200)
    replies = [row for row in history["data"]["entries"] if "search_terms" in row]
    single_reply = len(replies) == 1 and [item["id"] for item in replies[0]["search_terms"]] == list(range(1, 25))
    unique_entry = {row["saved"]["entry_id"] for row in saved.values()} == {first_detail["entry_id"]}
    count = conn.execute("SELECT count(*) FROM chat_group_entries WHERE run_id=? AND kind='assistant'",
                         (payload["run_id"],)).fetchone()[0]
    business_ok = single_reply and unique_entry and count == 1 and refreshed == history and baseline == after
    final = summarize(saved, "strict", "action_correct", "blocked", "wrong_release", "incomplete")
    first = summarize(first_wire, "strict", "action_correct", "required_rows_complete", "unique_citations")
    first["denominator"] = final["denominator"] = 24
    first["required_physical_rows"] = sum(len(scores[key]["source_rows"]) for key in batch["item_keys"])
    first["verified_physical_rows"] = sum(row["required_rows_hit"] for row in first_wire.values())
    first["citation_set_exact"] = sum(row["checks"]["normal_stop"] and row["checks"]["receipt"]
                                      and row["checks"]["alignment"] and row["checks"]["references"]
                                      for row in first_wire.values())
    first["citation_precision"] = (first["verified_physical_rows"] / first["unique_citations"]
                                   if first["unique_citations"] else None)
    final["required_physical_rows"] = first["required_physical_rows"]
    final["verified_physical_rows"] = sum(len(saved[key]["cited_lines"]) for key in saved if saved[key]["strict"])
    final["required_rows_complete"] = sum(row["strict"] and bool(final_wire[key]["required_rows_complete"])
                                          for key, row in saved.items())
    usage = {key: [call["usage"][key] for call in audit.calls] for key in audit.calls[0]["usage"]}
    tokens = {key: {"reported_tokens": sum(v for v in values if v is not None), "missing_calls": values.count(None)}
              for key, values in usage.items()}
    averages = {key: round(value["reported_tokens"] / final["strict"], 2)
                if final["strict"] and not value["missing_calls"] else None for key, value in tokens.items()}
    result = {**payload, "fault": fault, "strategy": "4x6" if serial else "24", "http_status": response.status_code,
              "first": first, "final": final, "sdk_calls": audit.calls,
              "recovery": final if not serial and len(audit.calls) > 1 else None,
              "submit_to_read_seconds": elapsed, "usage_totals": tokens, "tokens_per_verified_item": averages,
              "business_sql": {"before": before, "after_injected_change": baseline, "after": after},
              "single_reply_and_refresh": business_ok, "server_passed": business_ok and all(row["valid"] for row in saved.values())}
    if not live:
        n_calls = 1 if fault in (None, "cross_item") else 3 if fault in ("time_budget", "call_budget") else 5
        n_final = 21 if fault == "cross_item" else 24 - len(incomplete)
        result["passed"] = (result["server_passed"] and len(audit.calls) == n_calls and final["strict"] == n_final
                            and first["strict"] == (24 if fault is None else 21 if fault == "cross_item" else 0)
                            and final["incomplete"] == len(incomplete))
    else:
        result["passed"] = result["server_passed"] and len(audit.calls) == (4 if serial else 1)
        result["all_strict"] = first["strict"] == final["strict"] == 24
    write(folder / "cases.json", {key: {"expected": scores[key], "sql": sql[index], "first": first_wire.get(key),
                                       "final_wire": final_wire.get(key), "saved_result": saved[key]}
                                  for index, key in enumerate(batch["item_keys"])})
    write(folder / "result.json", result)
    print(folder.name, json.dumps({key: result[key] for key in ("first", "final", "passed")}, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fixed", "live"))
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    live, root = args.mode == "live", args.output_dir.resolve()
    folder = root / args.mode
    folder.mkdir(parents=True)
    frozen = freeze(read(BASE.with_suffix(".json")))
    scores = read(BASE.with_name(BASE.name + "_scores.json"))
    lock = read(PREVIOUS_LOCK)
    lock["source_sha256"] = {path: sha(Path(path).read_bytes()) for path in lock["source_sha256"]}
    lock["full_batch_recovery"] = {"items": 24, "child_size": 6, "child_calls": 4, "seconds": 300,
                                   "sql_sha256": sha(BASE.with_suffix(".sql").read_bytes())}
    assert set(frozen["cases"]) == set(scores) and len(scores) == 48
    for name, data in (("frozen", frozen), ("scores", scores), ("protocol", lock)):
        if live:
            assert read(root / "fixed" / f"{name}.json") == data
        write(folder / f"{name}.json", data)
    if live:
        assert read(root / "fixed" / "result.json")["passed"]
    os.environ.update(LLM_PROVIDER="glm" if live else "mock", JWT_SECRET=token_urlsafe(48),
                      DIAGNOSIS_MYSQL_ENABLED="false", DIAGNOSIS_SANDBOX_DB_PATH="")
    if not live:
        os.environ["AD_OFFLINE_ACCEPTANCE"] = "1"
    else:
        os.environ.pop("AD_OFFLINE_ACCEPTANCE", None)
    with TemporaryDirectory(prefix="sandbox-", dir=folder) as directory:
        sandbox = Path(directory)
        os.environ["RAG_DB_PATH"] = str(sandbox / "rag")
        from backend import observability
        from backend.agent import search_term_diagnosis as diagnosis
        from backend.database import database
        from fast_server import app, settings

        assert_lock(lock, diagnosis, settings)
        assert (diagnosis.CLAIM_RECOVERY_SIZE, diagnosis.CLAIM_RECOVERY_CALLS, diagnosis.CLAIM_RECOVERY_SECONDS) == (6, 4, 300)
        if live and not settings.llm_config("glm").api_key:
            raise SystemExit("GLM_API_KEY required")
        database.DB_PATH = str(sandbox / "sandbox.sqlite3")
        database.init_db()
        observability.TRACE_LOG_FILE = sandbox / "trace.jsonl"
        observability.ERROR_LOG_FILE = sandbox / "errors.jsonl"
        observability.FUNC_LOG_FILE = sandbox / "functions.jsonl"
        results = {}
        with closing(database.get_db()) as conn, closing(TestClient(app)) as client:
            password = token_urlsafe(20)
            with conn:
                conn.execute("INSERT INTO users (id, username, password) VALUES (?, ?, ?)",
                             (17, "sp-full-batch24", database.hash_password(password)))
            login = response_json(client.post("/api/login", json={"username": "sp-full-batch24", "password": password}), 200)
            client.headers["Authorization"] = "Bearer " + login["data"]["token"]
            scenarios = [(name, None, serial) for name in frozen["batches"]
                         for serial in ((False, True) if live else (False,))]
            if not live:
                scenarios.extend((next(iter(frozen["batches"])), fault, False) for fault in FAULTS)
            for name, fault, serial in scenarios:
                key = fault or (f"{name}-4x6" if serial else name)
                results[key] = run_one(client, conn, diagnosis, database, sandbox, frozen, scores, lock,
                                       name, folder / key, live=live, fault=fault, serial=serial)
                write(folder / "result.json", {"passed": all(row["passed"] for row in results.values()), "runs": results})
        traces, requests = collect_trace(folder, args.mode, source=observability.TRACE_LOG_FILE)
        assert len(traces) == len(results) and requests == sum(len(row["sdk_calls"]) for row in results.values())
    return 0 if all(row["passed"] for row in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
