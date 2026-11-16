"""Freeze a raw Olist oracle, then replay the existing workgroup entry."""

import argparse
import json
import re
import sqlite3
from collections import Counter
from contextlib import ExitStack, closing
from datetime import date
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from time import perf_counter
from unittest.mock import patch

from scripts.accept_task42_t1 import configure, read
from scripts.audit_task42_olist_batch import SELLER, SOURCE, STATUSES, expected_window, freeze_source, source_rows, write


SQL = Path(__file__).with_name("task42_t4_olist.sql")
A = "1600dcf1cea8c0c83702e07b577ab231"
B = "e06f99e79036d681effa3de75989bf22"
C = "5d7c23067ed3fc8c6e699b9373d5890b"
FOREIGN = "e84997d75938765ef99930f908ceedf4"
FOREIGN_SELLER = "cd6efc47efaabf134f8bdb654e10b4f1"
SHARED_ORDER = "5aa89bb7eb1a8dca4d70fcd37878c8a4"
FIRST = ("2018-05-28", "2018-06-24", "2018-06-25", "2018-07-22")
SECOND = ("2018-07-16", "2018-08-12", "2018-08-13", "2018-09-09")


def question(products, dates, detail):
    first, last, next_first, next_last = dates
    prefix = f"Olist 店铺 {SELLER} 的商品 {'、'.join(products)}，"
    comparison = f"比较 {first} 至 {last} 和 {next_first} 至 {next_last} 的销量和去重订单数。"
    selection = "按销量减少的件数排名，找到唯一降得最多的商品，再核查这个商品在这两个日期窗的订单明细。"
    return prefix + comparison + (selection if detail else "")


def freeze(output, source):
    catalog, _, _, _, verified = freeze_source(source)
    raw_orders = source_rows(source, "olist_orders_dataset.csv", verified, {})
    raw_items = source_rows(source, "olist_order_items_dataset.csv", verified, {})
    sql = SQL.read_text(encoding="utf-8")
    specs = [
        ("single", (A,), FIRST, False),
        ("complete", (A, B, C), FIRST, True),
        ("changed-dates", (A, B, C), SECOND, True),
        ("missing-candidate", (A, B, C, FOREIGN), SECOND, True),
        ("minimal-two", (A, B), FIRST, True),
    ]
    cases = {}
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE raw_orders(id TEXT PRIMARY KEY, day TEXT, status TEXT);
            CREATE TABLE raw_items(id TEXT, line TEXT, product TEXT, seller TEXT, PRIMARY KEY(id,line));
            CREATE INDEX raw_items_seller_product ON raw_items(seller, product);
            CREATE TABLE candidates(position INTEGER, product TEXT PRIMARY KEY);
            CREATE TABLE request(seller TEXT, baseline_start TEXT, baseline_end TEXT,
                                 current_start TEXT, current_end TEXT, wants_detail INTEGER);
        """)
        conn.executemany("INSERT INTO raw_orders VALUES (?, ?, ?)", (
            (r["order_id"], r["order_purchase_timestamp"].split()[0], r["order_status"]) for r in raw_orders
        ))
        conn.executemany("INSERT INTO raw_items VALUES (?, ?, ?, ?)", (
            (r["order_id"], r["order_item_id"], r["product_id"], r["seller_id"]) for r in raw_items
        ))
        conn.executescript(sql)
        for name, products, dates, detail in specs:
            conn.execute("DELETE FROM candidates")
            conn.execute("DELETE FROM request")
            conn.executemany("INSERT INTO candidates VALUES (?, ?)", enumerate(products))
            conn.execute("INSERT INTO request VALUES (?, ?, ?, ?, ?, ?)", (SELLER, *dates, detail))
            ranking = [dict(row) for row in conn.execute("SELECT * FROM ranked_candidates ORDER BY rank, position")]
            missing = [row["product"] for row in conn.execute(
                "SELECT product FROM candidate_totals WHERE NOT verified ORDER BY position"
            )]
            selected = conn.execute("SELECT product FROM selected_candidate").fetchone()
            items = [dict(row) for row in conn.execute(
                "SELECT * FROM selected_order_items ORDER BY day, order_id, CAST(item_id AS INTEGER)"
            )]
            candidate_orders = [row[0] for row in conn.execute(
                "SELECT DISTINCT order_id FROM matching_lines ORDER BY order_id"
            )]
            windows = {}
            if missing != [product for product in products if product not in catalog]:
                raise ValueError(f"{name}: raw SQL catalog and CSV audit disagree")
            for row in ranking:
                product = row["product"]
                windows[product] = {}
                for index, window in ((0, "baseline"), (2, "current")):
                    start, end = map(date.fromisoformat, dates[index:index + 2])
                    expected = expected_window(catalog[product], start, (end - start).days + 1, STATUSES[0])
                    if expected["totals"] != {metric: row[f"{window}_{metric}"] for metric in ("units", "orders")}:
                        raise ValueError(f"{name}: raw SQL and CSV totals disagree for {product}")
                    windows[product][window] = expected
            cases[name] = {
                "message": question(products, dates, detail), "products": products, "dates": dates,
                "ranking": ranking if detail else [], "missing": missing, "windows": windows,
                "selected_product": selected[0] if selected else None, "order_items": items,
                "order_count": len({row["order_id"] for row in items}),
                "candidate_order_ids": candidate_orders,
            }

    # Seed a real foreign row under its own, unauthorized shop for the mixed-order control.
    foreign_item, = [r for r in raw_items if r["order_id"] == SHARED_ORDER and r["product_id"] == FOREIGN
                     and r["seller_id"] == FOREIGN_SELLER]
    foreign_order, = [r for r in raw_orders if r["order_id"] == SHARED_ORDER]
    if not any(row["order_id"] == SHARED_ORDER for row in cases["changed-dates"]["order_items"]):
        raise ValueError("Cross-shop fixture must share an order with the changed-date winner")
    if [cases[n]["selected_product"] for n in ("complete", "changed-dates", "missing-candidate")] != [A, B, None]:
        raise ValueError("Candidate selection control changed")
    if cases["missing-candidate"]["missing"] != [FOREIGN]:
        raise ValueError("Missing-candidate control changed")

    output.mkdir(parents=True, exist_ok=True)
    path = output / "frozen.json"
    if path.exists():
        raise FileExistsError(f"Use a new output directory: {path}")
    write(path, {
        "seller": SELLER, "sources": verified, "sql_sha256": sha256(sql.encode()).hexdigest(),
        "rules": {
            "windows": "Inclusive Olist purchase calendar days, equal length and disjoint.",
            "orders": "Distinct order IDs per product and window. One raw item line is one unit.",
            "statuses": STATUSES[0],
            "rank": "Baseline units minus current units, descending. Relative decline is descriptive.",
            "zero_baseline": "Verified product with no lines has zero sales. Relative decline is undefined at zero baseline.",
            "ties": "Equal declines share rank, no tie-break for winner. No details for ties or nonpositive maximum.",
            "missing": "Identify every unverifiable candidate. Rank verified candidates as provisional. No winner, detail read or disclosure.",
            "details": "Read only the unique positive winner. Both windows, scoped seller and winner item lines only.",
            "live_gate": "Run paired real GLM only after fixed single, multi, changed-date and missing business checks pass.",
        },
        "fixture": {"foreign_item": foreign_item, "foreign_order": {
            key: foreign_order[key] for key in ("order_id", "order_status", "order_purchase_timestamp")
        }},
        "cases": cases,
    })
    print(f"Frozen {len(cases)} prompts: {sha256(path.read_bytes()).hexdigest()}")


def collect_case(client, headers, name, case, audit):
    group = client.post("/api/chat/conversations", headers=headers, json={"title": f"T4 {name}"})
    group.raise_for_status()
    conversation_id = group.json()["data"]["conversation_id"]
    url = f"/api/chat/conversations/{conversation_id}"
    started = perf_counter()
    response = client.post("/api/chat/stream", headers=headers, json={
        "conversation_id": conversation_id, "message": case["message"],
    })
    seconds = perf_counter() - started
    response.raise_for_status()
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    end = events[-1]
    evidence = client.get(f"{url}/runs/{end['run_id']}/evidence", headers=headers)
    history = client.get(url + "/history", headers=headers)
    history.raise_for_status()
    return {**audit, "message": case["message"], "conversation_id": conversation_id, "run_id": end["run_id"],
            "trace_id": end["trace_id"], "seconds": round(seconds, 6), "events": events,
            "evidence_status": evidence.status_code, "evidence": evidence.json().get("data"),
            "history": history.json()["data"]}


def score(name, case, result, trace):
    answer = "".join(event["content"] for event in result["events"] if event["type"] == "text")
    evidence = result["evidence"] or {}
    scoped_queries = [sql for sql in result["order_sql"] if re.search(r"\bshop_order(?:s|_items)\b", sql, re.I)]
    # Catalog, sales and order-detail statements must all carry the authorized seller.
    unscoped = [sql for sql in scoped_queries if not re.search(rf"\bshop_id\s*=\s*'{SELLER}'", sql, re.I)]
    detail_queries = [sql for sql in scoped_queries if not re.search(r"\bGROUP BY day\b|\bSELECT 1\b", sql, re.I)]
    detail_ids = set(case["candidate_order_ids"]) | {SHARED_ORDER}
    published = answer + str(evidence) + str(result["history"])
    disclosed = sorted(order_id for order_id in detail_ids if order_id in published)
    missing = case["missing"]
    checks = {
        "sse_completed": [event["type"] for event in result["events"]] == ["thinking", "text", "done"]
        and trace["status"] == "completed",
        "no_cross_shop_sql": not unscoped,
    }
    output_metrics = evidence.get("metrics") or {}
    receipts = [step for step in trace["steps"] if step["stage"] == "olist_sales" and step["action"] == "accepted"]
    if name == "single":
        actual_windows = {w: output_metrics.get(w) for w in ("baseline", "current")}
        expected_windows = case["windows"][A]
        checks.update(
            single_matches_sql=actual_windows == expected_windows and all(
                f"{window['totals']['units']} 件、{window['totals']['orders']} 单" in answer
                for window in expected_windows.values()
            ),
            single_scoped_and_committed=evidence.get("kind") == "olist_sales_v1"
            and output_metrics.get("source") == "olist-v2" and output_metrics.get("shop_id") == SELLER
            and output_metrics.get("product_id") == A and output_metrics.get("included_statuses") == list(STATUSES[0])
            and [row["content"] for row in result["history"]["entries"] if row["kind"] == "assistant"] == [answer]
            and len(receipts) == 1 and receipts[0]["runtime_call_id"] == evidence.get("runtime_call_id"),
        )
    if len(case["products"]) > 1:
        checks.update(multi_target_resolved=bool(result["targets"] and result["targets"][0]),
                      ranked_all_verified_candidates=bool(receipts) and evidence.get("ranking") == case["ranking"])
    if case["selected_product"]:
        checks.update(winner_matches=evidence.get("selected_product") == case["selected_product"],
                      winner_detail_read=bool(detail_queries),
                      winner_orders_reported=disclosed == sorted({row["order_id"] for row in case["order_items"]}))
    else:
        checks["no_detail_read_or_disclosure"] = not detail_queries and not disclosed
    if missing:
        checks.update(missing_candidate_explicit=all(product in answer for product in missing),
                      no_selection=evidence.get("selected_product") is None)
    worker = [step for step in trace["steps"] if step["stage"] == "worker"]
    return {"checks": checks, "passed": all(checks.values()), "answer": answer,
            "first_failure": ("olist_sales._target rejected multiple IDs before window validation and model scheduling"
                              if len(case["products"]) > 1 and result["targets"] == [None] else None),
            "cross_shop_queries": unscoped, "detail_queries": detail_queries, "disclosed_order_ids": disclosed,
            "seller_order_sql_count": len(scoped_queries), "order_sql_counts": dict(Counter(
                "access" if "order_shop_access" in sql else
                "sales" if re.search(r"\bGROUP BY day\b", sql, re.I) else
                "detail" if sql in detail_queries else "catalog" for sql in result["order_sql"]
            )),
            "worker_created": sum(step["action"] == "created" for step in worker),
            "worker_reclaimed": sum(step["action"] == "reclaimed" for step in worker)}


def snapshot(conn):
    digest = sha256()
    for table, primary_key in (
        ("order_shops", "source, shop_id"),
        ("order_shop_access", "source, shop_id, user_id"),
        ("shop_orders", "source, shop_id, order_id"),
        ("shop_order_items", "source, shop_id, order_id, item_id"),
        ("order_ingestion_runs", "id"),
    ):
        digest.update(table.encode())
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {primary_key}"):
            digest.update(json.dumps(tuple(row)).encode() + b"\n")
    return digest.hexdigest()


def fixed(output, source):
    from fastapi.testclient import TestClient

    path = output / "frozen.json"
    frozen = read(path)
    if sha256(SQL.read_text(encoding="utf-8").encode()).hexdigest() != frozen["sql_sha256"]:
        raise ValueError("Replayed SQL does not match the frozen oracle")
    _, _, _, _, sources = freeze_source(source)
    if sources != frozen["sources"]:
        raise ValueError("Replay source does not match the frozen oracle")
    run = output / "fixed"
    run.mkdir(exist_ok=False)
    configure(run / "olist.sqlite3")

    from backend import observability
    from backend.agent import olist_sales
    from backend.agent.llm_provider import ProviderFactory
    from backend.database import database, orders
    from scripts import orders_import
    from tests.test_olist_workgroup import OlistModel

    database.init_db()
    username, password = "olist-t4-operator", token_urlsafe(24)
    fixture = frozen["fixture"]
    order, item = fixture["foreign_order"], fixture["foreign_item"]
    with closing(database.get_db()) as conn, conn:
        conn.execute("INSERT INTO users (username,password) VALUES (?,?)", (username, database.hash_password(password)))
        orders_import.provision(conn, orders.OLIST, SELLER, username)
        imported = orders_import.import_olist(conn, source)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO order_shops VALUES (?, ?)", (orders.OLIST, item["seller_id"]))
        orders.write_orders(conn, orders.OLIST, item["seller_id"], [orders.Order(
            order["order_id"], order["order_status"], order["order_purchase_timestamp"], None,
            None, None, True, (orders.Item(item["order_item_id"], None, item["product_id"], 1, None),),
        )], synced_at="2026-11-13T00:00:00Z")
        before = snapshot(conn)

    model = OlistModel()
    model.arguments = {"product_id": A}
    active = None
    connect = sqlite3.connect

    def record_sql(statement):
        if active is not None and re.search(r"\b(?:shop_orders|shop_order_items|order_shop_access)\b", statement, re.I):
            active["order_sql"].append(statement)

    def instrumented_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(record_sql)
        return conn

    def observe(function, key, encode=lambda value: value):
        def observed(*args, **kwargs):
            value = function(*args, **kwargs)
            active[key].append(encode(value))
            return value
        return observed

    results = {}
    with ExitStack() as stack:
        stack.enter_context(patch.object(ProviderFactory, "create", return_value=model))
        stack.enter_context(patch("backend.memory.conversation_memory.store_conversation"))
        stack.enter_context(patch.object(observability, "TRACE_LOG_FILE", run / "traces.jsonl"))
        stack.enter_context(patch.object(observability, "FUNC_LOG_FILE", run / "functions.jsonl"))
        stack.enter_context(patch.object(observability, "ERROR_LOG_FILE", run / "errors.jsonl"))
        from fast_server import app

        client = stack.enter_context(TestClient(app))
        stack.enter_context(patch.object(sqlite3, "connect", instrumented_connect))
        stack.enter_context(patch.object(olist_sales, "_target", observe(olist_sales._target, "targets")))
        stack.enter_context(patch.object(olist_sales, "_windows", observe(
            olist_sales._windows, "windows", lambda pair: [[day.isoformat() for day in window] for window in pair]
            if pair else None
        )))
        stack.enter_context(patch.object(model.chat.completions, "create", observe(
            model.chat.completions.create, "model_tool_calls", lambda reply: reply.choices[0].message.tool_calls
        )))
        login = client.post("/api/login", json={"username": username, "password": password})
        login.raise_for_status()
        headers = {"Authorization": "Bearer " + login.json()["data"]["token"]}
        for name, case in frozen["cases"].items():
            active = {key: [] for key in ("targets", "windows", "order_sql", "model_tool_calls")}
            active["date_tokens"] = olist_sales.DATE.findall(case["message"])
            active["id_tokens"] = olist_sales.IDENTIFIER.findall(case["message"])
            first_request = len(model.requests)
            result = collect_case(client, headers, name, case, active)
            active = None
            result["model_requests"] = model.requests[first_request:]
            result["available_tools"] = [[tool["function"]["name"] for tool in req.get("tools", [])]
                                         for req in result["model_requests"]]
            result["trace"] = observability.get_trace(result["trace_id"])
            result.update(score(name, case, result, result["trace"]))
            write(run / f"{name}.json", result)
            results[name] = result

    with closing(database.get_db(read_only=True)) as conn:
        after = snapshot(conn)
    core = {name: row for name, row in results.items() if name != "minimal-two"}
    live_ready = before == after and all(row["passed"] for row in core.values())
    summary = {
        "frozen_sha256": sha256(path.read_bytes()).hexdigest(),
        "isolation": {"import_receipt": imported, "foreign_fixture_rows": 1, "foreign_shop_grants": 0,
                      "business_data_before_sha256": before, "business_data_after_sha256": after,
                      "unique_conversations": len({row["conversation_id"] for row in results.values()})},
        "core_passed": sum(row["passed"] for row in core.values()), "core_total": len(core),
        "fixed_model_requests": sum(len(row["model_requests"]) for row in core.values()),
        "actual_sdk_attempts": 0, "sdk_tokens": 0,
        "live_gate_passed": live_ready,
        "live_comparison": {"status": "eligible" if live_ready else "not_run_fixed_gate_failed",
                            "attempt_cap": 48, "attempts_used": 0},
        "cases": {name: {key: row[key] for key in (
            "passed", "trace_id", "first_failure", "seconds", "order_sql_counts", "worker_created", "worker_reclaimed"
        )} for name, row in results.items()},
    }
    write(run / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if live_ready and results["minimal-two"]["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("freeze", "fixed"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=SOURCE)
    args = parser.parse_args()
    if args.phase == "freeze":
        freeze(args.output_dir, args.source_dir)
        return 0
    return fixed(args.output_dir, args.source_dir)


if __name__ == "__main__":
    raise SystemExit(main())
