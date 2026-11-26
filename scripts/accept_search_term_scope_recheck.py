"""Replay permission loss, late replies, and report regrouping in isolated SQLite."""

import argparse
import csv
import io
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts.accept_search_term_full_batch import (
    GROUPS, SDKLedger, change_scope, response_json, seed_batch, sql_oracle,
)
from scripts.accept_search_terms import collect_trace, read, sha, write


REGROUP_SQL = """
WITH resolved AS (
    SELECT r.line, lower(r.term) AS term,
           coalesce(nullif(r.campaign_id, ''), (
               SELECT CASE WHEN count(DISTINCT campaign_id) = 1 THEN min(campaign_id) END
               FROM search_term_amazon_membership m
               WHERE m.campaign_name = r.campaign AND m.ad_group_name = r.ad_group
           ), lower(r.campaign)) AS campaign,
           coalesce(nullif(r.ad_group_id, ''), (
               SELECT CASE WHEN count(DISTINCT ad_group_id) = 1 THEN min(ad_group_id) END
               FROM search_term_amazon_membership m
               WHERE m.campaign_name = r.campaign AND m.ad_group_name = r.ad_group
           ), lower(r.ad_group)) AS ad_group
    FROM report_lines r
)
SELECT row_number() OVER (ORDER BY min(line)) AS position, campaign, ad_group, term,
       group_concat(line ORDER BY line) AS physical_lines
FROM resolved
GROUP BY campaign, ad_group, term
ORDER BY min(line)
"""


def regroup_oracle(sandbox, batch):
    fields = ("Campaign Name", "Ad Group Name", "Campaign ID", "Ad Group ID", "Customer Search Term")
    physical_rows = csv.DictReader(io.StringIO(batch["report_csv"]))
    with closing(sqlite3.connect(sandbox / "sandbox.sqlite3")) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TEMP TABLE report_lines(line INT, campaign TEXT, ad_group TEXT,"
                     " campaign_id TEXT, ad_group_id TEXT, term TEXT)")
        conn.executemany("INSERT INTO report_lines VALUES (?,?,?,?,?,?)",
                         [(i, *(row[key] for key in fields)) for i, row in enumerate(physical_rows, 2)])
        return [dict(row) for row in conn.execute(REGROUP_SQL)]


def report_variant(batch, cross_page):
    csv_rows = csv.DictReader(io.StringIO(batch["report_csv"]))
    rows = list(csv_rows)
    rows[5].update({"Campaign ID": "", "Ad Group ID": "", "Ad Group Name": "verified"})
    if cross_page:
        rows.append({**rows[0], "Customer Search Term": "scope page boundary sentinel", "Ad Group Name": "low",
                     "Ad Group ID": "rinse-24-low"})
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, csv_rows.fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return {**batch, "report_csv": buffer.getvalue(), "report_sha256": sha(buffer.getvalue().encode())}


def open_report(client, batch, name):
    group = response_json(client.post(GROUPS, json={"title": name}), 201)["data"]["conversation_id"]
    api = f"{GROUPS}/{group}/search-terms"
    report = response_json(client.post(api + "/reports", params={"filename": "rinse.csv"},
                                       headers={"Content-Type": "text/csv"},
                                       content=batch["report_csv"].encode()), 201)["report"]
    return group, api, report


def diagnose(client, api, report, batch, frozen):
    return client.post(api + "/runs", json={"import_id": report["import_id"], "sku": batch["sku"],
                                          "message": frozen["question"]})


def history_and_sql(client, conn, group):
    history = response_json(client.get(f"{GROUPS}/{group}/history"), 200)
    assert response_json(client.get(f"{GROUPS}/{group}/history"), 200) == history
    cards = [entry["search_terms"] for entry in history["data"]["entries"] if "search_terms" in entry]
    messages = conn.execute(
        "SELECT details FROM chat_group_entries e JOIN chat_group_runs r ON e.run_id=r.run_id"
        " WHERE r.conversation_id=? AND e.kind='assistant'", (group,),
    ).fetchall()
    drafts = conn.execute(
        "SELECT item_id FROM search_term_title_drafts d JOIN chat_group_runs r ON d.run_id=r.run_id"
        " WHERE r.conversation_id=?", (group,),
    ).fetchall()
    return cards, len(messages), len(drafts)


def regroup(client, conn, database, sandbox, diagnosis, frozen, batch, folder, scenario):
    published = {}

    def fixed_infer(message, manifest, product, items, trace_id, *, recheck):
        assert not recheck(items, "before_sdk", 0)
        with closing(database.get_db()) as c, c:
            if scenario == "split_and_page":
                membership = list(c.execute(
                    "SELECT * FROM search_term_amazon_membership WHERE ad_group_id='rinse-24-verified'"
                ).fetchone())
                membership[5] = "competing-id"
                c.execute("INSERT INTO search_term_amazon_membership VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", membership)
            else:
                c.execute("DELETE FROM search_term_amazon_membership WHERE ad_group_id=''")
                c.execute("INSERT INTO search_term_amazon_membership VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", saved_member)
        for item in items:
            published[item["id"]] = {**item, "reason": f"Model result for original ID {item['id']}",
                                     "action": "observe", "code": "fixed_result", "citations": []}
        return {"kind": diagnosis.reports.KIND, "report": manifest, "product": product,
                "rules": diagnosis.CLAIM_RULES, "items": list(published.values())}

    saved_member = None
    if scenario == "merge":
        with conn:
            saved_member = tuple(conn.execute(
                "SELECT * FROM search_term_amazon_membership WHERE ad_group_id='rinse-24-verified'"
            ).fetchone())
            conn.execute("DELETE FROM search_term_amazon_membership WHERE ad_group_id IN ('rinse-24-verified','')")
    else:
        with conn:
            conn.execute("DELETE FROM search_term_amazon_membership WHERE ad_group_id=''")
    group, api, report = open_report(client, batch, scenario)
    before = regroup_oracle(sandbox, batch)
    assert len(before) == report["term_count"] == 24
    with (patch.object(diagnosis, "infer", side_effect=fixed_infer),
          patch.object(diagnosis, "create_completion", side_effect=AssertionError("Unexpected SDK request")) as sdk):
        response = diagnose(client, api, report, batch, frozen)
    write(folder / "http.json", {"status": response.status_code, "body": response.text, "sdk_requests": sdk.call_count})
    payload = response_json(response, 200)
    after = regroup_oracle(sandbox, batch)
    write(folder / "independent-sql.json", {"before": before, "after": after})
    assert len(after) == (23 if scenario == "merge" else 25)
    results = []
    for row in before:
        original_id = row["position"]
        detail = response_json(client.get(f"{api}/runs/{payload['run_id']}/items/{original_id}"), 200)
        item = detail["item"]
        lines = row["physical_lines"].split(",")
        assert item["id"] == original_id and detail["report"]["sha256"] == batch["report_sha256"]
        if item["attribution"] == "unavailable":
            assert item["metrics"] is None and not detail["rows"] and item["metrics_unavailable_reason"]
            source = detail["historical_rows"]
        else:
            assert item["metrics"] is not None
            source = detail["rows"]
        assert [record["line"] for record in source] == list(map(int, lines))
        assert all(record["sheet"] is None for record in source)
        assert item["term"].casefold() == row["term"]
        overlaps = [fresh for fresh in after if set(lines) & set(fresh["physical_lines"].split(","))]
        cause = item.get("incomplete_cause")
        if len(overlaps) > 1:
            assert cause == "regrouped_split" and item["attribution"] == "unavailable"
        elif overlaps[0]["physical_lines"] != row["physical_lines"]:
            assert cause == "regrouped_merge" and item["attribution"] == "unavailable"
        elif overlaps[0]["position"] > 24:
            assert cause == "regrouped_page"
        elif not cause:
            assert item["reason"] == f"Model result for original ID {original_id}"
        if cause:
            assert item["action"] == "incomplete" and not item["citations"] and not item.get("review")
        assert detail["draft"] is None
        results.append({"item": item, "current_positions": [fresh["position"] for fresh in overlaps]})
    expected = {"regrouped_merge": 2} if scenario == "merge" else {"regrouped_split": 1, "regrouped_page": 1}
    assert all(sum(result["item"].get("incomplete_cause") == cause for result in results) == count
               for cause, count in expected.items())
    assert any(result["item"]["action"] == "observe" and result["item"]["id"] not in result["current_positions"]
               for result in results)
    cards, replies, drafts = history_and_sql(client, conn, group)
    assert len(cards) == replies == 1 and drafts == 0 and len(cards[0]) == 24
    write(folder / "items.json", results)
    return {"http": response.status_code, "before": len(before), "after": len(after), "scope_changes": expected,
            "assistant_messages": replies, "draft_rows": drafts, "sdk_requests": sdk.call_count}


def sdk_scope(client, conn, database, chat_groups, sandbox, diagnosis, frozen, batch, scores, lock, folder, scenario):
    group, api, report = open_report(client, batch, scenario)
    sql = sql_oracle(conn, batch)
    revoked = scenario in ("shop_revoked", "session_revoked")
    late = scenario in ("late_version", "late_ownership")
    serial = scenario == "serial_control"
    fault = "length" if revoked else None
    audit = SDKLedger(batch, frozen["cases"], scores, sql, lock, frozen["question"], folder, fault=fault, serial=serial)
    sent, release = Event(), Event()

    def sdk(provider, budget, **kwargs):
        stream = audit(provider, budget, **kwargs)
        if (revoked and len(audit.calls) == 3) or late:
            sent.set()
            assert release.wait(10)
        return stream

    def mutate():
        if late or scenario == "commit_version":
            change_scope(database, sandbox, batch, frozen,
                         "ownership_change" if scenario == "late_ownership" else "version_change")
        else:
            with closing(database.get_db()) as writer, writer:
                if scenario == "shop_revoked":
                    writer.execute("DELETE FROM listing_test_shops WHERE user_id=17 AND shop_id=?",
                                   (frozen["membership"]["shop"],))
                else:
                    writer.execute("DELETE FROM sessions WHERE user_id=17")

    save_reply = chat_groups.GroupRun.save_reply

    def mutate_before_save(run, *args, **kwargs):
        mutate()
        return save_reply(run, *args, **kwargs)

    infer = diagnosis.infer
    save = mutate_before_save if scenario == "commit_version" else save_reply
    with (patch.object(diagnosis, "create_completion", side_effect=sdk),
          patch.object(diagnosis, "infer", side_effect=lambda *args, **kwargs: infer(*args, **kwargs, serial=serial)),
          patch.object(chat_groups.GroupRun, "save_reply", save)):
        if revoked or late:
            with ThreadPoolExecutor(max_workers=1) as worker:
                pending = worker.submit(diagnose, client, api, report, batch, frozen)
                try:
                    assert sent.wait(10)
                    mutate()
                finally:
                    release.set()
                response = pending.result(timeout=15)
        else:
            response = diagnose(client, api, report, batch, frozen)
    write(folder / "http.json", {"status": response.status_code, "body": response.text})
    assert not audit.errors
    if scenario == "session_revoked":
        assert client.get(f"{GROUPS}/{group}/history").status_code == 401
        client.headers["Authorization"] = login(client, "scope-test", conn, existing=True)
    cards, replies, drafts = history_and_sql(client, conn, group)
    if revoked:
        assert response.status_code == 409 and "未保存" in response.json()["detail"]
        assert len(audit.calls) == 3 and not cards and replies == drafts == 0
        return {"http": response.status_code, "sdk_requests": len(audit.calls), "post_revocation_requests": 0,
                "assistant_messages": replies, "draft_rows": drafts}
    payload = response_json(response, 200)
    assert len(cards) == replies == 1 and drafts == 0
    affected = []
    details = {}
    for key, stats in zip(batch["item_keys"], sql, strict=True):
        path = f"{api}/runs/{payload['run_id']}/items/{stats['item_id']}"
        detail = response_json(client.get(path), 200)
        item = detail["item"]
        assert [row["line"] for row in detail["rows"]] == frozen["cases"][key]["physical_lines"]
        changed = (stats["ad_group"].endswith("-verified") if scenario == "late_ownership" else
                   stats["attribution"] == "product" if late or scenario == "commit_version" else False)
        if changed:
            affected.append(item["id"])
            assert item["action"] == "incomplete" and item["citations"] == []
            assert not item.get("source_chunks") and not item.get("review") and detail["draft"] is None
            assert client.get(path + "/draft/copy?revision=1").status_code == 404
            assert client.put(path + "/draft", json={"revision": 1, "title": batch["title"]}).status_code == 404
        else:
            assert item["action"] == scores[key]["action"] and item["code"] == scores[key]["code"]
        details[key] = detail
    assert len(affected) == (17 if scenario == "late_ownership" else 22 if late or scenario == "commit_version" else 0)
    traces, requests = collect_trace(folder, "fixed", source=sandbox / "trace.jsonl")
    assert requests == len(audit.calls) == (4 if serial else 1)
    if scenario == "commit_version":
        checks = {step["action"]: step for step in traces[0]["steps"] if step["stage"] == "scope_recheck"}
        assert all(item["result"] == "verified" for item in checks["after_sdk"]["evidence"]["items"])
        assert checks["before_commit"]["evidence"]["document_version"] == "v-next"
        assert sorted(item["id"] for item in checks["before_commit"]["evidence"]["items"]
                      if item["result"] == "product_version_changed") == affected
    write(folder / "items.json", details)
    return {"http": response.status_code, "sdk_requests": requests, "affected_ids": affected,
            "assistant_messages": replies, "draft_rows": drafts}


def login(client, username, conn, *, existing=False):
    from backend.database import database

    password = token_urlsafe(20)
    with conn:
        hashed = database.hash_password(password)
        if existing:
            conn.execute("UPDATE users SET password=? WHERE id=17", (hashed,))
        else:
            conn.execute("INSERT INTO users (id, username, password) VALUES (17, ?, ?)", (username, hashed))
    return "Bearer " + response_json(client.post("/api/login", json={"username": username, "password": password}),
                                    200)["data"]["token"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixed", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    root = args.output_dir.resolve()
    root.mkdir(parents=True)
    frozen, scores, lock = (read(args.fixed / path) for path in ("frozen.json", "scores.json", "protocol.json"))
    (root / "regroup-oracle.sql").write_text(REGROUP_SQL, encoding="utf-8")
    os.environ.update(LLM_PROVIDER="mock", JWT_SECRET=token_urlsafe(48), DIAGNOSIS_MYSQL_ENABLED="false",
                      DIAGNOSIS_SANDBOX_DB_PATH="", AD_OFFLINE_ACCEPTANCE="1", RAG_DB_PATH=str(root / "rag"))
    from backend import observability
    from backend.agent import search_term_diagnosis as diagnosis
    from backend.database import chat_groups, database
    from fast_server import app

    outcomes = {}
    scenarios = ("merge", "split_and_page", "shop_revoked", "session_revoked", "late_version",
                 "late_ownership", "commit_version", "serial_control")
    for scenario in scenarios:
        folder = root / scenario
        folder.mkdir()
        with TemporaryDirectory(prefix="sandbox-", dir=folder) as directory:
            sandbox = Path(directory)
            database.DB_PATH = str(sandbox / "sandbox.sqlite3")
            database.init_db()
            observability.TRACE_LOG_FILE = sandbox / "trace.jsonl"
            observability.ERROR_LOG_FILE = sandbox / "errors.jsonl"
            observability.FUNC_LOG_FILE = sandbox / "functions.jsonl"
            with closing(database.get_db()) as conn, closing(TestClient(app, raise_server_exceptions=False)) as client:
                client.headers["Authorization"] = login(client, "scope-test", conn)
                batch = frozen["batches"]["rinse-24"]
                if scenario in ("merge", "split_and_page"):
                    batch = report_variant(batch, scenario == "split_and_page")
                seed_batch(conn, sandbox, frozen, batch)
                if scenario in ("merge", "split_and_page"):
                    result = regroup(client, conn, database, sandbox, diagnosis, frozen, batch, folder, scenario)
                else:
                    result = sdk_scope(client, conn, database, chat_groups, sandbox, diagnosis, frozen, batch,
                                       scores, lock, folder, scenario)
            collect_trace(folder, "fixed", source=observability.TRACE_LOG_FILE)
        outcomes[scenario] = result
        write(root / "result.json", {"passed": len(outcomes) == len(scenarios), "scenarios": outcomes})
        print(scenario, result, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
