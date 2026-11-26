"""Replay AD-053 through loopback HTTP and optionally verify saved details in Edge."""

import argparse
import csv
import io
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch

import httpx

from scripts.accept_search_term_full_batch import BASE, freeze, seed_batch, sha
from scripts.accept_search_terms import read, serve, write


METRICS = ("clicks", "impressions", "orders", "spend_cents", "sales_cents")
TABLES = ("ad_plans", "listing_test_shops", "listing_test_products", "listing_test_facts", "listing_test_sales",
          "listing_test_required_facts", "listing_change_proposals", "listing_change_receipts",
          "search_term_amazon_membership", "search_term_ad_groups", "search_term_amazon_row_links")


def oracle(conn, batch, report):
    source = list(csv.DictReader(io.StringIO(batch["report_csv"])))[6]
    params = {"import_id": report["import_id"], "sha256": batch["report_sha256"],
              "campaign": source["Campaign ID"], "ad_group": source["Ad Group ID"],
              "term": source["Customer Search Term"], "start_date": report["start_date"],
              "end_date": report["end_date"]}
    sql = Path(__file__).with_suffix(".sql").read_text("utf-8")
    rows = [dict(row) for row in conn.execute(sql + " ORDER BY line", params)]
    sums = ", ".join(f"sum({metric}) AS {metric}" for metric in METRICS)
    metrics = dict(conn.execute(f"""
        SELECT {sums}, round(100.0*sum(clicks)/sum(impressions), 2) AS ctr_pct,
               round(100.0*sum(orders)/sum(clicks), 2) AS cvr_pct,
               round(0.01*sum(spend_cents)/sum(clicks), 2) AS cpc_usd,
               round(100.0*sum(spend_cents)/sum(sales_cents), 2) AS acos_pct
        FROM ({sql})
    """, params).fetchone())
    return {"scope": params, "rows": rows, "metrics": metrics}


def configuration(conn):
    return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES}


def receipt(response):
    return {"status": response.status_code, "body": response.json()}


def replay(client, database, diagnosis, root, frozen, scenario, baseline):
    folder = root / scenario
    folder.mkdir()
    batch = frozen["batches"]["rinse-24"]
    member = frozen["membership"]
    with closing(database.get_db()) as conn, conn:
        seed_batch(conn, folder, frozen, batch)
        conn.execute("INSERT INTO search_term_amazon_row_links VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (member["shop"], member["marketplace"], batch["report_sha256"], "", 26,
                      "batch-rinse-24", "rinse-24-mixed", member["control_sku"], sha(b"probe-line-26"), "synthetic:26"))
    title = "AD-053 " + scenario
    created = client.post("/api/chat/conversations", json={"title": title})
    assert created.status_code == 201, created.text
    group = created.json()["data"]["conversation_id"]
    api = f"/api/chat/conversations/{group}/search-terms"
    raw = batch["report_csv"].encode()
    assert sha(raw) == batch["report_sha256"]
    upload = client.post(api + "/reports", params={"filename": "rinse.csv"}, content=raw)
    assert upload.status_code == 201, upload.text
    report = upload.json()["report"]
    with closing(sqlite3.connect(database.DB_PATH)) as sql:
        sql.row_factory = sqlite3.Row
        before = oracle(sql, batch, report)
        assert [(r["line"], r["clicks"]) for r in before["rows"]] == [(8, 10), (26, 11)]
        assert before["metrics"]["clicks"] == 21
        config_before = configuration(sql)
        point = {}

        def model(message, manifest, product, items, trace_id, **options):
            item = next(i for i in items if i["row_locations"] == [{"sheet": None, "line": 8}])
            assert (item["attribution"], item["metrics"]["clicks"]) == ("product", 10)
            point.update(id=item["id"], term=item["term"])
            with closing(database.get_db()) as writer, writer:
                if scenario != "product":
                    writer.execute("DELETE FROM search_term_amazon_row_links WHERE report_sha256=? AND line=26",
                                   (batch["report_sha256"],))
                if scenario == "missing":
                    writer.execute("""
                        UPDATE search_term_report_imports
                        SET original_rows=(SELECT json_group_array(value) FROM json_each(original_rows)
                                           WHERE json_extract(value, '$.line') != 26)
                        WHERE import_id=?
                    """, (report["import_id"],))
            with closing(sqlite3.connect(database.DB_PATH)) as audit:
                audit.row_factory = sqlite3.Row
                point["configuration_after_injection"] = configuration(audit)
            fixed = [dict(i, action="observe", reason="固定回放", citations=[]) for i in items]
            if scenario != "product":
                fixed[item["id"] - 1].update(initial_draft="Fixed model draft", review={"status": "pending"})
            return {"kind": diagnosis.reports.KIND, "report": manifest, "product": product,
                    "rules": diagnosis.CLAIM_RULES, "items": fixed}

        with (patch.object(diagnosis, "infer", side_effect=model) as fixed_model,
              patch.object(diagnosis, "create_completion", side_effect=AssertionError("Unexpected external call")) as sdk):
            response = client.post(api + "/runs", json={"import_id": report["import_id"], "sku": batch["sku"],
                                                       "message": frozen["question"]})
        assert fixed_model.call_count == 1 and sdk.call_count == 0
        post = receipt(response)
        assert post["status"] == 200, post
        run_id = post["body"]["run_id"]
        path = f"{api}/runs/{run_id}/items/{point['id']}"
        get = receipt(client.get(path))
        assert get["status"] == 200, get
        detail = get["body"]
        item = detail["item"]
        after = oracle(sql, batch, report)
        config_after = configuration(sql)
        expected_config = {**config_before}
        if scenario != "product":
            expected_config["search_term_amazon_row_links"] = [
                link for link in config_before["search_term_amazon_row_links"] if link["line"] != 26]
        assert config_after == point["configuration_after_injection"] == expected_config
        assert [r["line"] for r in after["rows"]] == ([8] if scenario == "missing" else [8, 26])
        history = receipt(client.get(f"/api/chat/conversations/{group}/history"))
        actions = {}
        if scenario != "product":
            actions = {"copy": receipt(client.get(path + "/draft/copy?revision=1")),
                       "edit": receipt(client.put(path + "/draft", json={"title": batch["title"], "revision": 1}))}
            assert all(response["status"] == 404 for response in actions.values())
            assert item["action"] == "incomplete" and detail["draft"] is None and "review" not in item
        saved = sql.execute("""
            SELECT j.value FROM chat_group_entries AS e, json_each(e.details, '$.items') AS j
            WHERE e.run_id=? AND e.kind='assistant' AND json_extract(j.value, '$.id')=?
        """, (run_id, point["id"])).fetchone()
        saved = json.loads(saved[0]) if saved else {}
        drafts = sql.execute("SELECT count(*) FROM search_term_title_drafts WHERE run_id=?", (run_id,)).fetchone()[0]
        reviews = int(saved.get("review", {}).get("status") == "pending")
        assert drafts == reviews == 0 and "initial_draft" not in saved and "review" not in saved
        assert saved["attribution"] == item["attribution"] and saved["metrics"] == item["metrics"]
        assert saved["row_locations"] == item["row_locations"]
        write(folder / "http.json", {"post": post, "get": get, "history_status": history["status"], **actions})
        write(folder / "independent-sql.json", {"before": before, "after": after, "draft_rows": drafts,
                                               "review_pending": reviews,
                                               "configuration_before": config_before, "configuration_after": config_after,
                                               "configuration_after_injection": point["configuration_after_injection"],
                                               "persisted_item": saved})
        lines = [row["line"] for row in detail["rows"]]
        if scenario == "product" or baseline:
            assert lines == [8] and item["metrics"]["clicks"] == 10
            assert item["attribution"] == ("product" if scenario == "product" else "ad_group")
        elif scenario == "complete":
            assert lines == [8, 26] and item["metrics"] == after["metrics"] and item["attribution"] == "ad_group"
        else:
            assert lines == [] and item["metrics"] is None and item["attribution"] == "unavailable"
            assert "第 26 行" in item["reason"]
            assert item["historical"]["sku"] == batch["sku"] and item["historical"]["metrics"]["clicks"] == 10
            assert [r["line"] for r in detail["historical_rows"]] == [8]
        assert history["status"] == 200 and client.get(path).json() == detail
        return {"group": title, "path": path, "id": point["id"], "term": point["term"],
                "action": item["action"], "attribution": item["attribution"], "lines": lines,
                "clicks": item["metrics"]["clicks"] if item["metrics"] else None,
                "fixed_model_calls": fixed_model.call_count, "real_sdk_requests": sdk.call_count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--baseline", action="store_true")
    modes.add_argument("--edge", action="store_true")
    args = parser.parse_args()
    root = args.output_dir.resolve()
    root.mkdir(parents=True)
    os.environ.update(LLM_PROVIDER="mock", JWT_SECRET=token_urlsafe(48), DIAGNOSIS_MYSQL_ENABLED="false",
                      DIAGNOSIS_SANDBOX_DB_PATH="", AD_OFFLINE_ACCEPTANCE="1", RAG_DB_PATH=str(root / "rag"))
    from backend import observability
    from backend.agent import search_term_diagnosis as diagnosis
    from backend.database import database
    from fast_server import app

    database.DB_PATH = str(root / "sandbox.sqlite3")
    database.init_db()
    observability.TRACE_LOG_FILE = root / "trace.jsonl"
    observability.ERROR_LOG_FILE = root / "errors.jsonl"
    observability.FUNC_LOG_FILE = root / "functions.jsonl"
    frozen = freeze(read(BASE.with_suffix(".json")))
    password = token_urlsafe(20)
    with closing(database.get_db()) as conn, conn:
        conn.execute("INSERT INTO users (id, username, password) VALUES (17, ?, ?)",
                     ("ad053-replay", database.hash_password(password)))
    with serve(app) as origin, httpx.Client(base_url=origin, trust_env=False) as client:
        login = client.post("/api/login", json={"username": "ad053-replay", "password": password})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = "Bearer " + login.json()["data"]["token"]
        scenarios = ("product", "complete") if args.baseline else ("product", "complete", "missing")
        outcomes = {}
        for scenario in scenarios:
            outcomes[scenario] = replay(client, database, diagnosis, root, frozen, scenario, args.baseline)
            print(scenario, outcomes[scenario], flush=True)
        write(root / "result.json", {"passed": True, "scenarios": outcomes})
        if args.edge:
            environment = {**os.environ, "AD_BROWSER_CHANNEL": "msedge", "AD053_PASSWORD": password}
            subprocess.run(["node", "tests/ad053_edge.cjs", origin, str(root)], env=environment, timeout=120, check=True)


if __name__ == "__main__":
    main()
