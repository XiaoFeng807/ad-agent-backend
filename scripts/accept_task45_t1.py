"""Run frozen StorePilot business questions and isolated controls through Edge and SSE."""

import argparse
import json
import os
import sqlite3
import subprocess
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, Lock, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from scripts import task45_t1_fixture as fixture
from scripts.accept_task42_t1 import configure, read, serve, write


SQL = Path(__file__).with_name("task45_t1_sales.sql")
SALES, CURRENT, FACTS = "get_listing_sales", "get_listing_current", "get_listing_facts"
READS = [SALES, CURRENT, FACTS]
LAST_WEEK_SQL = """
WITH RECURSIVE days(day) AS (
    SELECT :start_date UNION ALL SELECT date(day, '+1 day') FROM days WHERE day < :end_date
), points AS (
    SELECT sku, version, day, json_extract(series, '$."' || day || '"') units
    FROM listing_test_sales CROSS JOIN days WHERE shop_id=:shop_id AND marketplace_id=:marketplace_id
)
SELECT sku, version, COUNT(units) current_covered,
       CASE WHEN COUNT(units)=COUNT(*) THEN SUM(units) END current_units,
       json_group_array(day) FILTER (WHERE units IS NULL) current_missing
FROM points GROUP BY sku, version ORDER BY sku
"""


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def sql_connection(path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def seed(path, database, password, sources):
    database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(path)
    database.init_db()
    with closing(database.get_db()) as conn, conn:
        owner = conn.execute("INSERT INTO users(username,password) VALUES (?,?)",
                             ("task45-operator", database.hash_password(password))).lastrowid
        conn.execute("INSERT INTO listing_test_shops VALUES (?,?,?)", (fixture.SHOP, fixture.MARKET, owner))
        for product in sources:
            key = (fixture.SHOP, fixture.MARKET, product["target"]["sku"])
            conn.execute("INSERT INTO listing_test_products VALUES (?,?,?,?,?,?,?,?)", (
                *key, product["listing_version"], product["title"], json.dumps(product["bullets"]),
                product["price_cents"], product["inventory_units"]))
            conn.execute("INSERT INTO listing_test_sales VALUES (?,?,?,?,?)",
                         (*key, product["sales_version"], json.dumps(product["series"])))
            conn.execute("INSERT INTO listing_test_facts VALUES (?,?,?,?,?)",
                         (*key, product["facts_version"], json.dumps(product["facts"])))
            if product["target"]["sku"] == fixture.TOTE:
                conn.execute("INSERT INTO listing_test_required_facts VALUES (?,?,?,?)",
                             (*key, json.dumps({"capacity": "主隔层容量"}, ensure_ascii=False)))


def snapshot(path):
    tables = {"products": "listing_test_products ORDER BY sku", "facts": "listing_test_facts ORDER BY sku",
              "sales": "listing_test_sales ORDER BY sku", "required_facts": "listing_test_required_facts ORDER BY sku",
              "proposals": "listing_change_proposals ORDER BY action_id",
              "receipts": "listing_change_receipts ORDER BY receipt_id"}
    with closing(sql_connection(path)) as conn:
        return {key: [dict(row) for row in conn.execute("SELECT * FROM " + table)] for key, table in tables.items()}


def oracle(path, windows, *, single=False):
    with closing(sql_connection(path)) as conn:
        return [dict(row) for row in conn.execute(LAST_WEEK_SQL if single else SQL.read_text(encoding="utf-8"), {
            "shop_id": fixture.SHOP, "marketplace_id": fixture.MARKET,
            **dict(zip(("start_date", "end_date") if single else fixture.KEYS, windows)),
        })]


class Replay:
    name = "Mock"

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.requests = []
        self.lock = Lock()
        self.case = None

    def create(self, **payload):
        messages = payload["messages"]
        system = messages[0]["content"]
        tools = [row["function"]["name"] for row in payload.get("tools") or []]
        calls, content = [], None
        if system.startswith("Produce only a JSON object"):
            role = "draft"
            context = json.loads(messages[-1]["content"])
            content = json.dumps({"bullets": [{"fact_id": fact["fact_id"], "text": fact["options"][0]}
                                              for fact in context["facts"]["facts"]]})
            if self.case == "confirmed-zero" and "previous response" not in system:
                content = json.dumps({"answer": "37 to 0." + content})
        elif "temporary worker" in system:
            role = "worker"
            task = json.loads(messages[1]["content"])["task"]
            if any(row["role"] == "tool" for row in messages):
                content = json.dumps({"status": "succeeded", "output": "Returned the requested listing evidence."})
            else:
                calls = [(row["tool"], row["arguments"]) for row in task["read_scope"]]
        elif system.startswith("You are the operations agent for a local test Listing."):
            role = "main"
            scope = json.loads(system.splitlines()[1])
            def task(task_id, tool, skus, **dependencies):
                return {"task_id": task_id, "objective": f"Verify {tool}", "capabilities": [tool],
                        "read_scope": [{"tool": tool, "arguments": {"sku": sku}} for sku in skus],
                        "expected_output": "Tool receipt", "stop_condition": "Evidence returned", **dependencies}

            if not scope["target"]:
                skus = scope["pending_sales_skus"]
                if self.case == "swap" and "delegate_tasks" in tools:
                    calls = [("delegate_tasks", {"reason": "Read sales for all candidates",
                                                 "tasks": [task("sales", SALES, skus)]})]
                else:
                    calls = [(SALES, {"sku": sku}) for sku in skus]
            elif "delegate_tasks" in tools and self.case in {"base", "swap", "control-tote", "control-riser", "control-missing-fact"}:
                skus = [scope["target"]["sku"]]
                calls = [("delegate_tasks", {"reason": "Verify the source facts before reviewing the listing", "tasks": [
                    task("facts", FACTS, skus), task("listing", CURRENT, skus, dependencies=["facts"], input_refs=["facts"]),
                ]})]
            else:
                calls = [(tool, {"sku": scope["target"]["sku"]}) for tool in READS if tool in tools]
        else:
            role = "other"
            content = "本轮未取得可核验的商品销量。"
        response = {"content": content, "tool_calls": [
            {"id": f"t45-{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for index, (name, args) in enumerate(calls)
        ]}
        with self.lock:
            self.requests.append({"ns": monotonic(), "role": role, "body": payload, "response": response})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(**response))])


class EvidenceBarrier:
    """Delay the first actual fact read and sample the real execution while it is blocked."""

    def __init__(self, original, model, observability):
        self.original = original
        self.model = model
        self.observability = observability
        self.armed = False
        self.log = {}

    def read(self, *args, **kwargs):
        if self.armed:
            self.armed = False
            released = Event()
            self.log["blocked_ns"] = monotonic()

            def inspect():
                sleep(0.45)
                try:
                    with self.model.lock:
                        self.log["blocked_model_calls"] = list(self.model.requests)
                    with self.observability._trace_lock:
                        self.log["blocked_traces"] = [
                            {**trace, "steps": list(trace["steps"])}
                            for trace in self.observability._traces.values() if trace["status"] == "running"
                        ]
                    self.log["released_ns"] = monotonic()
                finally:
                    released.set()

            Thread(target=inspect, daemon=True).start()
            if not released.wait(5):
                raise RuntimeError("Evidence barrier did not release")
        return self.original(*args, **kwargs)


def worker_inputs(rows):
    result = {}
    for row in rows:
        messages = row["body"]["messages"]
        if "temporary worker" in messages[0]["content"]:
            context = json.loads(messages[1]["content"])
            result.setdefault(context["task"]["task_id"], {"request": row, "context": context})
    return result


def worker_handoffs(inputs, steps, evidence):
    artifacts = evidence.get("artifacts", {})
    handoffs = []
    for task_id, model_input in inputs.items():
        context = model_input["context"]
        task = next((step for step in steps if step["stage"] == "worker" and step["action"] == "created"
                     and step["detail"] == task_id), {})
        worker_id = task.get("runtime_call_id")
        if not worker_id or not any(step["stage"] == "batch" and step["action"] == "completed"
                                   and step.get("parent_call_id") == task.get("parent_call_id") for step in steps):
            continue
        first_call = next((i for i, step in enumerate(steps) if step["stage"] == "model_call"
                           and step.get("parent_call_id") == worker_id), None)
        if first_call is None or not any(step["stage"] == "tool_result" and step["status"] == "ok"
                                        and step.get("parent_call_id") == worker_id for step in steps[first_call:]):
            continue
        supplied = [*context.get("direct_inputs", []), *(row for refs in context.get("inputs", {}).values()
                                                         for row in refs)]
        verified = []
        for row in supplied:
            source = row["source"]
            link = artifacts.get(source, {})
            key = {SALES: "metrics", CURRENT: "listing", FACTS: "facts"}.get(source)
            valid = (row["status"] == "succeeded" and key and (
                row in evidence.get("comparison_receipts", []) if source == SALES and "comparisons" in evidence else
                row["artifact_id"] == link.get("artifact_id") and row["tool_call_id"] == link.get("runtime_call_id")
                and row.get("result") == evidence.get(key)))
            if (not valid or context.get("scope", {}).get("target") != evidence.get("target")
                    or context.get("scope", {}).get("windows") != evidence.get("windows")):
                continue
            if any(step["stage"] == "tool_result" and step["action"] == source and step["status"] == "ok"
                   and step.get("runtime_call_id") == row["tool_call_id"] for step in steps[:first_call]):
                verified.append({"task_id": task_id, "worker_id": worker_id, "artifact_id": row["artifact_id"],
                                 "source": source, "tool_call_id": row["tool_call_id"],
                                 "target": row["result"]["target"], "version": row["result"].get("version")})
        comparison_refs = {row["artifact_id"] for row in evidence.get("comparison_receipts", [])}
        if len(verified) == len(supplied) and comparison_refs <= {row["artifact_id"] for row in verified}:
            handoffs.extend(verified)
    return handoffs


def check_metrics(metrics, expected, windows, source):
    if not metrics:
        return False
    for index, name in enumerate(("baseline", "current")):
        window = metrics[name]
        start, end = windows[index * 2:index * 2 + 2]
        points = [{"date": day, "units": value} for day, value in source["series"].items() if start <= day <= end]
        for day in json.loads(expected[name + "_missing"]):
            points.append({"date": day, "units": None})
        if (window["start_date"], window["end_date"], window["units"], window["covered_days"], window["days"]) != (
            start, end, expected[name + "_units"], expected[name + "_covered"], sorted(points, key=lambda p: p["date"])
        ):
            return False
    return (metrics["target"] == source["target"] and metrics["version"] == source["sales_version"]
            and metrics["units_delta"] == expected["delta"]
            and metrics["units_change_pct"] == (
                None if expected["change_pct"] is None else round(expected["change_pct"], 6)))


def score(case, browser, trace, before, after, rows, barrier, sources, sql, *, fixed):
    checks = {}

    def check(stage, name, condition):
        checks[name] = {"stage": stage, "passed": bool(condition)}

    round_data = (browser.get("rounds") or [{}])[-1]
    evidence = (round_data.get("evidence") or {}).get("body", {}).get("data") or {}
    steps = trace.get("steps", [])
    reads = [step for step in steps if step["stage"] == "business_read" and step["action"] in READS]
    workers = [step for step in steps if step["stage"] == "worker" and step["action"] == "created"]
    inputs = worker_inputs(rows)
    handoffs = worker_handoffs(inputs, steps, evidence)
    draft_calls = [row for row in rows if row["body"]["messages"][0]["content"].startswith("Produce only a JSON object")]
    draft_format_rejections = sum(step.get("error_code") == "listing_invalid_draft_format" for step in steps)
    response = "\n".join(event.get("content", "") for event in round_data.get("events", []) if event["type"] == "text")
    outside = (case.get("variant") or case["id"]) in ("missing-day", "missing-fact")
    missing_day = case.get("variant") == "missing-day"
    missing_fact = case.get("variant") == "missing-fact"
    comparison = case["group"] == "task" and not case.get("context")
    chosen = next((row["sku"] for row in sql["comparison"] if row["selected"]), None)
    sku = case.get("sku") or chosen
    source = next((item for item in sources if item["target"]["sku"] == sku), None)
    expected_targets = {item["target"]["sku"] for item in sources} if comparison else {sku}
    read_targets = [r["evidence"]["arguments"]["sku"] for r in reads if r["action"] == SALES]
    proposal = evidence.get("proposal") or {}
    final_listing = {row["sku"]: row for row in after["products"]}
    fact_refs = []

    check("选路", "自然题面经实际页面进入 SSE 并完成", round_data.get("request", {}).get("message") == case["message"]
          and round_data.get("terminal", {}).get("type") == "done" and not browser.get("failure")
          and not browser.get("pageErrors"))
    check("选路", "进入商品业务后的目标解析", evidence.get("kind") == "listing_operations_v1")
    entries = round_data.get("history", {}).get("data", {}).get("entries", [])
    entry = next((row for row in reversed(entries) if row["kind"] == "assistant"), {})
    check("选路", "提交后历史与本轮 SSE 和证据 GET 一致", entry.get("content") == response
          and entry.get("entry_id") == evidence.get("entry_id")
          and entry.get("run_id") == evidence.get("run_id"))
    check("取证", "销量实际工具覆盖冻结候选各一次", set(read_targets) == expected_targets and
          len(read_targets) == len(expected_targets))
    for read_step in reads:
        args = read_step["evidence"]["arguments"]
        dates = dict(zip(fixture.KEYS, fixture.WINDOWS))
        if case.get("context"):
            dates.update(current_start=fixture.LAST_WEEK[0], current_end=fixture.LAST_WEEK[1])
        check("取证", f"实际调用参数和工具回执 {read_step['runtime_call_id']}",
              args.get("shop_id") == fixture.SHOP and args.get("marketplace_id") == fixture.MARKET
              and (read_step["action"] == SALES and args["sku"] in expected_targets and all(
                   args.get(key) == dates[key] for key in (("current_start", "current_end") if case.get("context")
                                                         else fixture.KEYS))
                   and (not case.get("context") or not ({"baseline_start", "baseline_end"} & args.keys()))
                   or read_step["action"] != SALES and args["sku"] == sku)
              and any(s["stage"] == "tool_result" and s.get("runtime_call_id") == read_step["runtime_call_id"]
                      and s["status"] == "ok" for s in steps))
    sales_metrics = evidence.get("metrics") or {}
    if comparison:
        comparisons = evidence.get("comparisons", [])
        check("取证", "三款比较、排序和入选 SKU 对照独立 SQL",
              {row.get("target", {}).get("sku") for row in comparisons} == expected_targets
              and all(check_metrics(metric, next(expected for expected in sql["comparison"]
                                                if expected["sku"] == metric["target"]["sku"]), fixture.WINDOWS,
                                    next(product for product in sources if product["target"] == metric["target"]))
                      for metric in comparisons) and (evidence.get("target") or {}).get("sku") == chosen)
        check("取证", "逐 SKU 产物关联实际调用，排名先于入选商品资料读取",
              [row["result"] for row in evidence.get("comparison_receipts", [])] == comparisons
              and len({row["artifact_id"] for row in evidence.get("comparison_receipts", [])}) == len(expected_targets)
              and all(any(step["runtime_call_id"] == row["tool_call_id"] and step["action"] == SALES
                          and step["evidence"]["arguments"] == row["arguments"] for step in reads)
                      for row in evidence.get("comparison_receipts", []))
              and any(step["stage"] == "listing_ranking" and step["action"] == ("selected" if chosen else "incomplete")
                      and not any(prior["stage"] == "business_read" and prior["action"] in {CURRENT, FACTS}
                                  for prior in steps[:i]) for i, step in enumerate(steps)))
    elif case.get("context"):
        expected = next(row for row in sql["last_week"] if row["sku"] == sku)
        current = sales_metrics.get("current", {})
        check("取证", "答复给出该 SKU 上周日期和件数",
              (evidence.get("target") or {}).get("sku") == sku
              and set(sales_metrics) == {"version", "source", "target", "current"}
              and current.get("covered_days") == expected["current_covered"]
              and current.get("days") == [{"date": day, "units": units} for day, units in source["series"].items()
                                          if fixture.LAST_WEEK[0] <= day <= fixture.LAST_WEEK[1]]
              and (current.get("start_date"), current.get("end_date"), current.get("units")) ==
              (*fixture.LAST_WEEK, expected["current_units"])
              and all(token in response for token in (sku, *fixture.LAST_WEEK, f"{expected['current_units']} 件")))
        check("选路", "一次主 Agent 直接查数，Worker 和草案为零",
              [r.get("role") for r in reads] == ["orchestrator"] and not workers and not draft_calls)
    else:
        expected = next(row for row in sql["comparison"] if row["sku"] == sku)
        check("取证", "逐日覆盖、两窗销量和变化与独立 SQL 一致",
              check_metrics(sales_metrics, expected, fixture.WINDOWS, source))

    if missing_day:
        check("取证", "指出归属 SKU 与缺报日", all(token in response for token in (fixture.TOTE, fixture.MISSING_DAY, "6/7"))
              and not any(r["action"] in {CURRENT, FACTS} for r in reads))
    if missing_fact:
        check("取证", "指出入选 SKU 缺少主隔层容量依据",
              all(token in response for token in (fixture.TOTE, "主隔层容量依据"))
              and evidence.get("missing_fact_fields") == {"capacity": "主隔层容量"})
    if fixed and case["id"] == "confirmed-zero":
        check("起草", "首份草案格式错误后仅重试一次", len(draft_calls) == 2 and draft_format_rejections == 1)
    if fixed and case["id"] in ("base", "swap", "control-tote", "control-riser"):
        downstream = inputs.get("listing", {})
        context = downstream.get("context", {})
        blocked = [step for t in barrier.get("blocked_traces", []) for step in t["steps"]]
        blocked_requests = barrier.get("blocked_model_calls", [])
        check("交接", "上游事实读取被卡住期间，下游没有启动且无草案",
              any(s["stage"] == "business_read" and s["action"] == FACTS for s in blocked)
              and not any(s["stage"] == "business_read" and s["action"] == CURRENT for s in blocked)
              and not any(s["stage"] == "worker" and s["action"] == "created" and s["detail"] == "listing" for s in blocked)
              and "listing" not in worker_inputs(blocked_requests)
              and not any(row["role"] == "draft" for row in blocked_requests)
              and downstream.get("request", {}).get("ns", 0) > barrier.get("released_ns", float("inf")))
        fact_input = next((r for r in context.get("inputs", {}).get("facts", []) if r["source"] == FACTS), {})
        sales_input = next((r for r in context.get("direct_inputs", []) if r["source"] == SALES
                            and r["result"]["target"]["sku"] == sku), {})
        artifacts = evidence.get("artifacts", {})
        check("交接", "下游 Worker 首份输入收到相同 artifact_id、SKU、两窗和事实版本",
              set(inputs) == ({"facts", "listing", "sales"} if case["id"] == "swap" else {"facts", "listing"})
              and context.get("scope", {}).get("target") == source["target"]
              and context.get("scope", {}).get("windows") == list(fixture.WINDOWS)
              and context["task"]["read_scope"] == [{"tool": CURRENT, "arguments": {"sku": sku}}]
              and fact_input.get("artifact_id") == artifacts.get(FACTS, {}).get("artifact_id")
              and fact_input.get("result") == evidence.get("facts")
              and fact_input.get("result", {}).get("version") == source["facts_version"]
              and sales_input.get("artifact_id") == artifacts.get(SALES, {}).get("artifact_id")
              and sales_input.get("result") == sales_metrics
              and (not comparison or context.get("direct_inputs") == evidence["comparison_receipts"])
              and [r["role"] for r in reads if r["action"] in (CURRENT, FACTS)] == ["team_worker", "team_worker"]
              and any(s["stage"] == "batch" and s["action"] == "completed" for s in steps))

    if outside or case.get("context"):
        check("起草", "零草案和零变更提案", not proposal and not draft_calls and not after["proposals"])
        check("审批", "页面零批准入口、数据库零回执且商品原样", browser.get("approveButtons") == 0
              and browser.get("listingCards") == 0 and before == after)
    else:
        facts = {fact["fact_id"]: fact for fact in (source or {}).get("facts", [])}
        for bullet in proposal.get("bullets", []):
            fact = facts.get(bullet["fact_id"], {})
            fact_refs.append({"sku": sku, "version": source["facts_version"] if source else None,
                              "artifact_id": proposal.get("artifact_refs", {}).get("facts"), "bullet": bullet,
                              "frozen_fact": fact, "passed": bullet["text"] in fact.get("options", [])
                              and bullet["statement"] == fact.get("statement")})
        check("起草", "仅为入选 SKU 拟五条，每条对应独立事实原文",
              len(fact_refs) == len({r["bullet"]["fact_id"] for r in fact_refs}) == 5
              and all(r["passed"] for r in fact_refs) and len(draft_calls) == 1 + draft_format_rejections and source
              and proposal.get("target") == source["target"]
              and proposal.get("original_version") == source["listing_version"]
              and proposal.get("facts_version") == source["facts_version"]
              and len(after["proposals"]) == 1 and after["proposals"][0]["sku"] == sku)
        if case.get("approve") or case["id"] == "base":
            pre = browser.get("sqlBeforeApproval", {})
            post, get, refresh = (browser.get(key, {}) for key in ("post", "get", "refresh"))
            actual = get.get("body", {}).get("data", {})
            receipt = actual.get("receipt") or {}
            bullets = proposal.get("after_bullets")
            expected_products = {row["sku"]: dict(row) for row in before["products"]}
            if bullets:
                expected_products[sku].update(version=source["listing_version"] + 1,
                                              bullets=json.dumps(bullets, ensure_ascii=False, separators=(",", ":")))
            check("审批", "审批前独立 SQL 商品未变、待审批且零回执", pre.get("products") == before["products"]
                  and pre.get("receipts") == [] and len(pre.get("proposals") or []) == 1
                  and browser.get("pending", {}).get("body", {}).get("data", {}).get("state") == "pending")
            check("审批", "页面 POST 后独立 GET，刷新原答案再次 GET 同一回执",
                  [request["method"] for request in browser.get("approvals", [])] == ["POST", "GET", "GET"]
                  and [packet.get("status") for packet in (post, get, refresh)] == [200, 200, 200]
                  and post.get("body") == get.get("body") == refresh.get("body")
                  and receipt.get("receipt_id") and receipt.get("source_trace_id") == trace.get("trace_id")
                  and receipt["receipt_id"] in browser.get("refreshedReceipt", "")
                  and actual["listing"]["bullets"] == receipt["committed_bullets"] == bullets
                  and actual["listing"]["version"] == receipt["committed_version"] == source["listing_version"] + 1)
            check("审批", "SQL 仅入选 SKU 五点与版本变化且零额外回执",
                  final_listing == expected_products and before["facts"] == after["facts"]
                  and before["sales"] == after["sales"] and before["required_facts"] == after["required_facts"]
                  and len(after["receipts"]) == 1
                  and after["receipts"][0]["receipt_id"] == receipt.get("receipt_id")
                  and browser.get("sqlAfterApproval") == browser.get("sqlAfterRefresh"))
        else:
            check("审批", "待审可见、审批前 SKU 内容版本不变且零回执",
                  browser.get("approveButtons") == 1 and before["products"] == after["products"]
                  and not after["receipts"])
    first = min((row["stage"] for row in checks.values() if not row["passed"]), default=None,
                key=("选路", "取证", "交接", "起草", "审批").index)
    return {"passed": first is None, "first_deviation": first, "checks": checks,
            "trace_id": trace.get("trace_id"), "worker_count": len(workers), "worker_first_inputs": inputs,
            "collaboration": "passed" if handoffs else "unverified" if workers else "uncovered",
            "verified_handoffs": handoffs,
            "business_reads": reads, "bullet_sources": fact_refs, "draft_calls": len(draft_calls),
            "response": response, "expected_candidate": sku, "business_seconds": trace.get("total_duration")}


RULES = {
    "last_week": "Previous completed calendar week, Monday through Sunday, Asia/Shanghai.",
    "comparison": "Two contiguous seven-day periods through yesterday, Asia/Shanghai. Rank by relative decline. "
                  "Only a unique decline with every candidate covered and positive baselines selects a SKU.",
    "missing_day": "An absent date makes the window total and comparison unknown. A recorded zero is covered.",
    "draft": "Exactly five distinct frozen facts of the selected SKU, one per bullet. Preserve number, unit and condition.",
    "write": "No Listing write before approval or on any gap/read-only case. One target write and one receipt on approval. "
             "Separate browser GET and reload of the original answer must return the same actual listing and receipt.",
    "collaboration": "Actual workers, verified upstream artifact linked to first SDK/model inputs, and downstream tool "
                     "completion. A successful business answer may be direct.",
}


def freeze(output, database, password, prior):
    scenarios = fixture.cases()
    manifest = {"as_of": fixture.AS_OF, "timezone": "Asia/Shanghai", "comparison_windows": fixture.WINDOWS,
                "last_week_windows": fixture.LAST_WEEK, "rules": RULES, "sql_sha256": digest(SQL),
                "last_week_sql_sha256": sha256(LAST_WEEK_SQL.encode()).hexdigest(),
                "fixture_sha256": digest(Path(fixture.__file__)), "scenarios": []}
    for case in scenarios:
        folder = output / case["id"]
        folder.mkdir()
        db_path = folder / "sandbox.sqlite3"
        sources = fixture.source(case.get("variant", "base"))
        seed(db_path, database, password, sources)
        expected = {"comparison": oracle(db_path, fixture.WINDOWS), "last_week": oracle(db_path, fixture.LAST_WEEK, single=True)}
        write(folder / "scenario.json", case)
        write(folder / "source.json", sources)
        write(folder / "sql-expected.json", expected)
        write(folder / "sql-before.json", snapshot(db_path))
        manifest["scenarios"].append({"definition": case, "source_sha256": digest(folder / "source.json"),
                                      "oracle_sha256": digest(folder / "sql-expected.json")})
    write(output / "frozen.json", manifest)
    if prior and read(output / "frozen.json") != read(prior / "frozen.json"):
        raise RuntimeError("Fixed and live frozen inputs differ")
    return scenarios


def run(output, fixed, prior):
    from dotenv import dotenv_values
    from scripts.run_real_diagnosis import Redactor
    from scripts.run_task27_t5 import ENDPOINT, GLMAudit, MODEL, browser_env

    bootstrap = output / "startup.sqlite3"
    configure(bootstrap, "mock" if fixed else "glm")
    if not fixed:
        credentials = dotenv_values(".env")
        os.environ.setdefault("GLM_API_KEY", credentials.get("GLM_API_KEY") or "")
        os.environ.setdefault("GLM_MODEL", credentials.get("GLM_MODEL") or MODEL)
        if not os.environ["GLM_API_KEY"]:
            raise RuntimeError("GLM_API_KEY is required")
    from backend import observability
    from backend.agent import llm_provider, team_runtime
    from backend.captcha import captcha
    from backend.database import chat_groups, database, listing_operations
    from tests.home_diagnosis_server import demo_captcha

    password = token_urlsafe(24)
    redactor = Redactor([password, os.environ["JWT_SECRET"], os.environ.get("GLM_API_KEY")])
    scenarios = freeze(output, database, password, prior)
    if not fixed:
        scenarios = [case for case in scenarios if case["group"] == "task"]
    database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(bootstrap)
    database.init_db()
    requested_at = datetime.fromisoformat(fixture.AS_OF + "T12:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    accept = chat_groups.accept

    def accept_at_frozen_date(*args, **kwargs):
        group_run, context = accept(*args, **kwargs)
        return replace(group_run, requested_at=requested_at), context

    class LiveAudit(GLMAudit):
        run_cap = len(scenarios) * 12

        def case_cap(self, case):
            return 12

        def request(self, request):
            case = next((case for case in scenarios if case["id"] == self.case), None)
            if not case or case.get("context") or self.model_link.get().get("step") not in {
                    "listing_operations", "listing_draft", "team_worker"}:
                raise RuntimeError("Outbound request is outside the frozen Listing cases")
            path = output / self.case / "sandbox.sqlite3"
            if database.sandbox_db_path() != path:
                raise RuntimeError("Outbound request is outside the case's isolated database")
            current = snapshot(path)
            frozen = read(path.parent / "sql-before.json")
            if current != frozen:
                raise RuntimeError("Outbound request requires the unmodified synthetic fixture")
            return super().request(request)

    model = Replay() if fixed else llm_provider.ProviderFactory.create()
    barrier = EvidenceBarrier(listing_operations.read_facts, model, observability) if fixed else None
    audit = None
    results = {}
    with ExitStack() as stack:
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=model))
        stack.enter_context(patch.object(captcha, "generate_captcha", demo_captcha))
        stack.enter_context(patch.object(chat_groups, "accept", accept_at_frozen_date))
        stack.enter_context(patch("backend.memory.conversation_memory.store_conversation"))
        for attr, filename in (("TRACE_LOG_FILE", "traces.jsonl"), ("ERROR_LOG_FILE", "errors.jsonl"),
                               ("FUNC_LOG_FILE", "functions.jsonl")):
            stack.enter_context(patch.object(observability, attr, output / filename))
        if fixed:
            stack.enter_context(patch.object(listing_operations, "read_facts", barrier.read))
        else:
            stack.callback(model._client.close)
            if model._model != MODEL or str(model._client.base_url).rstrip("/") != ENDPOINT:
                raise RuntimeError("Expected the official GLM endpoint and " + MODEL)
            audit = LiveAudit(output / "sdk.sqlite3", redactor, uuid4().hex, LiveAudit.run_cap)
            hooks = model._client._client.event_hooks
            hooks["request"].append(audit.request)
            hooks["response"].append(audit.response)
            for module in (llm_provider, team_runtime):
                stack.enter_context(patch.object(module, "add_trace_step", audit.trace_model(module.add_trace_step)))
        from fast_server import app

        with serve(app) as origin:
            for case in scenarios:
                name = case["id"]
                folder = output / name
                db_path = folder / "sandbox.sqlite3"
                database.settings.DIAGNOSIS_SANDBOX_DB_PATH = str(db_path)
                before = read(folder / "sql-before.json")
                if fixed:
                    model.case = name
                    model.requests.clear()
                    barrier.log = {}
                    barrier.armed = name in ("base", "swap", "control-tote", "control-riser")
                else:
                    audit.case, audit.enabled = name, True
                started = monotonic()
                process = None
                try:
                    process = subprocess.run(["node", "tests/task45_t1_browser.cjs", origin, str(folder), str(db_path)],
                                             env={**browser_env(), "TASK45_BROWSER_PASSWORD": password},
                                             capture_output=True, timeout=150, check=False)
                except subprocess.TimeoutExpired:
                    write(folder / "browser-timeout.json", {"timeout_seconds": 150})
                finally:
                    if audit:
                        audit.enabled = False
                if process:
                    write(folder / "browser-process.json", redactor.clean({"exit_code": process.returncode,
                          "stderr": process.stderr.decode(errors="replace")}))
                browser = read(folder / "browser.json") if (folder / "browser.json").exists() else {}
                terminal = (browser.get("rounds") or [{}])[-1].get("terminal") or {}
                trace = observability.get_trace(terminal.get("trace_id")) or {}
                rows = list(model.requests) if fixed else [row for row in audit.rows() if row["case"] == name]
                write(folder / ("model-replay.json" if fixed else "sdk.json"), redactor.clean(rows))
                write(folder / "trace.json", trace)
                if barrier and barrier.log:
                    write(folder / "barrier.json", barrier.log)
                after = snapshot(db_path)
                write(folder / "sql-after.json", after)
                result = score(case, browser, trace, before, after, rows, barrier.log if barrier else {},
                               read(folder / "source.json"), read(folder / "sql-expected.json"), fixed=fixed)
                result.update(browser_seconds=round(monotonic() - started, 3),
                              model_replay_calls=len(rows) if fixed else None,
                              sdk_http_attempts=0 if fixed else len(rows),
                              path="delegated" if result["worker_count"] else "direct" if result["business_reads"]
                              else "before_business_read")
                write(folder / "result.json", redactor.clean(result))
                results[name] = {key: result[key] for key in (
                    "passed", "first_deviation", "trace_id", "worker_count", "sdk_http_attempts", "model_replay_calls",
                    "draft_calls", "path", "collaboration", "browser_seconds", "business_seconds",
                    "expected_candidate", "response")}
                write(output / "report.json", results)
                print(json.dumps({name: results[name]}, ensure_ascii=False), flush=True)
    if fixed:
        for group in ("task", "control"):
            cases = [case for case in scenarios if case["group"] == group]
            print(f"{group}: {sum(results[case['id']]['passed'] for case in cases)}/{len(cases)}", flush=True)
    return all(results[case["id"]]["passed"] for case in scenarios)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixed-results", type=Path, help="Run real GLM after a passing fixed replay.")
    args = parser.parse_args()
    prior = args.fixed_results.resolve() if args.fixed_results else None
    if prior:
        results = read(prior / "report.json")
        failed = [case["id"] for case in fixture.cases() if not results.get(case["id"], {}).get("passed")]
        if failed:
            parser.error("Fixed replay did not pass: " + ", ".join(failed))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    return run(output, prior is None, prior)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
