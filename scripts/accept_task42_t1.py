"""Independent TASK-042-T1 acceptance. Run from the repository root."""

import argparse
import csv
from contextlib import ExitStack, closing, contextmanager
from datetime import date, timedelta
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import sqlite3
import subprocess
from threading import Thread
from time import monotonic, sleep
from unittest.mock import patch
from zipfile import ZipFile


ROOT = Path("logs/task42-t1")
SOURCE = Path("logs/ecommerce-olist-source/v2")
SELLER = "6560211a19b47992c3666cc44a7e94c0"
ORIGINAL = Path("logs/task42/olist-sales/chart-reconciliation.json")
SQL = Path(__file__).with_name("task42_t1_olist.sql")


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_source(conn):
    conn.executescript("""
        CREATE TABLE raw_orders(id PRIMARY KEY, day, status);
        CREATE TABLE raw_items(id, line, product, seller, PRIMARY KEY(id,line));
        CREATE INDEX product_seller ON raw_items(seller,product);
    """)
    manifest = read(SOURCE / "source-audit.json")["files"]
    actual = {}
    for name, details in manifest.items():
        archive = (SOURCE / (name + ".zip")).read_bytes()
        with ZipFile(SOURCE / (name + ".zip")) as zipped:
            payload = zipped.read(name)
        rows = list(csv.DictReader(StringIO(payload.decode("utf-8-sig"))))
        actual[name] = {"archive_sha256": sha256(archive).hexdigest(),
                        "csv_sha256": sha256(payload).hexdigest(), "rows": len(rows)}
        assert all(details[k] == v for k, v in actual[name].items())
        if "order_items" in name:
            conn.executemany("INSERT INTO raw_items VALUES (?,?,?,?)", (
                (r["order_id"], r["order_item_id"], r["product_id"], r["seller_id"]) for r in rows))
        else:
            conn.executemany("INSERT INTO raw_orders VALUES (?,?,?)", (
                (r["order_id"], r["order_purchase_timestamp"][:10], r["order_status"]) for r in rows))
    return actual


def select_cases(conn):
    original = read(ORIGINAL)
    excluded_products = sorted({row["product_id"] for row in original.values()})
    excluded_days = set()
    for row in original.values():
        for name in ("baseline", "current"):
            excluded_days.update(p["date"] for p in row[name]["points"])
    conn.execute("CREATE TABLE exclusions(product PRIMARY KEY)")
    conn.executemany("INSERT INTO exclusions VALUES (?)", [(p,) for p in excluded_products])
    candidates = conn.execute("""
        SELECT i.product, o.day, o.status, o.id, count(*) own_units,
               (SELECT count(*) FROM raw_items j WHERE j.id=o.id AND j.seller<>:seller) other_units,
               (SELECT count(*) FROM raw_items j WHERE j.id=o.id AND j.seller<>:seller
                   AND j.product=i.product) other_same_product
        FROM raw_items i JOIN raw_orders o ON o.id=i.id
        WHERE i.seller=:seller AND i.product NOT IN (SELECT product FROM exclusions)
        GROUP BY i.product,o.id
        ORDER BY i.product,o.day,o.id
    """, {"seller": SELLER}).fetchall()
    by_product = {}
    for row in candidates:
        by_product.setdefault(row["product"], []).append(row)

    def choose(predicate, statuses, used=()):
        for rows in by_product.values():
            if rows[0]["product"] in used:
                continue
            for start in sorted({date.fromisoformat(r["day"]) - timedelta(days=7) for r in rows}):
                days = [(start + timedelta(days=n)).isoformat() for n in range(14)]
                if excluded_days.intersection(days):
                    continue
                b = [r for r in rows if days[0] <= r["day"] <= days[6]]
                c = [r for r in rows if days[7] <= r["day"] <= days[-1]]
                if predicate(b, c):
                    return rows[0]["product"], (days[0], days[6], days[7], days[-1]), statuses, b + c
        raise AssertionError("No sample matched selection rule")

    default = ["delivered", "shipped"]
    mixed = choose(lambda b, c: any(r["other_units"] for r in b+c if r["status"] in default)
                   and any(r["status"] in default for r in b) and any(r["status"] in default for r in c), default)
    multi = choose(lambda b, c: any(r["own_units"] > 1 for r in b+c if r["status"] in default)
                   and any(r["status"] in default for r in b) and any(r["status"] in default for r in c),
                   default, (mixed[0],))
    ordinary = choose(lambda b, c: b and c and all(r["own_units"] == 1 and not r["other_units"]
                      and r["status"] == "delivered" for r in b+c), default, (mixed[0], multi[0]))
    shipped = choose(lambda b, c: any(r["status"] == "delivered" for r in b)
                     and not any(r["status"] == "shipped" for r in b)
                     and {r["status"] for r in c} >= {"shipped", "delivered"},
                     ["shipped"], (mixed[0], multi[0], ordinary[0]))
    empty = ordinary[0], ("2015-02-01", "2015-02-07", "2015-02-08", "2015-02-14"), default, []
    return {
        "selected_seller_and_multi_item": mixed,
        "multi_item": multi,
        "ordinary": ordinary,
        "shipped_and_zero_baseline": shipped,
        "delivered_on_same_window": (*shipped[:2], ["delivered"], shipped[3]),
        "empty": empty,
    }, excluded_products, sorted(excluded_days)


def freeze(output):
    output.mkdir(parents=True, exist_ok=False)
    started = monotonic()
    sql = SQL.read_text(encoding="utf-8")
    expected, rows = {}, {}
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        hashes = load_source(conn)
        cases, excluded_products, excluded_days = select_cases(conn)
        for name, (product, dates, statuses, source_rows) in cases.items():
            params = dict(zip(("baseline_start", "baseline_end", "current_start", "current_end"), dates))
            params.update(seller=SELLER, product=product, status1=statuses[0], status2=statuses[-1])
            result = {"source": "olist-v2", "source_label": "Olist v2 · 公开历史订单", "shop_id": SELLER,
                      "product_id": product, "included_statuses": statuses, "date_basis": "olist_purchase_date"}
            for window in ("baseline", "current"):
                result[window] = {"start_date": params[window+"_start"], "end_date": params[window+"_end"], "points": []}
            for row in conn.execute(sql, params):
                window, day, units, orders, units_pct, orders_pct = row
                if window == "change":
                    result["change"] = {"units": {"absolute": units, "relative_pct": units_pct},
                                        "orders": {"absolute": orders, "relative_pct": orders_pct}}
                elif day is None:
                    result[window]["totals"] = {"units": units, "orders": orders}
                else:
                    result[window]["points"].append({"date": day, "units": units, "orders": orders})
            expected[name] = result
            rows[name] = [dict(r) for r in source_rows]
    write(output / "chart-reconciliation.json", expected)
    write(output / "freeze.json", {"sources": hashes, "sql_sha256": sha256(sql.encode()).hexdigest(),
          "expected_sha256": sha256((output / "chart-reconciliation.json").read_bytes()).hexdigest(),
          "excluded_products": excluded_products, "excluded_days": excluded_days,
          "selection": "Ascending product and date; current window starts on a matching raw day. "
                       "Disjoint products except status control and empty-window control. "
                       "Mixed orders include at least one line from another seller.",
          "source_rows": rows, "seconds": round(monotonic() - started, 3)})


def configure(path, provider="mock"):
    os.environ.update(AD_OFFLINE_ACCEPTANCE="1", PYTHON_DOTENV_DISABLED="1", DIAGNOSIS_MYSQL_ENABLED="0",
                      DIAGNOSIS_SANDBOX_DB_PATH=str(path.resolve()), JWT_SECRET=token_urlsafe(48),
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False",
                      LLM_PROVIDER=provider, AD_BROWSER_CHANNEL="msedge")


@contextmanager
def serve(app):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    with socket.socket() as local:
        local.bind(("127.0.0.1", 0))
        local.listen()
        thread = Thread(target=server.run, kwargs={"sockets": [local]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 30
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.05)
            assert server.started, "Local FastAPI failed to start"
            yield f"http://127.0.0.1:{local.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def order_chart(output):
    import httpx

    frozen = read(output / "freeze.json")
    source = output / "chart-reconciliation.json"
    assert frozen["expected_sha256"] == sha256(source.read_bytes()).hexdigest()
    configure(output / "orders.sqlite3")
    from backend.database import database, orders
    from fast_server import app
    from scripts import orders_import

    database.init_db()
    database.seed_data()
    with closing(database.get_db()) as conn:
        orders_import.provision(conn, orders.OLIST, SELLER, "boss")
        orders_import.import_olist(conn, SOURCE)
    results = {}
    with serve(app) as origin, httpx.Client(base_url=origin, timeout=15, trust_env=False) as client:
        login = client.post("/api/login", json={"username": "boss", "password": "admin123"})
        headers = {"Authorization": "Bearer " + login.json()["data"]["token"]}
        for name, target in read(source).items():
            started = monotonic()
            params = {"product_id": target["product_id"]}
            if len(target["included_statuses"]) == 1:
                params["status"] = target["included_statuses"][0]
            for window in ("baseline", "current"):
                for edge in ("start", "end"):
                    params[f"{window}_{edge}"] = target[window][edge+"_date"]
            response = client.get(f"/api/orders/shops/olist-v2/{SELLER}/sales", headers=headers, params=params)
            actual = response.json().get("data")
            results[name] = {"status": response.status_code, "actual": actual,
                             "passed": response.status_code == 200 and actual == target,
                             "seconds": round(monotonic() - started, 4)}
        write(output / "chart-api.json", results)
        started = monotonic()
        browser = subprocess.run(["node", "tests/olist_sales_browser.cjs", origin, str(output.resolve())],
                                 capture_output=True, timeout=120, check=False)
        write(output / "chart-browser-process.json", {"exit_code": browser.returncode,
              "stdout": browser.stdout.decode(errors="replace"), "stderr": browser.stderr.decode(errors="replace"),
              "seconds": round(monotonic() - started, 3)})
    assert all(result["passed"] for result in results.values())
    return browser.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["freeze", "olist", "fixed", "live", "routing", "verify-live"])
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.stage == "freeze":
        return freeze(args.output_dir / "olist")
    if args.stage == "olist":
        return order_chart(args.output_dir / "olist")
    from scripts.task42_t1_workgroup import verify_live, workgroups
    if args.stage == "verify-live":
        return verify_live(args.output_dir)
    return workgroups(args.output_dir, args.stage == "live", routing=args.stage == "routing")


if __name__ == "__main__":
    raise SystemExit(main())
