"""Run the frozen Sponsored Products acceptance in a fresh SQLite database and Edge."""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
from contextlib import closing, nullcontext
from decimal import Decimal
from pathlib import Path
from secrets import token_urlsafe
from time import sleep
from types import SimpleNamespace
from unittest.mock import patch

from scripts.accept_task42_t1 import read, serve, write

FIXTURES = Path("tests/fixtures/search_terms")
FILES = {"supported": "amazon-daily.xlsx", "changed": "amazon-daily.xlsx",
         "low": "amazon-low.csv", "mixed": "amazon-mixed.csv"}
PREVIEWS = ("amazon-daily.csv", "amazon-summary.xlsx", "amazon-summary.csv", "report.csv")
MEMBERSHIP = "amazon-advertised-products.csv"


def sha(value):
    return hashlib.sha256(value).hexdigest()


def oracle(path):
    """Recompute from each original file with SQL, independently of application mapping."""
    raw = path.read_bytes()
    if path.suffix == ".xlsx":
        from openpyxl import load_workbook
        with closing(load_workbook(io.BytesIO(raw), read_only=True, data_only=True)) as workbook:
            sheet = workbook.active
            cells = sheet.values
            headers = next(cells)
            source = [dict(zip(headers, row)) for row in cells]
            sheet_name = sheet.title
        raw_lines = [None] * len(source)
    else:
        source = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
        raw_lines = raw.decode("utf-8").splitlines(keepends=True)[1:]
        sheet_name = None
    legacy = path.name == "report.csv"
    daily = "Date" in source[0]
    campaign, group, term, spend = (("Campaign", "Ad Group", "Search Term", "Spend USD") if legacy else
                                    ("Campaign Name", "Ad Group Name", "Customer Search Term", "Spend"))
    orders, sales = (("Orders", "Sales USD") if legacy else ("Purchases", "Sales") if daily else
                     ("7 Day Total Orders (#)", "7 Day Total Sales"))
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE raw_rows (
            line, started, ended, campaign, ad_group, term, impressions, clicks, orders, spend, sales
        )""")
        for line, row in enumerate(source, 2):
            conn.execute("INSERT INTO raw_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                line, row.get("Date") or row["Start Date"], row.get("Date") or row["End Date"],
                row[campaign], row[group], row[term],
                int(row["Impressions"]), int(row["Clicks"]), int(row[orders]),
                int(Decimal(str(row[spend])) * 100), int(Decimal(str(row[sales])) * 100),
            ))
        groups = conn.execute("""SELECT campaign, ad_group, term, min(started) AS start_date, max(ended) AS end_date,
            sum(impressions) AS impressions, sum(clicks) AS clicks, sum(orders) AS orders,
            sum(spend) AS spend_cents, sum(sales) AS sales_cents,
            round(100.0 * sum(clicks) / sum(impressions), 2) AS ctr_pct,
            round(100.0 * sum(orders) / sum(clicks), 2) AS cvr_pct,
            round(sum(spend) / (100.0 * sum(clicks)), 2) AS cpc_usd,
            round(100.0 * sum(spend) / sum(sales), 2) AS acos_pct
            FROM raw_rows GROUP BY campaign, ad_group, term""").fetchall()
    assert len(groups) == 1
    return {"sha256": sha(raw), "values": source, "raw_lines": raw_lines, "sheet": sheet_name,
            "granularity": "daily" if daily else "summary",
            **dict(groups[0])}


def freeze():
    contract = read(FIXTURES / "frozen.json")
    inputs = {filename: oracle(FIXTURES / filename) for filename in sorted(set(FILES.values()) | set(PREVIEWS))}
    raw_membership = (FIXTURES / MEMBERSHIP).read_bytes()
    membership = list(csv.DictReader(io.StringIO(raw_membership.decode("utf-8"))))
    scores = {}
    with closing(sqlite3.connect(":memory:")) as conn:
        for case, filename in FILES.items():
            report = inputs[filename]
            fact = contract["capacity_8" if case == "changed" else "capacity_12"]
            params = {
                **contract["rules"], **{key: report[key] for key in ("impressions", "clicks", "orders")},
                "mixed": len({row["Advertised SKU"] for row in membership
                              if row["Ad Group Name"] == report["ad_group"]}) != 1,
                "request_capacity": int(report["term"].split()[0].removesuffix("L")),
                "facts_capacity": int(fact.rsplit("Capacity: ", 1)[1].split()[0]),
                "capacity_present": bool(re.search(r"\b\d+\s*[Ll]\b", contract["title"])),
            }
            (code,) = conn.execute("""SELECT CASE
              WHEN :mixed THEN 'unattributed'
              WHEN :impressions < :min_impressions OR :clicks < :min_clicks THEN 'sample'
              WHEN :request_capacity <> :facts_capacity THEN 'fact_conflict'
              WHEN :orders < :title_min_orders OR 100 * :orders < :clicks * :title_min_cvr_pct THEN 'weak_performance'
              WHEN :capacity_present THEN 'already_in_title'
              ELSE 'supported_missing' END""", params).fetchone()
            action = {"fact_conflict": "review", "weak_performance": "review", "supported_missing": "title"}.get(code, "observe")
            scores[case] = {"action": action, "code": code}
            assert all(contract["expected"][case][key] == value for key, value in scores[case].items()), case
    assert len(contract["valid_edit"]) == 75 and len(contract["over_limit"]) == 76
    return {"fixture_hash": sha((FIXTURES / "frozen.json").read_bytes()), "contract": contract,
            "files": FILES, "independent_sql": inputs, "independent_scores": scores,
            "membership_sha256": sha(raw_membership), "membership": membership}


def facts(contract, capacity):
    return [
        {"fact_id": "capacity", "statement": contract[f"capacity_{capacity}"], "options": [f"{capacity} L"]},
        {"fact_id": "brand", "statement": contract["brand"], "options": ["Trailbox"]},
        {"fact_id": "product_type", "statement": contract["product_type"],
         "options": ["Reusable Soft-Sided Insulated Lunch Bag, Zippered Cooler Tote"]},
    ]


def seed(database, contract, password):
    database.init_db()
    with closing(database.get_db()) as conn, conn:
        conn.executemany("INSERT INTO users(id, username, password) VALUES (?, ?, ?)", [
            (17, "sp-test-operator", database.hash_password(password)),
            (18, "sp-test-other", database.hash_password(password)),
        ])
        conn.execute("INSERT INTO listing_test_shops VALUES ('search-terms-test', 'US', 17)")
        products = [("SP-LUNCH", contract["title"]), ("SP-CONTROL", "Trailbox Picnic Bag")]
        conn.executemany("INSERT INTO listing_test_products VALUES ('search-terms-test','US',?,1,?,'[]',2499,24)", products)
        conn.execute("INSERT INTO listing_test_facts VALUES ('search-terms-test','US','SP-LUNCH',1,?)",
                     (json.dumps(facts(contract, 12)),))
        conn.executemany("INSERT INTO search_term_ad_groups VALUES ('search-terms-test','US','SP-101',?,?)",
                         (("AG-101", "SP-LUNCH"), ("AG-102", "SP-LUNCH"), ("AG-102", "SP-CONTROL")))
        raw = (FIXTURES / MEMBERSHIP).read_bytes()
        conn.executemany("INSERT INTO search_term_amazon_membership VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (*row.values(), sha(raw), f"{MEMBERSHIP}:{line}")
            for line, row in enumerate(csv.DictReader(io.StringIO(raw.decode("utf-8"))), 2)
        ])


class Replay:
    name = "Fixed SDK replay"
    model = "frozen-search-term-fixture"

    def __init__(self, contract):
        self.contract = contract
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, messages, **_):
        assert messages[1]["content"] == self.contract["question"]
        evidence = json.loads(messages[-1]["content"])
        assert evidence["rules"] == self.contract["rules"]
        receipt = evidence["receipt"]
        quote = next((fact["statement"] for fact in evidence["product"]["facts"] if fact["fact_id"] == "capacity"), "")
        title = evidence["current_listing"]["text"]
        assert evidence["report"]["granularity"] == "daily"
        assert (evidence["report"]["currency"], evidence["report"]["attribution_days"]) == ("USD", 7)
        result = []
        for item in evidence["items"]:
            if item["attribution"] == "ad_group":
                case = "mixed"
                assert not (item["ownership"] or title or quote)
            elif item["metrics"]["clicks"] < self.contract["rules"]["min_clicks"]:
                case = "low"
            else:
                case = "changed" if quote == self.contract["capacity_8"] else "supported"
            if case != "mixed":
                assert title == self.contract["title"]
                assert quote in {self.contract["capacity_8"], self.contract["capacity_12"]}
                assert all(entry["sku"] == "SP-LUNCH" and len(entry["membership_sha256"]) == 64
                           for entry in item["ownership"])
            expected = self.contract["expected"][case]
            result.append({"id": item["id"], "action": expected["action"], "code": expected["code"]})
        sleep(0.65)
        content = json.dumps({"receipt": receipt, "capacity_quote": quote, "title_quote": title, "items": result})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def collect_trace(folder, mode, *, source=None):
    source = folder / "trace.jsonl" if source is None else source
    traces = []
    if source.exists():
        for line in source.read_text(encoding="utf-8").splitlines():
            trace = json.loads(line)
            if trace.get("source") == "search_term_diagnosis":
                steps = [{key: step[key] for key in ("stage", "action", "status", "duration", "error_code", "evidence")
                          if key in step} for step in trace["steps"]]
                traces.append({"trace_id": trace["trace_id"], "status": trace["status"], "steps": steps,
                               "total_seconds": trace["total_duration"]})
    sdk_requests = sum(step.get("evidence", {}).get("sdk_requests", 0) for trace in traces for step in trace["steps"])
    write(folder / "sanitized-trace.json", {"mode": mode, "sdk_requests": sdk_requests, "traces": traces})
    return traces, sdk_requests


def reconcile_trace(trace, outcome, frozen, mode):
    name = next(case for case, score in frozen["independent_scores"].items()
                if score["code"] == outcome["result"]["code"])
    original = frozen["independent_sql"][frozen["files"][name]]
    contract = frozen["contract"]
    stages = {step["stage"]: step for step in trace["steps"]}
    observed = stages["model_input"]["evidence"]

    def fingerprint(value):
        return sha(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))

    expected_scope = {"report_sha256": original["sha256"], "report_version": outcome["receipt"]["report_version"],
                      "marketplace": "US", "currency": "USD", "granularity": original["granularity"],
                      "attribution_days": 7, "batch": 1, "start_date": original["start_date"],
                      "end_date": original["end_date"], "item_count": 1}
    matched = all(observed[key] == value for key, value in expected_scope.items())
    source = next(entry for entry in frozen["membership"]
                  if entry["Ad Group Name"] == original["ad_group"] and entry["Advertised SKU"] == "SP-LUNCH")
    scope = {"source": "Amazon Sponsored Products Search Term Report", "marketplace": "US",
             "start_date": original["start_date"], "end_date": original["end_date"], "row_count": len(original["values"]),
             "groups": [{"campaign": original["campaign"], "ad_group": original["ad_group"],
                         "campaign_id": source["Campaign ID"], "ad_group_id": source["Ad Group ID"]}],
             "format": "amazon-sp-us-seller-v1", "currency": "USD",
             "granularity": original["granularity"], "attribution_days": 7, "batch": 1}
    if name == "mixed":
        ownership, product_facts, quote, title = [], [], "", ""
    else:
        ownership = [{**location, "sku": source["Advertised SKU"], "asin": source["Advertised ASIN"],
                      "campaign_id": source["Campaign ID"], "ad_group_id": source["Ad Group ID"],
                      "matched_by": "ids" if "Campaign ID" in original["values"][0] else "period_names",
                      "membership_sha256": frozen["membership_sha256"],
                      "membership_location": f"{MEMBERSHIP}:{frozen['membership'].index(source) + 2}",
                      "membership_start": source["Start Date"], "membership_end": source["End Date"]}
                     for location in outcome["rows"]]
        capacity = 8 if name == "changed" else 12
        product_facts, quote, title = facts(contract, capacity), contract[f"capacity_{capacity}"], contract["title"]
    verification = stages["verification"]["evidence"]
    expected_decision = {"id": 1, **frozen["independent_scores"][name]}
    call = stages["model_call"]["evidence"]
    checks = {
        "scope": matched and observed["scope_sha256"] == fingerprint(scope),
        "ownership": observed["ownership_sha256"] == fingerprint([ownership]),
        "originals": (observed["facts_sha256"] == fingerprint(product_facts)
                      and verification["capacity_quote_sha256"] == fingerprint(quote)
                      and verification["title_quote_sha256"] == fingerprint(title)
                      and verification["capacity_quote_matched"] and verification["title_quote_matched"]),
        "decision": verification["receipt_matched"] and verification["model_decisions"] == [expected_decision],
        "provider": call["provider"].startswith("GLM(") if mode == "live" else call["provider"] == Replay.name,
    }
    if "rerun" in outcome:
        checks["original_run"] = stages["rerun"]["evidence"] == {"original_run_id": outcome["rerun"]["original_run_id"]}
    return checks


def run(mode, root):
    frozen = freeze()
    browser_environment = {key: value for key, value in os.environ.items()
                           if not any(marker in key for marker in ("API_KEY", "SECRET", "PASSWORD", "TOKEN"))}
    if mode == "live":
        prior = read(root / "fixed" / "result.json")
        if not prior["passed"] or read(root / "fixed" / "frozen.json") != frozen:
            raise SystemExit("A passing fixed replay with identical frozen inputs is required.")
    if mode == "fixed" and (root / "baseline" / "result.json").is_file():
        if not read(root / "baseline" / "result.json")["passed"] or read(root / "baseline" / "frozen.json") != frozen:
            raise SystemExit("The baseline must reproduce the failure with the same frozen inputs.")
    folder = root / mode
    folder.mkdir(parents=True, exist_ok=False)
    write(folder / "frozen.json", frozen)
    contract = frozen["contract"]
    fixed = mode in {"baseline", "fixed"}
    os.environ["LLM_PROVIDER"] = "mock" if fixed else "glm"
    os.environ["JWT_SECRET"] = token_urlsafe(48)
    os.environ["DIAGNOSIS_MYSQL_ENABLED"] = "false"
    os.environ["DIAGNOSIS_SANDBOX_DB_PATH"] = ""
    os.environ["RAG_DB_PATH"] = str(folder / "rag")
    if fixed:
        os.environ["AD_OFFLINE_ACCEPTANCE"] = "1"
    from backend import observability
    from backend.agent import search_term_diagnosis
    from backend.config.config import settings
    from backend.database import database

    assert search_term_diagnosis.RULES == contract["rules"]
    database_path = folder / "sandbox.sqlite3"
    database.DB_PATH = str(database_path)
    password = token_urlsafe(20)
    seed(database, contract, password)
    observability.TRACE_LOG_FILE = folder / "trace.jsonl"
    observability.ERROR_LOG_FILE = folder / "errors.jsonl"
    observability.FUNC_LOG_FILE = folder / "functions.jsonl"
    from fast_server import app

    if not fixed and not settings.llm_config("glm").api_key:
        raise SystemExit("GLM_API_KEY is required for the live acceptance and manual demo.")
    if mode == "demo":
        with serve(app) as origin:
            print(f"StorePilot {origin}\n测试账号 sp-test-operator\n测试密码 {password}", flush=True)
            print(f"在会话中上传 {(FIXTURES / 'report.csv').resolve()}", flush=True)
            input("按 Enter 停止本地演示。")
        return 0
    replay = patch.object(search_term_diagnosis.ProviderFactory, "create", return_value=Replay(contract)) if fixed else nullcontext()
    environment = {**browser_environment, "AD_BROWSER_CHANNEL": "msedge", "SEARCH_TERMS_PASSWORD": password}
    with replay, serve(app) as origin:
        browser = subprocess.run(["node", "tests/search_terms_browser.cjs", origin, str(folder), mode],
                                 env=environment, capture_output=True, timeout=180, check=False)
    (folder / "browser-stdout.txt").write_bytes(browser.stdout)
    (folder / "browser-stderr.txt").write_bytes(browser.stderr)
    traces, requests = collect_trace(folder, mode)
    expected_calls = 4 if mode == "fixed" else 1
    passed = browser.returncode == 0 and len(traces) == expected_calls and requests == expected_calls and all(
        trace["status"] == "completed" for trace in traces
    )
    trace_checks = {}
    if passed:
        cases = read(folder / "browser-result.json")["cases"]
        by_id = {outcome["trace_id"]: outcome for outcome in cases.values()}
        trace_checks = {trace["trace_id"]: reconcile_trace(trace, by_id[trace["trace_id"]], frozen, mode)
                        for trace in traces}
        passed = all(all(checks.values()) for checks in trace_checks.values())
    write(folder / "result.json", {"passed": passed, "browser_exit": browser.returncode,
                                  "sdk_requests": requests, "mode": mode,
                                  "seconds": [trace["total_seconds"] for trace in traces], "trace_checks": trace_checks})
    print(json.dumps(read(folder / "result.json")), folder)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("baseline", "fixed", "live", "demo"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    return run(args.mode, args.output_dir.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
