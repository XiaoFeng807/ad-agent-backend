"""Replay and measure read-only three-SKU Listing inspections over authenticated SSE."""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime
from functools import wraps
from itertools import combinations
from pathlib import Path
from secrets import token_urlsafe
from threading import Event
from time import perf_counter_ns
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from scripts import accept_task45_t1 as sql
from scripts import task45_t1_fixture as base
from scripts import task45_t3_fixture as fixture
from scripts.accept_task42_t1 import configure, read, serve, write
from scripts.run_real_diagnosis import Redactor
from scripts.run_task27_t5 import ENDPOINT, GLMAudit, MODEL, browser_env
from scripts.task45_t2_observe import Observation, calls, phase, public_receipt, verify_handoffs


SALES, CURRENT, FACTS = sql.READS
DELEGATE = "delegate_tasks"
READERS = {SALES: "read_sales", CURRENT: "read_listing", FACTS: "read_facts"}
MODEL_STEPS = {"listing_inspection_collect", "listing_inspection_review", "team_worker"}
PROTECTED = {"listing_test_shops", "listing_test_products", "listing_test_facts", "listing_test_sales",
             "listing_test_required_facts", "listing_change_proposals", "listing_change_receipts",
             "chat_group_date_proposals", "chat_group_pause_proposals", "chat_group_pause_receipts",
             "diagnosis_pause_proposals", "diagnosis_pause_receipts", "diagnosis_review_proposals"}


def tool(name, args):
    return {"id": uuid4().hex, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def job(task_id, reads, dependencies=()):
    return {"task_id": task_id, "objective": "Collect the specified Listing evidence",
            "capabilities": list(dict.fromkeys(name for name, _ in reads)),
            "read_scope": [{"tool": name, "arguments": {"sku": sku}} for name, sku in reads],
            "dependencies": list(dependencies), "input_refs": list(dependencies),
            "expected_output": "Business tool receipts", "stop_condition": "All scoped reads return"}


class Replay(sql.Replay):
    def __init__(self, observation, active):
        super().__init__()
        self.obs = observation
        self.active = active

    def create(self, **payload):
        row = {**self.obs.model.get(), "start_ns": perf_counter_ns(), "body": payload}
        # The exact first input is retained before tool output grows the conversation.
        row["body"] = json.loads(json.dumps(payload))
        self.obs.requests.append(row)
        content, calls = self.reply(payload)
        reply = {"content": content, "tool_calls": calls}
        row.update(end_ns=perf_counter_ns(), response=reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(**reply))])

    def reply(self, payload):
        messages = payload["messages"]
        if phase(payload) == "worker":
            context = json.loads(messages[1]["content"])
            task = context["task"]
            if any(row["role"] == "tool" for row in messages):
                return json.dumps({"status": "succeeded", "output": "Scoped reads finished"}), []
            if task["task_id"] == "tote-page":
                fact, = context["inputs"]["tote-fact"]
                return None, [tool(CURRENT, {"sku": fact["result"]["target"]["sku"]})]
            requests = [tool(row["tool"], row["arguments"]) for row in task["read_scope"]]
            if task["task_id"] == "case" and self.active["case"] == "tie":
                requests.append(tool(CURRENT, {"sku": base.TOTE}))
            return None, requests
        if messages[0]["content"].startswith("Review Listing bullets"):
            expected = fixture.expected_findings(self.active["case"])
            findings = {}
            for sku in json.loads(messages[1]["content"]):
                findings[sku] = [{"bullet": row["bullet"], "status": row["status"],
                                  "fact_ids": [] if row["status"] == "unverifiable" else [row["fact_id"]],
                                  "reason": {"supported": "同一部件和规格的描述一致。",
                                             "conflict": "同一部件和适用条件下的规格数值不同。",
                                             "unverifiable": "缺少主隔层容量的生效事实。"}[row["status"]]}
                                 for row in expected[sku]]
            if self.active["case"] == "base" and self.active["group"] == "A":
                # Replay the first real response's valid findings under its extra wrapper.
                findings = {sku: {"findings": rows} for sku, rows in findings.items()}
            return json.dumps({"products": findings}, ensure_ascii=False), []
        if self.active["group"] == "B":
            invalid = job("wrong-link", [(CURRENT, base.RISER)], ["tote-fact"])
            fact = job("tote-fact", [(FACTS, base.TOTE)])
            return None, [
                tool(DELEGATE, {"reason": "Scope rejection control", "tasks": [fact, invalid]}),
                tool(DELEGATE, {"reason": "Independent products and a same-SKU fact dependency", "tasks": [
                    fact, job("riser", [(name, base.RISER) for name in READERS]),
                    job("case", [(name, base.CASE) for name in READERS]),
                    job("tote-page", [(CURRENT, base.TOTE)], ["tote-fact"]),
                ]}),
                tool(SALES, {"sku": base.TOTE}),
            ]
        return None, [
            tool(CURRENT, {"sku": fixture.FOREIGN}), tool(FACTS, {"sku": base.TOTE, "marketplace_id": "CA"}),
            tool(SALES, {"sku": base.RISER, "current_start": "2026-11-05"}),
            tool("approve_listing", {"sku": base.TOTE}),
            *[tool(name, {"sku": sku}) for sku in base.PRODUCTS for name in READERS],
        ]


class Barrier:
    def __init__(self, observation):
        self.obs = observation
        self.reset(False)

    def reset(self, armed):
        self.armed = armed
        self.blocked = Event()
        self.independent_finished = Event()
        self.record = {}

    def wrap(self, name, reader):
        @wraps(reader)
        def observe(conn, user_id, shop_id, marketplace_id, sku, **windows):
            if self.armed and self.obs.link.get():
                if (name, sku) == (FACTS, base.TOTE):
                    self.record["blocked_ns"] = perf_counter_ns()
                    self.blocked.set()
                    self.record["independent_finished"] = self.independent_finished.wait(5)
                    self.record["workers_before_release"] = [
                        step for step in self.obs.trace if (step["stage"], step["action"]) == ("worker", "created")
                    ]
                    self.record["sdk_before_release"] = [row["worker_id"] for row in self.obs.requests if row.get("worker_id")]
                    self.record["released_ns"] = perf_counter_ns()
                if (name, sku) == (CURRENT, base.RISER):
                    self.record["independent_wait_met"] = self.blocked.wait(5)
                    result = reader(conn, user_id, shop_id, marketplace_id, sku, **windows)
                    self.independent_finished.set()
                    return result
            return reader(conn, user_id, shop_id, marketplace_id, sku, **windows)
        return observe


def freeze(output, database, password, prior):
    manifest = {"statistics_date": base.AS_OF, "windows": base.WINDOWS, "natural_week": base.LAST_WEEK,
                "model": MODEL, "endpoint": ENDPOINT, "schedule": ["AB", "BA"], "tools": 12, "models": 12,
                "deadline_seconds": 90, "fixture_hash": sql.digest(Path(fixture.__file__)),
                "sales_sql_hash": sql.digest(sql.SQL), "questions": {case: fixture.question(case)
                    for case in (*fixture.CASES, *fixture.FIXED_ONLY)}, "cases": {}}
    for case in (*fixture.CASES, *fixture.FIXED_ONLY):
        folder = output / "frozen" / case
        folder.mkdir(parents=True)
        db = folder / "seed.sqlite3"
        sources = fixture.source(case)
        sql.seed(db, database, password, sources)
        with closing(database.get_db()) as conn, conn:
            other = conn.execute("INSERT INTO users(username,password) VALUES (?,?)", ("other", "unused")).lastrowid
            conn.executemany("INSERT INTO listing_test_shops VALUES (?,?,?)",
                             [("extra-shop", base.MARKET, 1), (base.SHOP, "CA", 1), ("private-shop", base.MARKET, other)])
            conn.execute("INSERT INTO listing_test_products VALUES (?,?,?,?,?,?,?,?)",
                         (base.SHOP, base.MARKET, fixture.FOREIGN, 1, "额外商品", json.dumps(sources[0]["bullets"]), 1, 1))
            targets = {"mixed-shop": ("extra-shop", base.MARKET), "mixed-market": (base.SHOP, "CA"),
                       "unauthorized": ("private-shop", base.MARKET)}
            if case in targets:
                conn.execute("INSERT INTO listing_test_products SELECT ?, ?, sku, version, title, bullets, "
                             "price_cents, inventory_units FROM listing_test_products WHERE sku=?",
                             (*targets[case], base.CASE))
                for table in ("listing_test_facts", "listing_test_sales"):
                    conn.execute(f"DELETE FROM {table} WHERE shop_id=? AND marketplace_id=? AND sku=?",
                                 (base.SHOP, base.MARKET, base.CASE))
                conn.execute("DELETE FROM listing_test_products WHERE shop_id=? AND marketplace_id=? AND sku=?",
                             (base.SHOP, base.MARKET, base.CASE))
        before = sql.snapshot(db)
        expected = sql.oracle(db, base.LAST_WEEK if case == "simple" else base.WINDOWS, single=case == "simple")
        findings = fixture.expected_findings(case)
        conflict_skus = {sku for sku, rows in findings.items() if any(item["status"] == "conflict" for item in rows)}
        eligible = [row for row in expected if row["sku"] in conflict_skus and (row.get("change_pct") or 0) < 0]
        decline = min((row["change_pct"] for row in eligible), default=None)
        priority = [row["sku"] for row in eligible if row["change_pct"] == decline]
        expected = {"sales": expected, "findings": findings, "priority": priority}
        for name, value in {"sql-before": before, "source": sources, "expected": expected}.items():
            write(folder / f"{name}.json", value)
        manifest["cases"][case] = {name: sql.digest(folder / f"{name}.json")
                                   for name in ("sql-before", "source", "expected")}
    path = output / "frozen.json"
    write(path, manifest)
    if prior and read(prior / "frozen.json") != read(path):
        raise RuntimeError("Live and fixed frozen inputs differ")
    return sql.digest(path)


def send(client, headers, message, conversation):
    started = perf_counter_ns()
    with client.stream("POST", "/api/chat/stream", headers=headers,
                       json={"message": message, "conversation_id": conversation}) as response:
        response.raise_for_status()
        events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
    terminal = next((event for event in reversed(events)
                     if event["type"] in {"done", "error", "stopped", "superseded"}), {})
    return {"events": events, "terminal": terminal, "sse_ms": round((perf_counter_ns() - started) / 1e6, 3)}


def business(case, answer, evidence, expected, sources, initial):
    if case in fixture.FIXED_ONLY[1:]:
        return {"rejected_scope": not evidence and not initial and ("同一授权店铺" in answer)}
    if case == "simple":
        sales = evidence.get("metrics", {})
        data = sales.get("current", {})
        target = sources[0]["target"]
        expected_sales = next(row for row in expected["sales"] if row["sku"] == target["sku"])
        return {"selected_product": sales.get("target") == target and sales.get("version") == expected_sales["version"],
                "natural_week": (data.get("start_date"), data.get("end_date")) == base.LAST_WEEK
                and data.get("units") == expected_sales["current_units"] and str(data.get("units")) in answer,
                "one_read": len(initial) == 1 and initial[0]["source"] == SALES}
    rows = {row["sku"]: row for row in evidence.get("products", [])}
    checks = {"three_products": len(evidence.get("products", [])) == 3 and rows.keys() == base.PRODUCTS.keys(),
              "windows": evidence.get("windows") == list(base.WINDOWS) and evidence.get("statistics_date") == base.AS_OF,
              "priority": set(evidence.get("priority", {}).get("skus", [])) == set(expected["priority"])
              and evidence.get("priority", {}).get("status") == ("tie" if case == "tie" else "selected")
              and all(sku in answer.split("优先")[-1] for sku in expected["priority"])}
    for source in sources:
        sku = source["target"]["sku"]
        row = rows.get(sku, {})
        receipts = row.get("receipts", {})
        listing = receipts.get(CURRENT, {}).get("result", {})
        facts = receipts.get(FACTS, {}).get("result", {})
        sales = receipts.get(SALES, {}).get("result", {})
        want_sales = next(item for item in expected["sales"] if item["sku"] == sku)
        findings = row.get("findings", [])
        expected_fields = expected["findings"][sku]
        checks.update({
            f"{sku}/sales": sales.get("target") == source["target"]
            and sql.check_metrics(sales, want_sales, base.WINDOWS, source)
            and f"{want_sales['baseline_units']} → {want_sales['current_units']} 件" in answer,
            f"{sku}/listing": listing.get("target") == source["target"] and listing.get("bullets") == source["bullets"]
            and listing.get("version") == source["listing_version"] and all(quote in answer for quote in source["bullets"]),
            f"{sku}/facts": facts.get("target") == source["target"] and facts.get("facts") == source["facts"]
            and facts.get("version") == source["facts_version"],
            f"{sku}/findings": len(findings) == len(expected_fields) and all(
                actual["bullet"] == want["bullet"] and actual["status"] == want["status"]
                and actual["page"] == source["bullets"][want["bullet"] - 1]
                and (actual["facts"] == {} if want["status"] == "unverifiable" else
                     actual["facts"] == {want["fact_id"]: want["statement"]} and want["statement"] in answer)
                for actual, want in zip(findings, expected_fields)),
        })
        if sku == base.TOTE and case == "missing-fact":
            checks["missing_capacity"] = row.get("missing_fields") == {"capacity": "主隔层容量"} and "主隔层容量" in answer
    return checks


def audit_writes(connect, active, writes):
    @wraps(connect)
    def open_db(path, *args, **kwargs):
        conn = connect(path, *args, **kwargs)
        database = active.get("db")
        if database and str(path) in {str(database), database.as_uri() + "?mode=ro"}:
            def authorizer(action, table, column, schema, trigger):
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and table in PROTECTED:
                    writes.append({"action": action, "table": table, "column": column, "trigger": trigger})
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorizer)
        return conn
    return open_db


def measure(obs, sdk, initial, fixed, handoffs, sse_ms):
    def count(stage, action):
        return sum((row["stage"], row["action"]) == (stage, action) for row in obs.trace)

    overlap = [{"first": left["runtime_call_id"], "second": right["runtime_call_id"], "overlap_ms": round(
        (min(left["end_ns"], right["end_ns"]) - max(left["start_ns"], right["start_ns"])) / 1e6, 6)}
        for left, right in combinations(initial, 2)
        if left.get("parent_call_id") != right.get("parent_call_id")
        and min(left["end_ns"], right["end_ns"]) > max(left["start_ns"], right["start_ns"])]
    first_reads = [row["start_ns"] for row in initial]
    last_reads = [row["end_ns"] for row in initial]
    usages = [row["response"]["body"]["usage"] for row in sdk if row.get("response", {}).get("body", {}).get("usage")]
    return {
        "attempted_dispatches": count("listing_inspection_dispatch", "attempt"),
        "approved_dispatches": count("batch", "started"),
        "approved_tasks": sum(len(plan["tasks"]) for plan in obs.plans),
        "workers_started": count("worker", "created"),
        "workers_reclaimed": count("worker", "reclaimed"),
        "worker_inputs_reconciled": sum(row["passed"] for row in handoffs),
        "handoffs_reconciled": sum(len(row["receipts"]) for row in handoffs if row["passed"]),
        "worker_artifacts_used": sum(row.get("applied_artifacts", 0) for row in handoffs),
        "read_overlaps": overlap,
        "read_window_ms": round((max(last_reads) - min(first_reads)) / 1e6, 3) if initial else None,
        "initial_reads": len(initial), "bottom_reads": len(obs.reads),
        "rechecks": len(obs.reads) - len(initial),
        "same_key_repeat_reads": len(obs.reads) - len({(row["source"], json.dumps(row["arguments"], sort_keys=True))
                                                       for row in obs.reads}),
        "read_modes": sorted({row["role"] for row in initial}), "main_batch_execution": "serial",
        "sdk_requests": 0 if fixed else len(sdk), "replay_requests": len(sdk) if fixed else 0,
        "worker_requests": sum(phase(row["body"]) == "worker" for row in sdk),
        "responses_with_usage": len(usages),
        "input_tokens": sum(usage["prompt_tokens"] for usage in usages) if usages else None,
        "output_tokens": sum(usage["completion_tokens"] for usage in usages) if usages else None,
        "sse_ms": sse_ms,
    }


def reconcile(obs, sdk, initial, evidence, trace_id):
    adopted = {row["artifact_id"]: row for product in evidence.get("products", [])
               for row in product["receipts"].values()}
    checked = []
    for row in adopted.values():
        board = next((item for item in obs.board if item["artifact_id"] == row["artifact_id"]
                      and public_receipt(item) == public_receipt(row)), None)
        actual = next((item for item in initial if item["runtime_call_id"] == row["tool_call_id"]), None)
        passed = bool(board and actual and row["run_id"] == evidence["run_id"] and row["source"] == actual["source"]
                      and row["arguments"] == actual["arguments"] and row["result"] == actual["result"]
                      and actual["end_ns"] <= board["ns"])
        checked.append({"receipt": row, "reader": actual, "board": board, "passed": passed})
    handoffs = verify_handoffs(obs, sdk, trace_id, evidence) if adopted else []
    for item in handoffs:
        task_skus = {row["arguments"]["sku"] for row in item["task"]["read_scope"]}
        applied = [row for row in adopted.values() if row.get("task_id") == item["task"]["task_id"]]
        item["applied_artifacts"] = len(applied)
        if not item["receipts"]:
            item["passed"] = bool(item["worker_id"] and item["board_task"] == item["task"] and item["reclaimed"]
                and all(any(read["source"] == action["tool"] and
                            all(read["arguments"].get(key) == value for key, value in action["arguments"].items())
                            for action in item["task"]["read_scope"]) for read in item["downstream_reads"]))
        scope = {key: evidence[key] for key in ("target", "candidates", "windows", "sales_timezone", "statistics_date", "run_id")}
        item["sdk_tool_calls"] = {}
        for request in sdk:
            if request.get("worker_id") != item["worker_id"]:
                continue
            response = request.get("response", {})
            if choices := response.get("body", {}).get("choices"):
                response = choices[0]["message"]
            for call_id, name, raw in calls(response):
                try:
                    item["sdk_tool_calls"][call_id] = {"source": name, "arguments": json.loads(raw)}
                except (TypeError, ValueError):
                    continue
        sdk_reads_match = all(
            (call := item["sdk_tool_calls"].get(row["tool_call_id"])) and call["source"] == row["source"]
            and isinstance(call["arguments"], dict)
            and all(row["arguments"].get(key) == value for key, value in call["arguments"].items())
            for row in item["downstream_reads"]
        )
        item["passed"] &= (item["scope"] == scope and sdk_reads_match
                           and all(row["first_input_receipt"]["arguments"]["sku"] in task_skus for row in item["receipts"]))
        item["adopted"] = applied
    return checked, handoffs


def report(output, runs, fixed, manifest):
    pairs = []
    for row in runs:
        if row["group"] != "B" or row["case"] not in fixture.CASES:
            continue
        left = next((item for item in runs if item["case"] == row["case"] and item["pair"] == row["pair"]
                     and item["group"] == "A"), None)
        if left is None:
            continue
        metrics = ("sse_ms", "sdk_requests", "input_tokens", "output_tokens", "bottom_reads", "rechecks", "initial_reads")
        pairs.append({"case": row["case"], "pair": row["pair"], "order": row["order"], "A": left["folder"],
                      "B": row["folder"], "business_score_delta": row["business_score"] - left["business_score"],
                      "delta": {name: round(row["metrics"][name] - left["metrics"][name], 3)
                                if row["metrics"][name] is not None and left["metrics"][name] is not None else None
                                for name in metrics}, "b_delegated": row["metrics"]["workers_started"] > 0})
    expected = 2 * (len(fixture.CASES) + len(fixture.FIXED_ONLY)) if fixed else 4 * len(fixture.CASES)
    result = {"fixed": fixed, "passed": len(runs) == expected and all(row["passed"] for row in runs),
              "complete": len(runs) == expected, "frozen_sha256": manifest, "rounds": runs, "pairs": pairs,
              "b_actual_dispatch_pairs": [row for row in pairs if row["b_delegated"]],
              "groups": {group: {"business_passed": sum(row["business_passed"] for row in runs if row["group"] == group),
                                 "rounds": sum(row["group"] == group for row in runs)} for group in "AB"}}
    write(output / "report.json", result)
    return result["passed"]


def run(output, prior, browser_smoke=False):
    fixed = prior is None
    startup = output / "startup.sqlite3"
    configure(startup, "mock" if fixed else "glm")
    if not fixed:
        credentials = dotenv_values(".env")
        os.environ.setdefault("GLM_API_KEY", credentials.get("GLM_API_KEY") or "")
        os.environ["GLM_MODEL"] = MODEL
        if not os.environ["GLM_API_KEY"].strip():
            raise RuntimeError("GLM_API_KEY is required in live mode")
    from fastapi.testclient import TestClient
    from backend import observability
    from backend.agent import listing_inspection, listing_run, llm_provider, request_budget, team_contracts, team_runtime, team_tools
    from backend.database import chat_groups, database, listing_operations

    password = token_urlsafe(24)
    redactor = Redactor([password, os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY")])
    manifest = freeze(output, database, password, prior)
    database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(startup)
    database.init_db()
    obs, active, writes = Observation(), {}, []
    barrier = Barrier(obs)
    model = Replay(obs, active) if fixed else llm_provider.ProviderFactory.create()
    date = datetime.fromisoformat(base.AS_OF + "T12:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    accept, listing = chat_groups.accept, listing_run.run_listing

    def accept_frozen(*args, **kwargs):
        group_run, context = accept(*args, **kwargs)
        return replace(group_run, requested_at=date), context

    def policy(message, user_id, group_run, provider, model_name, budget, trace_id, sink):
        obs.budgets.append({"run_id": group_run.run_id, "trace_id": trace_id, "delegation": active["group"] == "B",
                            "remaining": budget.execution.remaining()})
        return listing(message, user_id, group_run, provider, model_name, budget, trace_id, sink,
                       allow_delegation=active["group"] == "B")

    class LiveAudit(GLMAudit):
        run_cap = 4 * len(fixture.CASES) * 12

        def request(self, request):
            if (not self.enabled or self.case != active.get("id") or self.model_link.get().get("step") not in MODEL_STEPS
                    or database.sandbox_db_path() != active["db"] or sql.snapshot(active["db"]) != active["before"]):
                raise RuntimeError("The active request is outside the frozen synthetic inspection")
            return super().request(request)

        def response(self, response):
            super().response(response)
            self.stop = None

    completed = []
    audit = None
    with ExitStack() as stack:
        for module in (observability, listing_run, listing_inspection, llm_provider, request_budget, team_tools, team_runtime,
                       listing_operations, chat_groups):
            stack.enter_context(patch.object(module, "add_trace_step", obs.trace_step(module.add_trace_step)))
        for key, value in {"TRACE_LOG_FILE": "traces.jsonl", "ERROR_LOG_FILE": "errors.jsonl",
                           "FUNC_LOG_FILE": "functions.jsonl"}.items():
            stack.enter_context(patch.object(observability, key, output / value))
        stack.enter_context(patch.object(sqlite3, "connect", audit_writes(sqlite3.connect, active, writes)))
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=model))
        stack.enter_context(patch.object(chat_groups, "accept", accept_frozen))
        stack.enter_context(patch.object(listing_run, "run_listing", policy))
        stack.enter_context(patch("backend.memory.conversation_memory.store_conversation"))
        stack.enter_context(patch.object(team_tools, "trace_tool_call", obs.trace_tool(team_tools.trace_tool_call)))
        board = team_contracts.ReceiptBoard
        stack.enter_context(patch.object(board, "append", obs.board_append(board.append)))
        stack.enter_context(patch.object(board, "accept", obs.board_accept(board.accept)))
        stack.enter_context(patch.object(board, "accept_direct", obs.board_accept(board.accept_direct, direct=True)))
        for source, name in READERS.items():
            reader = barrier.wrap(source, getattr(listing_operations, name))
            stack.enter_context(patch.object(listing_operations, name, obs.reader(source, reader)))
        if not fixed:
            if model._model != MODEL or str(model._client.base_url).rstrip("/") != ENDPOINT:
                raise RuntimeError("Expected the official GLM model and endpoint")
            stack.callback(model._client.close)
            audit = LiveAudit(output / "sdk.sqlite3", redactor, uuid4().hex, LiveAudit.run_cap)
            hooks = model._client._client.event_hooks
            hooks["request"].append(audit.request)
            hooks["response"].append(audit.response)
            for module in (llm_provider, team_runtime):
                stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        from fast_server import app

        cases = (*fixture.CASES, *fixture.FIXED_ONLY) if fixed else fixture.CASES
        for pair, order in enumerate(("AB",) if fixed else ("AB", "BA"), 1):
            for case in cases:
                for group in order:
                    identifier = f"{pair:02}-{case}-{group}"
                    folder = output / identifier
                    folder.mkdir()
                    db = folder / "sandbox.sqlite3"
                    frozen = output / "frozen" / case
                    shutil.copyfile(frozen / "seed.sqlite3", db)
                    before = read(frozen / "sql-before.json")
                    if sql.snapshot(db) != before:
                        raise RuntimeError("Database clone differs from frozen fixture")
                    database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(db)
                    active.update(id=identifier, case=case, group=group, db=db, before=before)
                    obs.reset()
                    writes.clear()
                    armed = fixed and group == "B" and case in {"base", "swap", "missing-fact", "tie"}
                    barrier.reset(armed)
                    with TestClient(app, raise_server_exceptions=False) as client:
                        login = client.post("/api/login", json={"username": "task45-operator", "password": password})
                        login.raise_for_status()
                        headers = {"Authorization": "Bearer " + login.json()["data"]["token"]}
                        created = client.post("/api/chat/conversations", headers=headers, json={"title": identifier})
                        created.raise_for_status()
                        conversation = created.json()["data"]["conversation_id"]
                        if case == "simple":
                            selection = send(client, headers, "选 SKU " + base.TOTE, conversation)
                            write(folder / "selection-sse.json", selection)
                        if audit:
                            audit.case, audit.enabled = identifier, True
                        try:
                            sse = send(client, headers, fixture.question(case), conversation)
                        finally:
                            if audit:
                                audit.enabled = False
                        terminal = sse["terminal"]
                        run_id, trace_id = terminal.get("run_id"), terminal.get("trace_id")
                        endpoint = f"/api/chat/conversations/{conversation}"
                        history = client.get(endpoint + "/history", headers=headers).json()["data"]
                        stored = client.get(endpoint + f"/runs/{run_id}/evidence", headers=headers)
                        evidence = stored.json().get("data", {}) if stored.status_code == 200 else {}
                    after = sql.snapshot(db)
                    sdk = list(obs.requests) if fixed else [row for row in audit.rows() if row["case"] == identifier]
                    initial = [row for row in obs.reads if row.get("runtime_call_id")]
                    adopted, handoffs = reconcile(obs, sdk, initial, evidence, trace_id)
                    answer_row = next((row for row in reversed(history["entries"])
                                       if row["kind"] == "assistant" and row["run_id"] == run_id), {})
                    answer = answer_row.get("content", "")
                    business_checks = business(case, answer, evidence, read(frozen / "expected.json"),
                                               read(frozen / "source.json"), initial)
                    metrics = measure(obs, sdk, initial, fixed, handoffs, sse["sse_ms"])
                    checks = {"sse_done": terminal.get("type") == "done",
                              "zero_writes": before == after and not writes,
                              "zero_actions": not after["proposals"] and not after["receipts"]
                              and not answer_row.get("listing_actions") and not answer_row.get("pause_actions")
                              and all("proposal" not in key and "action" not in key for key in terminal),
                              "tool_allowlist": all(item["function"]["name"] in (set(READERS) | ({DELEGATE} if group == "B" else set()))
                                                    for row in sdk for item in row["body"].get("tools") or ()),
                              "worker_accounting": metrics["workers_started"] == metrics["workers_reclaimed"] == len(handoffs)
                              and all(row["passed"] for row in handoffs),
                              "final_provenance": all(row["passed"] for row in adopted)}
                    if case == "simple":
                        checks["simple_worker_free"] = not metrics["workers_started"] and not sdk
                    if armed:
                        page = next((row for row in handoffs if row["task"]["task_id"] == "tote-page"), {})
                        scoped_reads = [row for row in initial if row.get("parent_call_id") == page.get("worker_id")]
                        checks["barrier_and_handoff"] = bool(
                            page.get("passed") and page["receipts"] and barrier.record.get("independent_finished")
                            and barrier.record.get("independent_wait_met") and metrics["read_overlaps"]
                            and not any(row["detail"] == "tote-page" for row in barrier.record["workers_before_release"])
                            and page["worker_id"] not in barrier.record["sdk_before_release"]
                            and len(scoped_reads) == 1 and scoped_reads[0]["start_ns"] > barrier.record["released_ns"]
                            and scoped_reads[0]["arguments"]["sku"] == base.TOTE
                            and scoped_reads[0]["source"] == CURRENT and any(row["passed"] and
                                row["receipt"]["artifact_id"] == page["receipts"][0]["board"]["artifact_id"] for row in adopted)
                            and any(row["passed"] and row["receipt"]["tool_call_id"] == scoped_reads[0]["runtime_call_id"]
                                    for row in adopted))
                        checks["cross_sku_plan_rejected"] = metrics["attempted_dispatches"] == 2 and metrics["approved_dispatches"] == 1
                        if case == "tie":
                            checks["cross_sku_worker_rejected"] = len(initial) == 9 and any(
                                row.get("error_code") == "task_read_out_of_scope" for row in obs.trace)
                    if fixed and group == "A" and case in {"base", "swap", "missing-fact", "tie"}:
                        checks["direct_scope_rejected"] = len(initial) == 9 and sum(
                            row.get("error_code") == "listing_tool_out_of_scope" for row in obs.trace) == 3
                        if case == "base":
                            checks["first_live_format_regression"] = sum(row.get("step") == "listing_inspection_review"
                                                                         for row in sdk) == 1
                    all_checks = {**business_checks, **checks}
                    result = {"case": case, "pair": pair, "order": order, "group": group, "folder": identifier,
                              "run_id": run_id, "trace_id": trace_id, "passed": all(all_checks.values()),
                              "business_passed": all(business_checks.values()),
                              "business_score": sum(business_checks.values()), "business_max": len(business_checks),
                              "first_deviation": next((key for key, passed in all_checks.items() if not passed), None),
                              "checks": all_checks, "metrics": metrics}
                    artifacts = {"sse": sse, "history": history, "evidence": evidence, "sql-before": before, "sql-after": after,
                                 "write-audit": writes, "bottom-reads": obs.reads, "trace": obs.trace,
                                 "board": obs.board, "task-sheets": obs.plans, "budgets": obs.budgets,
                                 "model-replay" if fixed else "sdk": sdk, "worker-handoff": handoffs,
                                 "adopted-receipts": adopted, "barrier": barrier.record, "result": result}
                    for name, value in artifacts.items():
                        write(folder / f"{name}.json", redactor.clean(value))
                    completed.append(result)
                    report(output, completed, fixed, manifest)
                    print(identifier, result["passed"], [name for name, passed in all_checks.items() if not passed], flush=True)
        if browser_smoke:
            from backend.captcha import captcha
            from tests.home_diagnosis_server import demo_captcha

            folder = output / "browser-smoke"
            folder.mkdir()
            db = folder / "sandbox.sqlite3"
            shutil.copyfile(output / "frozen" / "base" / "seed.sqlite3", db)
            before = sql.snapshot(db)
            database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(db)
            active.update(case="base", group="B", db=db, before=before)
            obs.reset()
            writes.clear()
            barrier.reset(False)
            write(folder / "scenario.json", {"id": "inspection", "message": fixture.QUESTION,
                                              "inspect_evidence": list(base.PRODUCTS), "observe_pending": True})
            with patch.object(captcha, "generate_captcha", demo_captcha), serve(app) as origin:
                process = subprocess.run(
                    ["node", "tests/task45_t1_browser.cjs", origin, str(folder), str(db)],
                    env={**browser_env(), "TASK45_BROWSER_PASSWORD": password}, capture_output=True, timeout=120,
                )
            browser = read(folder / "browser.json")
            write(folder / "browser.json", redactor.clean(browser))
            smoke = {"passed": process.returncode == 0 and browser.get("listingCards") == 0
                     and browser.get("approveButtons") == 0 and before == sql.snapshot(db) and not writes,
                     "exit_code": process.returncode, "writes": writes,
                     "stderr": redactor.clean(process.stderr.decode(errors="replace"))}
            write(folder / "result.json", smoke)
            final_report = read(output / "report.json")
            final_report["browser_smoke_passed"] = smoke["passed"]
            final_report["passed"] &= smoke["passed"]
            write(output / "report.json", final_report)
            if not smoke["passed"]:
                return False
            return final_report["passed"]
    return report(output, completed, fixed, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixed-results", type=Path)
    parser.add_argument("--browser-smoke", action="store_true", help="Also verify the fixed checklist in Edge")
    args = parser.parse_args()
    prior = args.fixed_results.resolve() if args.fixed_results else None
    if prior and args.browser_smoke:
        parser.error("The browser smoke uses the fixed model")
    if prior:
        fixed = read(prior / "report.json")
        if (not fixed["fixed"] or not fixed["passed"] or fixed["frozen_sha256"] != sql.digest(prior / "frozen.json")
                or sum(row["checks"].get("barrier_and_handoff", False) for row in fixed["rounds"]) != 4):
            parser.error("Live GLM requires the passing fixed gate and all four dependent handoffs")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    return run(output, prior, args.browser_smoke)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
