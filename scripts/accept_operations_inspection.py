"""Run frozen StorePilot inspection through authenticated SSE and audit paired GLM rounds."""

import argparse
import csv
import json
import os
import sqlite3
import subprocess
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from io import StringIO
from itertools import combinations
from math import ceil
from pathlib import Path
from secrets import token_urlsafe
from statistics import median
from threading import Event, Thread
from time import perf_counter_ns
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from scripts import accept_task45_t1 as previous
from scripts import operations_inspection_fixture as fixture
from scripts import task45_t1_fixture as base
from scripts.accept_task42_t1 import configure, read, serve, write
from scripts.accept_task45_t3 import Replay as RecordingReplay, job, tool
from scripts.run_real_diagnosis import Redactor
from scripts.run_task27_t5 import ENDPOINT, GLMAudit, MODEL, browser_env
from scripts.task45_t2_observe import Observation, phase


SALES, CURRENT = previous.SALES, previous.CURRENT
REPORT, SOURCES = "get_inspection_report", "get_inspection_sources"
SQL = Path(__file__).with_name("operations_inspection_oracle.sql")
FROZEN_CLOCK = datetime.fromisoformat(base.AS_OF + "T12:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
PROTECTED = (
    "listing_test_shops", "listing_test_products", "listing_test_facts", "listing_test_required_facts",
    "listing_test_sales", "product_document_releases", "product_documents", "product_document_chunks",
    "search_term_report_imports", "search_term_ad_groups", "search_term_amazon_membership", "search_term_amazon_row_links",
    "search_term_title_drafts", "listing_change_proposals", "listing_change_receipts", "ad_plans",
    "chat_group_pause_proposals", "chat_group_pause_receipts", "diagnosis_pause_proposals", "diagnosis_pause_receipts",
)


def snapshot(path):
    with closing(previous.sql_connection(path)) as conn:
        return {name: [tuple(h.hex() if isinstance(h, bytes) else h for h in row)
                       for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in PROTECTED}


def digest(value):
    return sha256(value).hexdigest()


def freeze(output):
    manifest = {"question": fixture.QUESTION, "simple": base.SIMPLE, "clock": FROZEN_CLOCK.isoformat(),
                "windows": base.WINDOWS, "threshold_pct": fixture.THRESHOLD, "oracle": previous.digest(SQL),
                "glm": {"model": MODEL, "paired_repetitions_per_case": fixture.REPETITIONS, "orders": ["AB", "BA"],
                        "cases": fixture.CASES, "score_fields": fixture.SCORE_FIELDS, "points_per_field": 1,
                        "failure_policy": "Every scheduled round counts; preserve initial raw output and original errors.",
                        "p95": "nearest rank", "time": "request send to actual ASGI SSE terminal event",
                        "cohort": {"A": "direct tools", "B": "direct tools or autonomous delegation"}},
                "cases": {}}
    for case in (*fixture.CASES, *fixture.CONTROLS):
        folder = output / "fixtures" / case
        folder.mkdir(parents=True)
        write(folder / "products.json", fixture.source(case))
        write(folder / "advertised-products.json", fixture.membership())
        raw = fixture.report(case)
        (folder / "report.csv").write_bytes(raw)
        links = [] if case == "unattributed" else [
            [base.SHOP, base.MARKET, digest(raw), "", line, "SP-101", "AG-101", base.TOTE]
            for line in range(2, len(raw.splitlines()) + 1)]
        write(folder / "row-links.json", links)
        manifest["cases"][case] = {item.name: previous.digest(item) for item in sorted(folder.iterdir())}
    path = output / "frozen.json"
    write(path, manifest)
    return path


def prepare(path, directory, database, password):
    previous.seed(path, database, password, read(directory / "products.json"))
    members = read(directory / "advertised-products.json")
    links = read(directory / "row-links.json")
    with closing(database.get_db()) as conn, conn:
        conn.executemany("INSERT INTO search_term_amazon_membership VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         [(*row, previous.digest(directory / "advertised-products.json"), f"fixture membership:{i}")
                          for i, row in enumerate(members, 2)])
        conn.executemany("INSERT INTO search_term_amazon_row_links VALUES (?,?,?,?,?,?,?,?,?,?)",
                         [(*row, previous.digest(directory / "row-links.json"), f"fixture row link:{i}")
                          for i, row in enumerate(links, 2)])


def oracle(path, conversation, case):
    params = {"conversation": conversation, "shop": base.SHOP, "market": base.MARKET, "threshold": fixture.THRESHOLD,
              **dict(zip(base.KEYS, base.WINDOWS))}
    with closing(previous.sql_connection(path)) as conn:
        conn.execute("""CREATE TEMP TABLE raw_report (
            sha256, line, sheet, campaign, ad_group, term, day, impressions, clicks, orders, spend, sales)""")
        for report in conn.execute("SELECT sha256, raw_csv FROM search_term_report_imports WHERE conversation_id=?",
                                   (conversation,)):
            reader = csv.DictReader(StringIO(report["raw_csv"].decode("utf-8-sig")))
            columns = ("Campaign ID", "Ad Group ID", "Customer Search Term", "Date",
                       "Impressions", "Clicks", "Purchases", "Spend", "Sales")
            conn.executemany("INSERT INTO raw_report VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             ((report["sha256"], reader.line_num, "", *(row[key] for key in columns)) for row in reader))
        result = [dict(row) for row in conn.execute(SQL.read_text(encoding="utf-8"), params)]
    if case == "simple":
        return next(row for row in previous.oracle(path, base.LAST_WEEK, single=True) if row["sku"] == base.TOTE)
    return result


class Replay(RecordingReplay):
    def reply(self, payload):
        messages = payload["messages"]
        if phase(payload) == "worker":
            if any(row["role"] == "tool" for row in messages):
                return json.dumps({"status": "succeeded", "output": "Scoped receipt returned."}), []
            inputs = json.loads(messages[1]["content"])
            task = inputs["task"]
            scoped, = task["read_scope"]
            sku = scoped["arguments"]["sku"]
            if task["task_id"] == "original-title":
                original, = inputs["inputs"]["product-source"]
                sku = original["result"]["target"]["sku"]
            return None, [tool(scoped["tool"], {"sku": sku})]
        state = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        ready = [(row["tool"], row["sku"]) for row in state["ready_reads"]]
        case, group = self.active["case"], self.active["group"]
        if ready:
            if group == "B" and case in {"complete", "missing-fact"} and ready == [(SOURCES, base.TOTE)]:
                tasks = [job("product-source", ready), job("original-title", [(CURRENT, base.TOTE)], ["product-source"])]
                return None, [tool("delegate_tasks", {"reason": "Gate the title read on verified product sources", "tasks": tasks})]
            if group == "B" and case == "confirmed-zero" and all(name == SALES for name, _ in ready):
                tasks = [job(f"sales-{i}", [row]) for i, row in enumerate(ready)]
                return None, [tool("delegate_tasks", {"reason": "Independent sales sources", "tasks": tasks})]
            explicit = dict(zip(base.KEYS, base.WINDOWS))
            calls = [tool(name, {"sku": sku, **(explicit if name == SALES and group == "A" else {})})
                     for name, sku in ready]
            if case == "complete" and group == "A" and all(name == SALES for name, _ in ready):
                calls.insert(0, tool(SALES, {"sku": base.TOTE, "current_start": base.WINDOWS[0]}))
            return None, calls
        chosen, items = [], []
        for row in self.active["expected"]:
            if not row["selected"]:
                continue
            sku = row["sku"]
            chosen.append(sku)
            if not row["term"]:
                continue
            receipts = next(item["receipts"] for item in state["products"] if item["sku"] == sku)
            action = "review" if case == "unsafe-model" else row["action"]
            items.append({"sku": sku, "item_id": 1, "action": action,
                          "evidence_ids": [receipt["artifact_id"] for receipt in receipts.values()],
                          **({"title": fixture.CONTRACT["valid_edit"]} if action == "title" else {})})
        return json.dumps({"selected_skus": chosen, "items": items, "gaps": state["known_gaps"]}, ensure_ascii=False), []


def observe_reads(original, obs, active):
    def observed(conn, user_id, conversation_id, name, target, windows, report=None):
        link = obs.link.get()
        row = {**link, "source": name, "arguments": target, "windows": windows,
               "start_ns": perf_counter_ns(), "sqlite_selects": 0}

        def query(statement):
            row["sqlite_selects"] += statement.lstrip().split(maxsplit=1)[0].upper() in {"SELECT", "WITH"}

        obs.reads.append(row)
        conn.set_trace_callback(query)
        try:
            if active.get("armed") and name == SOURCES and link:
                active["barrier"]["blocked_ns"] = perf_counter_ns()
                active["entered"].set()
                if not active["release"].wait(10):
                    raise TimeoutError("acceptance source barrier")
            value = original(conn, user_id, conversation_id, name, target, windows, report)
            row["result"] = value
            return value
        finally:
            conn.set_trace_callback(None)
            row["end_ns"] = perf_counter_ns()
    return observed


def writes_audit(original, active):
    def connect(path, *args, **kwargs):
        conn = original(path, *args, **kwargs)
        if active.get("auditing") and str(path) == str(active["db"]):
            def authorizer(action, table, column, *_):
                if table in PROTECTED and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                    active["writes"].append({"action": action, "table": table, "column": column})
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorizer)
        return conn
    return connect


def handoff(obs, requests, evidence):
    created = {row["runtime_call_id"]: row for row in obs.trace if (row["stage"], row["action"]) == ("worker", "created")}
    board = {row["artifact_id"]: row for row in obs.board}
    readers = {row["runtime_call_id"]: row for row in obs.reads if row.get("runtime_call_id")}
    model_trace = {row["runtime_call_id"]: row for row in obs.trace if row["stage"] == "model_call"}
    receipt_trace = {row["evidence"]["artifact_id"]: row for row in obs.trace
                     if row["stage"] == "receipt" and row["action"] == "accepted"}
    tasks = {row["task_id"]: row for plan in obs.plans for row in plan["tasks"]}
    output, seen = [], set()
    for request in requests:
        worker = request.get("worker_id")
        if not worker or worker in seen:
            continue
        seen.add(worker)
        context = json.loads(request["body"]["messages"][1]["content"])
        task = context["task"]
        check = created.get(worker, {}).get("detail") == task["task_id"] and tasks.get(task["task_id"]) == task
        model = model_trace.get(request["model_call_id"], {})
        check &= model.get("parent_call_id") == worker and model.get("detail") == task["task_id"]
        check &= context["scope"]["windows"] == list(base.WINDOWS) and context["scope"]["run_id"] == evidence.get("run_id")
        inputs = [(None, row) for row in context.get("direct_inputs", [])]
        inputs.extend((ref, row) for ref, rows in context["inputs"].items() for row in rows)
        matched = []
        for ref, row in inputs:
            artifact = row["artifact_id"]
            recorded = board.get(artifact, {})
            source_read = readers.get(row["tool_call_id"], {})
            receipt = receipt_trace.get(artifact, {})
            referenced = (artifact in task["direct_refs"] if ref is None else
                          ref == row.get("task_id") and (ref in task["input_refs"] or
                          {"task_id": ref, "artifact_id": artifact} in task["artifact_refs"]))
            exact = (referenced and all(recorded.get(k) == v for k, v in row.items())
                     and source_read.get("result") == row["result"] and source_read.get("arguments") == row["arguments"]
                     and row["arguments"]["sku"] == task["read_scope"][0]["arguments"]["sku"]
                     and source_read.get("end_ns", float("inf")) <= recorded.get("ns", 0) <= request["start_ns"]
                     and (ref is None or receipt.get("runtime_call_id") == row["tool_call_id"]
                          and receipt.get("parent_call_id") == row.get("worker_id")))
            matched.append({"input": row, "producer": recorded, "trace_receipt": receipt, "actual_read": source_read,
                            "worker_to_worker": ref is not None, "passed": bool(exact)})
        output.append({"worker_id": worker, "runtime_call_id": request["model_call_id"], "first_input": context,
                       "created_trace": created.get(worker), "inputs": matched,
                       "passed": bool(check and all(row["passed"] for row in matched))})
    return output


def score(case, expected, evidence, obs, requests):
    if case == "simple":
        metrics = evidence.get("metrics", {}).get("current", {})
        direct = [row for row in obs.trace if row["stage"] == "business_read" and row.get("role") == "orchestrator"]
        fields = {"selection": evidence.get("target", {}).get("sku") == base.TOTE, "action": not requests,
                  "evidence": metrics.get("units") == expected["current_units"], "gaps": not evidence.get("gaps")}
        return fields, fields, {"main_direct": len(direct), "no_worker": not requests}
    picked = [row["sku"] for row in expected if row["selected"]]
    products = {row["sku"]: row for row in evidence.get("products", [])}
    expected_items = {row["sku"]: row for row in expected if row["action"]}
    items = {sku: row for sku, product in products.items() for row in product.get("items", [])}
    expected_gaps = {}
    sales_ok, report_ok, action_ok = True, set(items) == set(expected_items), set(items) == set(expected_items)
    for row in expected:
        sku = row["sku"]
        receipts = products.get(sku, {}).get("receipts", {})
        sales = receipts.get(SALES, {}).get("result") or {}
        for field in ("baseline", "current"):
            sales_ok &= sales.get(field, {}).get("days") == json.loads(row[field + "_days"])
            sales_ok &= sales.get(field, {}).get("units") == row[field]
        days = json.loads(row["baseline_missing"]) + json.loads(row["current_missing"])
        if days:
            expected_gaps[(sku, "missing_dates")] = days
        if row["selected"] and not row["term"]:
            expected_gaps[(sku, "report_missing")] = ["报表"]
        if not row["action"]:
            continue
        item = items.get(sku, {})
        source = receipts.get(REPORT, {}).get("result", {})
        pending = item.get("pending")
        report_ok &= source.get("report", {}).get("sha256") == row["sha256"]
        report_ok &= item.get("attribution") == row["attribution"] and item.get("term") == row["term"]
        report_ok &= [loc["line"] for loc in item.get("row_locations", [])] == json.loads(row["lines"])
        report_ok &= all(item.get("metrics", {}).get(key) == row[key]
                         for key in ("impressions", "clicks", "orders", "spend_cents", "sales_cents"))
        expected_action = "observe" if case == "unsafe-model" else row["action"]
        action_ok &= item.get("action") == expected_action and bool(pending) == (expected_action in {"title", "review"})
        if pending:
            report_ok &= pending["facts_version"] == row["facts_version"] and any(
                quote.get("statement") == row["capacity"] for quote in pending["citations"])
            report_ok &= (pending["scope"]["campaign_id"], pending["scope"]["ad_group_id"]) == (row["campaign"], row["ad_group"])
            report_ok &= pending["sku"] == sku and pending["windows"] == list(base.WINDOWS)
            report_ok &= pending["artifact_ids"] == [receipt["artifact_id"] for receipt in receipts.values()]
            if expected_action == "title":
                report_ok &= pending["title_version"] == row["title_version"] and "12 L" in pending["title"]
        if row["attribution"] == "ad_group":
            expected_gaps[(sku, "unattributed")] = [f"第 {line} 行" for line in json.loads(row["lines"])]
            report_ok &= not item.get("ownership") and not item.get("citations")
        elif row["clicks"] == 15:
            expected_gaps[(sku, "sample")] = ["15", "20"]
        elif row["action"] == "missing":
            expected_gaps[(sku, "facts_missing")] = ["主隔层容量"]

    def gaps_correct(gaps):
        material = {(row.get("sku"), row.get("code")): " ".join(row.get("details", [])) for row in gaps}
        return set(material) == set(expected_gaps) and all(all(value in material[key] for value in details)
                                                            for key, details in expected_gaps.items())

    raw = next((row.get("evidence", {}).get("raw") for row in obs.trace if row["stage"] == "inspection_model"), None)
    try:
        parsed = json.loads(raw or "{}")
        raw_items = {(row["sku"], row["item_id"]): row for row in parsed.get("items", [])}
    except (ValueError, TypeError, KeyError, AttributeError):
        parsed, raw_items = {}, {}
    expected_actions = {(sku, 1): row["action"] for sku, row in expected_items.items()}
    model_fields = {
        "selection": sorted(parsed.get("selected_skus", [])) == sorted(picked) and "selected_skus" in parsed,
        "action": "items" in parsed and {key: row.get("action") for key, row in raw_items.items()} == expected_actions,
        "evidence": bool(raw) and sales_ok and report_ok and all(
            sorted(raw_items.get((sku, 1), {}).get("evidence_ids", [])) == sorted(item.get("evidence_ids", []))
            for sku, item in items.items()),
        "gaps": "gaps" in parsed and gaps_correct(parsed["gaps"]),
    }
    final_fields = {"selection": evidence.get("selected_skus") == picked, "action": action_ok,
                    "evidence": sales_ok and report_ok, "gaps": gaps_correct(evidence.get("gaps", []))}
    return model_fields, final_fields, {"guard_blocked": any(row.get("error_code") == "model_action_mismatch"
        and row["stage"] == "inspection_decision" for row in obs.trace)}


def send(client, group, question, timing):
    timing.clear()
    start = perf_counter_ns()
    with client.stream("POST", "/api/chat/stream", json={"message": question, "conversation_id": group}) as response:
        response.raise_for_status()
        events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
    terminal = next((row for row in reversed(events) if row["type"] in {"done", "error", "stopped", "superseded"}), {})
    return {"events": events, "terminal": terminal,
            "terminal_ms": round((timing.get("terminal", perf_counter_ns()) - start) / 1e6, 3)}


def metrics(obs, requests, handoffs, sse, fixed):
    starts = {row["runtime_call_id"]: row["ns"] for row in obs.trace if (row["stage"], row["action"]) == ("worker", "created")}
    ends = {row["runtime_call_id"]: row["ns"] for row in obs.trace if (row["stage"], row["action"]) == ("worker", "reclaimed")}
    intervals = {worker: [start, ends[worker]] for worker, start in starts.items() if worker in ends}
    overlap = [{"workers": [a, b], "overlap_ns": min(x[1], y[1]) - max(x[0], y[0])}
               for (a, x), (b, y) in combinations(intervals.items(), 2) if min(x[1], y[1]) > max(x[0], y[0])]
    usage = [row.get("response", {}).get("body", {}).get("usage") for row in requests] if not fixed else []
    first_reads = [read for read in obs.reads if read.get("runtime_call_id")]
    direct = sum(row["stage"] == "business_read" and row.get("role") == "orchestrator" for row in obs.trace)
    simple_reads = sum(row["stage"] == "business_read" for row in obs.trace) if not obs.reads else 0
    repeats = [read for read in obs.reads if not read.get("runtime_call_id")]
    seen_for_main = set()
    worker_artifacts = {row["artifact_id"] for row in obs.board if row.get("worker_id") and row["source"] != "worker_model"}
    for request in requests:
        if request.get("worker_id") or request.get("step") != "operations_inspection":
            continue
        context = json.loads(request["body"]["messages"][1]["content"].split("\n\n", 1)[1])
        seen_for_main.update(receipt["artifact_id"] for product in context["products"] for receipt in product["receipts"].values()
                             if receipt["artifact_id"] in worker_artifacts)
    stages = {step["action"]: sum(row["stage"] == "inspection_read" and row["action"] == step["action"]
                                 for row in obs.trace) for step in obs.trace if step["stage"] == "inspection_read"}
    return {"sse_ms": sse["terminal_ms"], "sdk_attempts": len(requests) if not fixed else 0,
            "sdk_retries": sum(row.get("sdk_retry", 0) > 0 for row in requests), "usage_attempts": sum(bool(row) for row in usage),
            "input_tokens": sum(row.get("prompt_tokens", 0) for row in usage if row),
            "output_tokens": sum(row.get("completion_tokens", 0) for row in usage if row),
            "sdk_failures": sum(row.get("response", {}).get("status") != 200 for row in requests) if not fixed else 0,
            "main_direct": direct,
            "business_tool_calls": sum(row["stage"] == "tool_call" for row in obs.trace),
            "bottom_first_reads": len(first_reads) + simple_reads, "bottom_repeated_reads": len(repeats),
            "evidence_sql_selects_first": sum(row["sqlite_selects"] for row in first_reads),
            "evidence_sql_selects_repeated": sum(row["sqlite_selects"] for row in repeats),
            "approval_reads": 0, **stages,
            "dispatches": len(obs.plans), "workers_started": len(starts), "workers_reclaimed": len(ends),
            "worker_intervals": intervals, "worker_overlap": overlap,
            "handoffs_to_main": sorted(seen_for_main),
            "worker_to_worker": sum(item["worker_to_worker"] and item["passed"] for row in handoffs for item in row["inputs"])}


def summary(output, results, fixed, manifest):
    groups = {}
    for group in ("A", "B"):
        rows = [row for row in results if row["group"] == group]
        if not rows:
            continue
        times = sorted(row["metrics"]["sse_ms"] for row in rows)
        counters = ("sdk_attempts", "sdk_retries", "sdk_failures", "usage_attempts", "input_tokens", "output_tokens",
                    "main_direct", "business_tool_calls", "bottom_first_reads", "bottom_repeated_reads", "approval_reads",
                    "evidence_sql_selects_first", "evidence_sql_selects_repeated", "persistence_recheck", "handoff_recheck",
                    "dispatches", "workers_started", "worker_to_worker")
        groups[group] = {"samples": len(rows), "failures": sum(not row["passed"] for row in rows),
                         "model_correct_rounds": sum(all(row["model"].values()) for row in rows),
                         "final_correct_rounds": sum(all(row["final"].values()) for row in rows),
                         "model_points": sum(sum(row["model"].values()) for row in rows),
                         "final_points": sum(sum(row["final"].values()) for row in rows), "possible_points": 4 * len(rows),
                         "median_ms": median(times), "p95_ms": times[ceil(.95 * len(times)) - 1],
                         "main_worker_receipts": sum(len(row["metrics"]["handoffs_to_main"]) for row in rows),
                         "overlap_pairs": sum(len(row["metrics"]["worker_overlap"]) for row in rows),
                         **{key: sum(row["metrics"].get(key, 0) for row in rows) for key in counters}}
    pair_rows = {}
    for row in results:
        pair_rows.setdefault((row["pair"], row["case"]), {})[row["group"]] = row
    paired = [{"pair": pair, "case": case, "order": rows["A"]["order"],
               "b_minus_a": {key: round(rows["B"]["metrics"].get(key, 0) - rows["A"]["metrics"].get(key, 0), 3)
                             for key in ("sse_ms", "sdk_attempts", "input_tokens", "output_tokens", "business_tool_calls",
                                         "bottom_first_reads", "bottom_repeated_reads")}}
              for (pair, case), rows in pair_rows.items() if rows.keys() == {"A", "B"}]
    report = {"fixed": fixed, "frozen_sha256": previous.digest(manifest), "passed": all(row["passed"] for row in results),
              "groups": groups, "pairs": paired, "results": results}
    if paired:
        report["median_paired_ms"] = median(row["b_minus_a"]["sse_ms"] for row in paired)
    write(output / "report.json", report)
    return report


def run(output, fixed_results, browser_only=False):
    fixed = fixed_results is None
    configure(output / "startup.sqlite3", "mock" if fixed else "glm")
    os.environ["LISTING_SIGNIFICANT_DECLINE_PCT"] = str(fixture.THRESHOLD)
    if not fixed:
        credentials = dotenv_values(".env")
        os.environ.setdefault("GLM_API_KEY", credentials.get("GLM_API_KEY") or "")
        os.environ.setdefault("GLM_MODEL", credentials.get("GLM_MODEL") or MODEL)
    from backend import observability
    from backend.agent import listing_run, llm_provider, operations_inspection as inspection, team_contracts, team_runtime, team_tools
    from backend.database import chat_groups, database

    manifest = freeze(output)
    if not fixed:
        prior = read(fixed_results / "report.json")
        if (not prior["fixed"] or not prior["passed"] or len(prior["results"]) !=
                2 * (len(fixture.CASES) + len(fixture.CONTROLS)) or prior["frozen_sha256"] != previous.digest(manifest)):
            raise ValueError("All matching fixed cases must pass before live GLM")
        if not os.environ["GLM_API_KEY"]:
            raise RuntimeError("GLM_API_KEY is required")
    database.init_db()
    password = token_urlsafe(24)
    redactor = Redactor([password, os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY")])
    obs, active, timing = Observation(), {}, {}
    provider = Replay(obs, active) if fixed else llm_provider.ProviderFactory.create()
    accept, listing, commit = chat_groups.accept, listing_run.run_listing, inspection.commit_result

    def frozen_accept(*args, **kwargs):
        group, context = accept(*args, **kwargs)
        return replace(group, requested_at=FROZEN_CLOCK), context

    def policy(*args):
        if browser_only:
            active["expected"] = oracle(active["db"], args[2].conversation_id, "complete")
            active["before"] = snapshot(active["db"])
            active["auditing"] = True
        return listing(*args, allow_delegation=active["group"] == "B")

    def persistence(conn, group, content, data):
        if active["case"] == "stale-evidence":
            # Simulate a source change in the save transaction and restore the injected change before commit.
            conn.execute("SAVEPOINT evidence_fault")
            conn.execute("DELETE FROM search_term_amazon_row_links WHERE line=2")
            try:
                return commit(conn, group, content, data)
            finally:
                conn.execute("ROLLBACK TO evidence_fault")
                conn.execute("RELEASE evidence_fault")
        return commit(conn, group, content, data)

    class LiveAudit(GLMAudit):
        run_cap = 4 * len(fixture.CASES) * 12

        def case_cap(self, case):
            return 12

        def request(self, request):
            if (not self.enabled or self.case != active.get("id") or self.model_link.get().get("step") not in
                    {"operations_inspection", "team_worker"} or database.sandbox_db_path() != active["db"]
                    or snapshot(active["db"]) != active["before"]):
                raise RuntimeError("SDK request outside the frozen synthetic inspection")
            return super().request(request)

        def response(self, response):
            super().response(response)
            self.stop = None

    audit = None
    results = []
    with ExitStack() as stack:
        for module in (observability, listing_run, llm_provider, inspection, team_runtime, team_tools, chat_groups):
            stack.enter_context(patch.object(module, "add_trace_step", obs.trace_step(module.add_trace_step)))
        for attr, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                               ("FUNC_LOG_FILE", "functions.jsonl")):
            stack.enter_context(patch.object(observability, attr, output / filename))
        stack.enter_context(patch.object(sqlite3, "connect", writes_audit(sqlite3.connect, active)))
        stack.enter_context(patch.object(team_tools, "trace_tool_call", obs.trace_tool(team_tools.trace_tool_call)))
        stack.enter_context(patch.object(inspection, "_read", observe_reads(inspection._read, obs, active)))
        stack.enter_context(patch.object(inspection, "commit_result", persistence))
        stack.enter_context(patch.object(chat_groups, "accept", frozen_accept))
        stack.enter_context(patch.object(listing_run, "run_listing", policy))
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=provider))
        stack.enter_context(patch("backend.memory.conversation_memory.store_conversation"))
        board = team_contracts.ReceiptBoard
        stack.enter_context(patch.object(board, "append", obs.board_append(board.append)))
        stack.enter_context(patch.object(board, "accept", obs.board_accept(board.accept)))
        stack.enter_context(patch.object(board, "accept_direct", obs.board_accept(board.accept_direct, direct=True)))
        if not fixed:
            if provider._model != MODEL or str(provider._client.base_url).rstrip("/") != ENDPOINT:
                raise RuntimeError("Expected the frozen GLM model and official endpoint")
            stack.callback(provider._client.close)
            audit = LiveAudit(output / "sdk.sqlite3", redactor, uuid4().hex, LiveAudit.run_cap)
            hooks = provider._client._client.event_hooks
            hooks["request"].append(audit.request)
            hooks["response"].append(audit.response)
            for module in (llm_provider, team_runtime):
                stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        from fast_server import app

        if browser_only:
            from backend.captcha import captcha
            from tests.home_diagnosis_server import demo_captcha

            folder = output / "browser"
            folder.mkdir()
            directory = output / "fixtures" / "complete"
            db = folder / "sandbox.sqlite3"
            prepare(db, directory, database, password)
            active.update(case="complete", group="A", db=db, writes=[])
            with patch.object(captcha, "generate_captcha", demo_captcha), serve(app) as origin:
                result = subprocess.run(["node", "tests/operations_inspection_browser.cjs", origin, str(folder), str(directory)],
                                        env={**browser_env(), "INSPECTION_BROWSER_PASSWORD": password},
                                        timeout=100, capture_output=True)
            after = snapshot(db)
            passed = result.returncode == 0 and after == active.get("before") and not active["writes"]
            write(folder / "browser-process.json", {"exit_code": result.returncode, "passed": passed,
                  "protected_sql_equal": after == active.get("before"), "writes": active["writes"],
                  "stderr": redactor.clean(result.stderr.decode(errors="replace"))})
            print("browser:", result.returncode, flush=True)
            return passed

        async def timed_app(scope, receive, send_event):
            async def timed_event(event):
                if event["type"] == "http.response.body":
                    body = event.get("body", b"")
                    if body.startswith(b"data: "):
                        data = json.loads(body[6:])
                        if data["type"] in {"done", "error", "stopped", "superseded"}:
                            timing["terminal"] = perf_counter_ns()
                await send_event(event)
            await app(scope, receive, timed_event)

        orders = ("AB",) if fixed else ("AB", "BA")
        for pair, order in enumerate(orders, 1):
            for case in (*fixture.CASES, *fixture.CONTROLS) if fixed else fixture.CASES:
                for group in order:
                    identifier = f"{pair:02}-{case}-{group}"
                    folder = output / identifier
                    folder.mkdir()
                    db = folder / "sandbox.sqlite3"
                    directory = output / "fixtures" / case
                    prepare(db, directory, database, password)
                    obs.reset()
                    active.clear()
                    active.update(case=case, group=group, id=identifier, db=db, writes=[], barrier={},
                                  armed=fixed and group == "B" and case in {"complete", "missing-fact"},
                                  entered=Event(), release=Event())
                    with TestClient(timed_app, raise_server_exceptions=False) as client:
                        logged = client.post("/api/login", json={"username": "task45-operator", "password": password})
                        logged.raise_for_status()
                        token = logged.json()["data"]["token"]
                        redactor.secrets.append(token)
                        client.headers["Authorization"] = "Bearer " + token
                        created = client.post("/api/chat/conversations", json={"title": identifier})
                        created.raise_for_status()
                        cid = created.json()["data"]["conversation_id"]
                        endpoint = f"/api/chat/conversations/{cid}"
                        imported = client.post(endpoint + "/search-terms/reports",
                                               params={"filename": "amazon-daily.csv", "shop_id": base.SHOP},
                                               content=(directory / "report.csv").read_bytes())
                        imported.raise_for_status()
                        if case == "simple":
                            send(client, cid, "选 SKU " + base.TOTE, timing)
                            obs.reset()
                        active["expected"] = oracle(db, cid, case)
                        active["before"] = snapshot(db)
                        write(folder / "sql-expected.json", active["expected"])
                        write(folder / "sql-before.json", active["before"])
                        if audit:
                            audit.case, audit.enabled = identifier, True

                        def examine():
                            reached = active["entered"].wait(8)
                            active["barrier"].update(reached=reached,
                                before=[row for row in obs.trace if row.get("detail") == "original-title"],
                                sdk_before=[row.get("worker_id") for row in obs.requests],
                                reads_before=[row["source"] for row in obs.reads])
                            active["barrier"]["released_ns"] = perf_counter_ns()
                            active["release"].set()

                        monitor = Thread(target=examine, daemon=True) if active["armed"] else None
                        if monitor:
                            monitor.start()
                        active["auditing"] = True
                        try:
                            sse = send(client, cid, base.SIMPLE if case == "simple" else fixture.QUESTION, timing)
                        finally:
                            active["auditing"] = False
                            if audit:
                                audit.enabled = False
                            if monitor:
                                monitor.join(11)
                        terminal = sse["terminal"]
                        response = client.get(endpoint + f"/runs/{terminal.get('run_id')}/evidence")
                        evidence = response.json().get("data", {}) if response.status_code == 200 else {}
                        history = client.get(endpoint + "/history").json()["data"]
                    after = snapshot(db)
                    requests = list(obs.requests) if fixed else [row for row in audit.rows() if row["case"] == identifier]
                    inputs = handoff(obs, requests, evidence)
                    measured = metrics(obs, requests, inputs, sse, fixed)
                    model_score, final_score, extra = score(case, active["expected"], evidence, obs, requests)
                    writes = active["writes"]
                    intentional = case == "stale-evidence"
                    checks = {"sse_done": terminal.get("type") == "done",
                              "protected_sql_equal": active["before"] == after,
                              "no_sql_write_statements": not writes if not intentional else all(
                                  row["table"] == "search_term_amazon_row_links" and row["action"] == sqlite3.SQLITE_DELETE
                                  for row in writes),
                              "worker_join": measured["workers_started"] == measured["workers_reclaimed"] == len(inputs)
                              and all(row["passed"] for row in inputs),
                              "direct_only_a": group != "A" or not (measured["dispatches"] or inputs),
                              "first_reads_scoped": all(read["arguments"]["sku"] in base.PRODUCTS for read in obs.reads)}
                    if active["armed"]:
                        barrier = active["barrier"]
                        title_input = next((row for row in inputs if row["first_input"]["task"]["task_id"] == "original-title"), None)
                        checks["barrier"] = bool(barrier.get("reached") and not any(row["stage"] in {"worker", "model_call"}
                            for row in barrier["before"]) and CURRENT not in barrier["reads_before"])
                        if case == "complete":
                            checks["dependent_first_input"] = bool(title_input and title_input["passed"] and
                                {row["input"]["source"] for row in title_input["inputs"]} == {SALES, REPORT, SOURCES}
                                and any(row["worker_to_worker"] and row["input"]["result"]["version"] == 7
                                        for row in title_input["inputs"]) and
                                title_input["created_trace"]["ns"] > barrier["released_ns"])
                        else:
                            checks["dependent_blocked"] = (title_input is None and all(row["source"] != CURRENT for row in obs.reads)
                                and any(row.get("error_code") == "inspection_title_evidence_missing" for row in obs.trace))
                    if case == "confirmed-zero" and group == "B":
                        checks["parallel_trace"] = bool(measured["worker_overlap"])
                    if fixed and case == "complete" and group == "A":
                        checks["explicit_sales_scope"] = (sum(row["source"] == SALES and row.get("runtime_call_id") is not None
                            for row in obs.reads) == 3 and sum(row.get("error_code") == "inspection_read_out_of_scope"
                                                              for row in obs.trace) == 1)
                    if case == "unsafe-model":
                        checks["wrong_model_blocked"] = not model_score["action"] and extra["guard_blocked"]
                    elif intentional:
                        final_score = {key: not evidence for key in fixture.SCORE_FIELDS}
                        checks["stale_rejected"] = (not evidence and any(row.get("error_code") == "inspection_evidence_changed"
                                                     for row in obs.trace))
                    if case == "simple":
                        checks["single_direct_read"] = extra["main_direct"] == 1 and extra["no_worker"]
                    result = {"id": identifier, "pair": pair, "order": order, "case": case, "group": group,
                              "trace_id": terminal.get("trace_id"), "checks": checks, "model": model_score, "final": final_score,
                              "passed": all(checks.values()) and all(final_score.values()), "metrics": measured, **extra}
                    artifacts = {"sse": sse, "evidence": evidence, "history": history, "trace": obs.trace, "board": obs.board,
                                 "bottom-reads": obs.reads, "task-sheets": obs.plans, "worker-first-input": inputs,
                                 "sql-after": after, "write-audit": writes, "barrier": active["barrier"],
                                 "model-replay" if fixed else "sdk": requests, "result": result}
                    for name, value in artifacts.items():
                        write(folder / (name + ".json"), redactor.clean(value))
                    results.append(result)
                    summary(output, results, fixed, manifest)
                    print(identifier, result["passed"], "model", sum(model_score.values()), "final", sum(final_score.values()),
                          "failed checks", [key for key, value in checks.items() if not value], flush=True)
    return summary(output, results, fixed, manifest)["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixed-results", type=Path)
    parser.add_argument("--browser-only", action="store_true")
    args = parser.parse_args()
    if args.browser_only and args.fixed_results:
        parser.error("--browser-only uses the fixed replay")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    return run(output, args.fixed_results, args.browser_only)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
