"""Replay the single-seller Olist catalog against a raw-CSV sales oracle."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import date, timedelta
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import subprocess
from time import perf_counter
from zipfile import ZipFile


SOURCE = Path("logs/ecommerce-olist-source/v2")
SELLER = "6560211a19b47992c3666cc44a7e94c0"
SCOPE = f"/api/orders/shops/olist-v2/{SELLER}/sales"
STATUSES = (("delivered", "shipped"), ("delivered",), ("shipped",))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_rows(source, filename, manifest, verified):
    archive = source / (filename + ".zip")
    with ZipFile(archive) as compressed:
        payload = compressed.read(filename)
    rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
    hashes = {
        "archive_sha256": sha256(archive.read_bytes()).hexdigest(),
        "csv_sha256": sha256(payload).hexdigest(),
        "rows": len(rows),
    }
    if any(manifest[filename][key] != value for key, value in hashes.items()):
        raise ValueError(f"Olist source manifest mismatch for {filename}")
    verified[filename] = hashes
    return rows


def freeze_source(source):
    manifest = json.loads((source / "source-audit.json").read_text(encoding="utf-8"))["files"]
    verified = {}
    purchases = {}
    for row in source_rows(source, "olist_orders_dataset.csv", manifest, verified):
        day = date.fromisoformat(row["order_purchase_timestamp"].split()[0])
        purchases[row["order_id"]] = (day, row["order_status"])
    catalog = defaultdict(list)
    merchants = defaultdict(set)
    other_products = set()
    for row in source_rows(source, "olist_order_items_dataset.csv", manifest, verified):
        order_id = row["order_id"]
        merchants[order_id].add(row["seller_id"])
        if row["seller_id"] == SELLER:
            catalog[row["product_id"]].append((order_id, *purchases[order_id]))
        else:
            other_products.add(row["product_id"])
    own_orders = {row[0] for lines in catalog.values() for row in lines}
    mixed_orders = {order for order in own_orders if len(merchants[order]) > 1}
    mixed_products = {product for product, lines in catalog.items() if any(row[0] in mixed_orders for row in lines)}
    return catalog, mixed_orders, mixed_products, min(other_products.difference(catalog)), verified


def contains_day(pairs, day):
    return any(start <= day < start + timedelta(days=width)
               for _, baseline, current, width in pairs for start in (baseline, current))


def pairs_for(lines):
    first, last = min(line[1] for line in lines), max(line[1] for line in lines)
    fixed_pairs = (
        ("first_day", first - timedelta(days=1), first, 1),
        ("last_day", last - timedelta(days=1), last, 1),
        ("latest_week", last - timedelta(days=13), last - timedelta(days=6), 7),
        ("latest_fortnight", last - timedelta(days=27), last - timedelta(days=13), 14),
        ("latest_month", last - timedelta(days=59), last - timedelta(days=29), 30),
        ("latest_maximum", last - timedelta(days=179), last - timedelta(days=89), 90),
        ("separated_weeks", first - timedelta(days=6), max(first + timedelta(days=9), last - timedelta(days=6)), 7),
        ("empty_future", last + timedelta(days=1), last + timedelta(days=8), 7),
    )
    backfill_blocks = {(last - day).days // 180 for _, day, _ in lines if not contains_day(fixed_pairs, day)}
    historical_pairs = []
    for block in sorted(backfill_blocks):
        end = last - timedelta(days=block * 180)
        historical_pairs.append(("historical_maximum", end - timedelta(days=179), end - timedelta(days=89), 90))
    return (*fixed_pairs, *historical_pairs)


def expected_window(lines, start, width, statuses):
    units = Counter()
    order_ids = defaultdict(set)
    end = start + timedelta(days=width - 1)
    for order_id, day, status in lines:
        if start <= day <= end and status in statuses:
            units[day] += 1
            order_ids[day].add(order_id)
    points = []
    for offset in range(width):
        day = start + timedelta(days=offset)
        points.append({"date": day.isoformat(), "units": units[day], "orders": len(order_ids[day])})
    return {"start_date": start.isoformat(), "end_date": end.isoformat(), "points": points,
            "totals": {"units": sum(units.values()), "orders": len(set().union(*order_ids.values()))}}


def expected_cases(catalog, mixed_products):
    for product, lines in sorted(catalog.items()):
        for name, first, second, width in pairs_for(lines):
            for statuses in STATUSES:
                baseline = expected_window(lines, first, width, statuses)
                current = expected_window(lines, second, width, statuses)
                change = {}
                for metric in ("units", "orders"):
                    old, new = baseline["totals"][metric], current["totals"][metric]
                    change[metric] = {"absolute": new - old, "relative_pct": (new - old) * 100 / old if old else None}
                params = {"product_id": product}
                for key, window in (("baseline", baseline), ("current", current)):
                    params[key + "_start"], params[key + "_end"] = window["start_date"], window["end_date"]
                if len(statuses) == 1:
                    params["status"] = statuses[0]
                expected = {"source": "olist-v2", "source_label": "Olist v2 · 公开历史订单",
                            "shop_id": SELLER, "product_id": product, "included_statuses": list(statuses),
                            "date_basis": "olist_purchase_date", "baseline": baseline,
                            "current": current, "change": change}
                yield name, params, expected, product in mixed_products


def replay(output, source, browser):
    import httpx

    from scripts.accept_task42_t1 import serve

    os.environ.update(LLM_PROVIDER="mock", JWT_SECRET=token_urlsafe(48), PYTHON_DOTENV_DISABLED="1",
                      AD_OFFLINE_ACCEPTANCE="1", DIAGNOSIS_MYSQL_ENABLED="0", HF_HUB_OFFLINE="1",
                      TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False",
                      DIAGNOSIS_SANDBOX_DB_PATH=str((output / "olist.sqlite3").resolve()))
    from backend.database import database, orders
    from fast_server import app
    from scripts import orders_import

    started = perf_counter()
    catalog, mixed_orders, mixed_products, foreign_product, verified = freeze_source(source)
    cases = list(expected_cases(catalog, mixed_products))
    oracle_hash = sha256()
    source_coverage = Counter()
    source_totals = Counter()
    for lines in catalog.values():
        pairs = pairs_for(lines)
        for _, day, status in lines:
            source_totals[status] += 1
            if contains_day(pairs, day):
                source_coverage[status] += 1
    for name, params, expected, mixed in cases:
        oracle_hash.update(json.dumps([name, params, expected, mixed], sort_keys=True).encode() + b"\n")
    frozen = {"sources": verified, "oracle_sha256": oracle_hash.hexdigest(), "seller": SELLER,
              "products": len(catalog), "orders": len({row[0] for lines in catalog.values() for row in lines}),
              "items": sum(map(len, catalog.values())), "mixed_seller_orders": len(mixed_orders),
              "products_with_mixed_seller_orders": len(mixed_products), "comparison_cases": len(cases),
              "source_item_status_counts": dict(source_totals), "source_items_in_replayed_windows": dict(source_coverage),
              "oracle_seconds": round(perf_counter() - started, 3)}
    write(output / "frozen.json", frozen)
    database.init_db()
    database.seed_data()
    with closing(database.get_db()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        orders_import.provision(conn, orders.OLIST, SELLER, "boss")
        first_import = orders_import.import_olist(conn, source)
        second_import = orders_import.import_olist(conn, source)
    counts = Counter()
    mismatches = []
    timings = []
    started = perf_counter()
    with serve(app) as origin, httpx.Client(base_url=origin, timeout=15, trust_env=False) as client, \
            (output / "case-results.jsonl").open("w", encoding="utf-8") as records:
        def login(username, password):
            response = client.post("/api/login", json={"username": username, "password": password})
            response.raise_for_status()
            return {"Authorization": "Bearer " + response.json()["data"]["token"]}

        owner = login("boss", "admin123")
        other = login("lisi", "user123")
        product_response = client.get(SCOPE + "/products", headers=owner)
        expected_products = [
            {"product_id": product, "first_date": min(line[1] for line in lines).isoformat(),
             "last_date": max(line[1] for line in lines).isoformat()}
            for product, lines in sorted(catalog.items(), key=lambda item: (-len(item[1]), item[0]))
        ]
        catalog_passed = product_response.status_code == 200 and product_response.json().get("data") == expected_products
        for name, params, expected, mixed in cases:
            case_started = perf_counter()
            response = client.get(SCOPE, headers=owner, params=params)
            timings.append((perf_counter() - case_started) * 1_000)
            actual = response.json().get("data")
            passed = response.status_code == 200 and response.headers.get("Cache-Control") == "no-store" and actual == expected
            counts["passed" if passed else "failed"] += 1
            counts["daily_points"] += len(expected["baseline"]["points"]) + len(expected["current"]["points"])
            counts["mixed_product_cases"] += mixed
            counts["both_windows_empty"] += not (expected["baseline"]["totals"]["units"] or expected["current"]["totals"]["units"])
            counts["both_windows_nonempty"] += bool(expected["baseline"]["totals"]["units"] and expected["current"]["totals"]["units"])
            counts["zero_baseline_positive_current"] += not expected["baseline"]["totals"]["units"] and expected["current"]["totals"]["units"] > 0
            counts["cases_with_multi_item_order"] += any(window["totals"]["units"] > window["totals"]["orders"]
                                                         for window in (expected["baseline"], expected["current"]))
            counts["status_" + params.get("status", "default")] += 1
            summary = {"window_type": name, "params": params, "status": response.status_code, "passed": passed,
                       "baseline": expected["baseline"]["totals"], "current": expected["current"]["totals"],
                       "expected_sha256": sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(),
                       "actual_sha256": sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()}
            records.write(json.dumps(summary, ensure_ascii=False) + "\n")
            if not passed:
                mismatches.append({**summary, "expected": expected, "actual": actual})

        params = cases[0][1]
        response_codes = {
            "anonymous_sales": (client.get(SCOPE, params=params).status_code, 401),
            "other_account_sales": (client.get(SCOPE, headers=other, params=params).status_code, 404),
            "other_account_catalog": (client.get(SCOPE + "/products", headers=other).status_code, 404),
            "wrong_seller": (client.get(SCOPE.replace(SELLER, "not-this-seller"), headers=owner, params=params).status_code, 404),
            "invalid_status": (client.get(SCOPE, headers=owner, params={**params, "status": "canceled"}).status_code, 422),
            "unequal_windows": (client.get(SCOPE, headers=owner, params={**params, "current_end": "2026-11-12"}).status_code, 422),
            "overlapping_windows": (client.get(SCOPE, headers=owner,
                                                  params={**params, "current_start": params["baseline_end"]}).status_code, 422),
            "over_90_days": (client.get(SCOPE, headers=owner, params={**params, "baseline_start": "2015-01-01",
                             "baseline_end": "2015-04-01", "current_start": "2015-04-02", "current_end": "2015-07-01"}).status_code, 422),
        }
        probe = client.get(SCOPE, headers=owner, params={**params, "product_id": "missing-product"})
        foreign = client.get(SCOPE, headers=owner, params={**params, "product_id": foreign_product})
        unknown_product = {"status": probe.status_code, "body": probe.json()}
        foreign_product_probe = {"product_id": foreign_product, "status": foreign.status_code, "body": foreign.json()}
        browser_exit = None
        if browser:
            first = next(row for row in cases if row[0] == "separated_weeks" and row[3])
            second = next(row for row in cases if row[1]["product_id"] == first[1]["product_id"]
                          and row[0] == "latest_maximum" and len(row[2]["included_statuses"]) == 2)
            write(output / "browser-samples.json", {name: expected for name, _, expected, _ in (first, second)})
            result = subprocess.run(["node", "tests/task42_olist_batch_browser.cjs", origin, str(output.resolve())],
                                    capture_output=True, timeout=90, check=False)
            browser_exit = result.returncode
            write(output / "browser-process.json", {"exit_code": browser_exit,
                  "stdout": result.stdout.decode(errors="replace"), "stderr": result.stderr.decode(errors="replace")})

    timings.sort()
    passing = (not mismatches and catalog_passed and browser_exit in (None, 0)
               and source_totals == source_coverage
               and probe.status_code == foreign.status_code == 404 and probe.json() == foreign.json()
               and all(actual == expected for actual, expected in response_codes.values()))
    report = {**frozen, "passed": passing, "first_import": first_import, "second_import": second_import,
              "catalog_passed": catalog_passed, "counts": dict(counts),
              "permission_and_validation": response_codes, "unknown_product_probe": unknown_product,
              "foreign_product_probe": foreign_product_probe,
              "browser_exit_code": browser_exit,
              "replay_seconds": round(perf_counter() - started, 3),
              "request_ms": {"p50": round(timings[len(timings) // 2], 3),
                             "p95": round(timings[(len(timings) * 95 - 1) // 100], 3),
                             "maximum": round(timings[-1], 3)}, "mismatches": mismatches}
    write(output / "report.json", report)
    print(json.dumps({key: report[key] for key in ("passed", "products", "comparison_cases", "counts", "replay_seconds", "request_ms")}, ensure_ascii=False))
    return 0 if passing else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=SOURCE)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--browser", action="store_true", help="also exercise disjoint and 90-day charts in Chromium")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    return replay(args.output_dir, args.source_dir, args.browser)


if __name__ == "__main__":
    raise SystemExit(main())
