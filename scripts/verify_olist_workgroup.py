"""Run frozen Olist sales cases through workgroups, with real GLM and browser options."""

import argparse
import json
import os
import subprocess
from contextlib import ExitStack, closing
from pathlib import Path
from secrets import token_urlsafe
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

import httpx
from dotenv import dotenv_values

import sqlite3

from scripts.accept_task42_t1 import configure, freeze, load_source, read, serve, write
from scripts.run_real_diagnosis import Redactor
from scripts.run_task27_t5 import ENDPOINT, GLMAudit, MODEL, browser_env


class SalesAudit(GLMAudit):
    run_cap = 8

    def case_cap(self, case):
        return 1


def evaluate(output, live, disjoint_sample=None):
    freeze(output / "oracle")
    frozen = read(output / "oracle/chart-reconciliation.json")
    if disjoint_sample:
        frozen["separated_weeks"] = read(disjoint_sample)["separated_weeks"]
    cases = {}
    for name, expected in frozen.items():
        before, current = expected["baseline"], expected["current"]
        status = expected["included_statuses"]
        message = (f"Olist 这款商品 {expected['product_id']} 在{before['start_date']}到{before['end_date']}"
                   f"和{current['start_date']}到{current['end_date']}分别卖了多少件？也看看订单数。"
                   + (f"只统计 {status[0]}。" if len(status) == 1 else ""))
        cases[name] = {"message": message, "expected": expected}
    write(output / "cases.json", cases)
    mixed = frozen["selected_seller_and_multi_item"]
    with closing(sqlite3.connect(":memory:")) as raw:
        raw.row_factory = sqlite3.Row
        load_source(raw)
        lines = [dict(row) for row in raw.execute("""
            SELECT o.id order_id, o.day, o.status, i.line, i.product, i.seller,
                   i.seller=:seller AND i.product=:product AS included
            FROM raw_orders o JOIN raw_items i ON i.id=o.id
            WHERE o.status IN ('delivered','shipped')
              AND (o.day BETWEEN :b_start AND :b_end OR o.day BETWEEN :c_start AND :c_end)
              AND EXISTS (SELECT 1 FROM raw_items j WHERE j.id=o.id AND j.seller=:seller AND j.product=:product)
              AND EXISTS (SELECT 1 FROM raw_items j WHERE j.id=o.id AND j.seller<>:seller)
            ORDER BY o.day, o.id, CAST(i.line AS INT)
        """, {"seller": mixed["shop_id"], "product": mixed["product_id"],
               "b_start": mixed["baseline"]["start_date"], "b_end": mixed["baseline"]["end_date"],
               "c_start": mixed["current"]["start_date"], "c_end": mixed["current"]["end_date"]})]
        assert lines and any(row["included"] for row in lines) and any(row["seller"] != mixed["shop_id"] for row in lines)
    write(output / "mixed-seller-lines.json", lines)
    configure(output / "olist.sqlite3", "glm" if live else "mock")
    if live:
        os.environ["GLM_MODEL"] = MODEL
        if not os.environ.get("GLM_API_KEY"):
            os.environ["GLM_API_KEY"] = dotenv_values(".env").get("GLM_API_KEY") or ""
        if not os.environ["GLM_API_KEY"]:
            raise RuntimeError("GLM_API_KEY is required for --live")

    from backend import observability
    from backend.agent import llm_provider
    from backend.database import database, orders
    from scripts import orders_import
    from tests.test_olist_workgroup import OlistModel

    database.init_db()
    username, password = "olist-group-operator", token_urlsafe(22)
    with closing(database.get_db()) as conn, conn:
        conn.execute("INSERT INTO users (username,password) VALUES (?,?)", (username, database.hash_password(password)))
        orders_import.provision(conn, orders.OLIST, orders_import.SELLER, username)
        imported = orders_import.import_olist(conn, orders_import.SOURCE_DIR)
    write(output / "import.json", imported)

    provider = llm_provider.ProviderFactory.create() if live else OlistModel()
    audit = None
    if live:
        if provider._model != MODEL or str(provider._client.base_url).rstrip("/") != ENDPOINT:
            raise RuntimeError("Expected the configured official GLM endpoint")
        redactor = Redactor([password, os.environ["JWT_SECRET"], os.environ["GLM_API_KEY"]])
        audit = SalesAudit(output / "glm-sdk-ledger.sqlite3", redactor, uuid4().hex, 8)
        audit.enabled = True
        hooks = provider._client._client.event_hooks
        hooks["request"].append(audit.request)
        hooks["response"].append(audit.response)

    reports = {}
    with ExitStack() as stack:
        if audit:
            stack.callback(provider._client.close)
            stack.enter_context(patch.object(llm_provider, "add_trace_step", audit.trace_model(llm_provider.add_trace_step)))
        stack.enter_context(patch.object(llm_provider.ProviderFactory, "create", return_value=provider))
        stack.enter_context(patch("backend.memory.conversation_memory.store_conversation"))
        stack.enter_context(patch.object(observability, "TRACE_LOG_FILE", output / "traces.jsonl"))
        from fast_server import app

        with serve(app) as origin, httpx.Client(base_url=origin, timeout=55, trust_env=False) as client:
            login = client.post("/api/login", json={"username": username, "password": password})
            login.raise_for_status()
            account = login.json()["data"]
            headers = {"Authorization": "Bearer " + account["token"]}
            for name, case in cases.items():
                if audit:
                    audit.case = name
                else:
                    provider.arguments = {"product_id": case["expected"]["product_id"]}
                started = monotonic()
                conversation = client.post("/api/chat/conversations", headers=headers, json={"title": name})
                conversation.raise_for_status()
                conversation_id = conversation.json()["data"]["conversation_id"]
                response = client.post("/api/chat/stream", headers=headers, json={
                    "conversation_id": conversation_id, "message": case["message"],
                })
                response.raise_for_status()
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                group = f"/api/chat/conversations/{conversation_id}"
                evidence = client.get(f"{group}/runs/{events[-1]['run_id']}/evidence", headers=headers)
                data = evidence.json().get("data") or {}
                passed = (data.get("metrics") == case["expected"] and data.get("kind") == "olist_sales_v1"
                          and [row["type"] for row in events] == ["thinking", "text", "done"])
                reports[name] = {"passed": passed, "events": events, "evidence": data,
                                 "conversation_id": conversation_id, "seconds": round(monotonic() - started, 3)}
            own = reports["selected_seller_and_multi_item"]
            if audit:
                audit.case = "same_product_followup"
            else:
                provider.arguments = {"product_id": mixed["product_id"]}
            before, current = mixed["baseline"], mixed["current"]
            followup = (f"这款商品在{before['start_date']}到{before['end_date']}和"
                        f"{current['start_date']}到{current['end_date']}分别卖了多少件？")
            conversation_id = own["conversation_id"]
            response = client.post("/api/chat/stream", headers=headers,
                                   json={"conversation_id": conversation_id, "message": followup})
            response.raise_for_status()
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            evidence = client.get(f"/api/chat/conversations/{conversation_id}/runs/{events[-1]['run_id']}/evidence",
                                  headers=headers).json().get("data") or {}
            reports["same_product_followup"] = {
                "passed": evidence.get("metrics") == mixed and events[-1]["type"] == "done", "message": followup,
                "events": events, "evidence": evidence, "conversation_id": conversation_id}
            for order_id in {line["order_id"] for line in lines}:
                response = client.get(f"/api/orders/shops/olist-v2/{mixed['shop_id']}/orders/{order_id}", headers=headers)
                response.raise_for_status()
                actual = response.json()["data"]["items"]
                expected = [line for line in lines if line["order_id"] == order_id and line["seller"] == mixed["shop_id"]]
                assert [(item["item_id"], item["product_id"], item["quantity"]) for item in actual] == [
                    (line["line"], line["product"], 1) for line in expected]
            own["mixed_order_line_reconciliation"] = True
            write(output / "cases-result.json", reports)
            if audit:
                write(output / "sdk.json", audit.rows())
            chosen = reports["selected_seller_and_multi_item"]
            if chosen["passed"]:
                env = browser_env()
                env["AD_OLIST_GROUP_SESSION"] = json.dumps(account)
                if disjoint_sample:
                    env["AD_OLIST_DISJOINT_SAMPLE"] = "1"
                result = subprocess.run(["node", "tests/olist_workgroup_browser.cjs", origin, str(output.resolve())],
                                        env=env, check=False, timeout=100)
                browser_passed = result.returncode == 0
            else:
                browser_passed = False

    trace_rows = [json.loads(line) for line in (output / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    trace_by_id = {row["trace_id"]: row for row in trace_rows}
    for row in reports.values():
        if row["passed"]:
            trace = trace_by_id[row["events"][-1]["trace_id"]]
            row["passed"] = sum(step["stage"] == "olist_sales" and step["action"] == "accepted"
                                and step.get("runtime_call_id") == row["evidence"]["runtime_call_id"]
                                for step in trace["steps"]) == 1
    write(output / "cases-result.json", reports)
    sdk = audit.rows() if audit else []
    summary = {"passed": sum(row["passed"] for row in reports.values()), "total": len(reports),
               "browser_passed": browser_passed, "sdk_attempts": len(sdk)}
    write(output / "summary.json", summary)
    print(json.dumps(summary))
    return int(summary["passed"] != summary["total"] or not browser_passed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--disjoint-sample", type=Path, help="Frozen browser-samples.json from the batch audit")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    return evaluate(args.output_dir, args.live, args.disjoint_sample)


if __name__ == "__main__":
    raise SystemExit(main())
