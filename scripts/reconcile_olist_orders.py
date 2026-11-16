"""Independently compare local Olist source rows to the application database."""

import argparse
import csv
import io
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile


SELLER = "6560211a19b47992c3666cc44a7e94c0"
SOURCE = "olist-v2"


def load_csv(conn, directory, filename, table, placeholders):
    with ZipFile(directory / (filename + ".zip")) as archive:
        with io.TextIOWrapper(archive.open(filename), encoding="utf-8-sig", newline="") as stream:
            rows = csv.reader(stream)
            next(rows)
            conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)


def compare(db_path, directory):
    with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TEMP TABLE raw_orders(
                id TEXT PRIMARY KEY, customer TEXT, status TEXT, purchased TEXT, approved TEXT,
                carrier TEXT, delivered TEXT, estimated TEXT);
            CREATE TEMP TABLE raw_items(
                id TEXT, item TEXT, product TEXT, seller TEXT, ship_by TEXT, price TEXT, freight TEXT,
                PRIMARY KEY(id, item));
            CREATE INDEX temp.raw_items_by_seller ON raw_items(seller, id);
        """)
        load_csv(conn, directory, "olist_orders_dataset.csv", "raw_orders", "?,?,?,?,?,?,?,?")
        load_csv(conn, directory, "olist_order_items_dataset.csv", "raw_items", "?,?,?,?,?,?,?")
        conn.execute("""
            CREATE TEMP TABLE expected_items AS
            SELECT id AS order_id, item AS item_id, NULL AS sku, NULLIF(product,'') AS product_id,
                1 AS quantity, NULLIF(ship_by,'') AS ship_by
            FROM raw_items WHERE seller=?
        """, (SELLER,))
        conn.execute("""
            CREATE TEMP TABLE expected_orders AS
            SELECT id AS order_id, NULLIF(status,'') AS status, NULLIF(purchased,'') AS ordered_at,
                NULL AS source_updated_at, NULL AS ship_by, NULLIF(carrier,'') AS carrier_at,
                EXISTS(SELECT 1 FROM raw_items WHERE raw_items.id=raw_orders.id AND seller<>?) AS mixed_seller
            FROM raw_orders JOIN (SELECT DISTINCT order_id FROM expected_items) AS seller
            ON seller.order_id=raw_orders.id
        """, (SELLER,))
        conn.execute("""
            CREATE TEMP TABLE actual_orders AS
            SELECT order_id,status,ordered_at,source_updated_at,ship_by,carrier_at,mixed_seller
            FROM shop_orders WHERE source=? AND shop_id=?
        """, (SOURCE, SELLER))
        conn.execute("""
            CREATE TEMP TABLE actual_items AS
            SELECT order_id,item_id,sku,product_id,quantity,ship_by FROM shop_order_items WHERE source=? AND shop_id=?
        """, (SOURCE, SELLER))
        differences = {}
        for name in ("orders", "items"):
            for left, right in (("expected", "actual"), ("actual", "expected")):
                differences[f"{name}_{left}_only"] = [dict(row) for row in conn.execute(
                    f"SELECT * FROM {left}_{name} EXCEPT SELECT * FROM {right}_{name} ORDER BY 1"
                )]
        by_order = [dict(row) for row in conn.execute("""
            WITH all_ids AS (SELECT order_id FROM expected_orders UNION SELECT order_id FROM actual_orders),
            expected_counts AS (SELECT order_id, count(*) AS items FROM expected_items GROUP BY order_id),
            actual_counts AS (SELECT order_id, count(*) AS items FROM actual_items GROUP BY order_id),
            order_diffs AS (
                SELECT order_id FROM (SELECT * FROM expected_orders EXCEPT SELECT * FROM actual_orders)
                UNION SELECT order_id FROM (SELECT * FROM actual_orders EXCEPT SELECT * FROM expected_orders)
            ),
            item_diffs AS (
                SELECT order_id FROM (SELECT * FROM expected_items EXCEPT SELECT * FROM actual_items)
                UNION SELECT order_id FROM (SELECT * FROM actual_items EXCEPT SELECT * FROM expected_items)
            )
            SELECT all_ids.order_id, e.status, e.mixed_seller,
                COALESCE(ec.items,0) AS expected_items, COALESCE(ac.items,0) AS actual_items,
                od.order_id IS NULL AND id.order_id IS NULL AS match
            FROM all_ids LEFT JOIN expected_orders e USING(order_id)
            LEFT JOIN expected_counts ec USING(order_id) LEFT JOIN actual_counts ac USING(order_id)
            LEFT JOIN order_diffs od USING(order_id) LEFT JOIN item_diffs id USING(order_id)
            ORDER BY all_ids.order_id
        """)]
        summary = dict(conn.execute("""
            SELECT (SELECT count(*) FROM raw_orders) AS all_orders,
                (SELECT count(*) FROM raw_items) AS all_items,
                (SELECT count(*) FROM expected_orders) AS expected_orders,
                (SELECT count(*) FROM actual_orders) AS actual_orders,
                (SELECT count(*) FROM expected_items) AS expected_items,
                (SELECT count(*) FROM actual_items) AS actual_items,
                (SELECT count(*) FROM expected_orders WHERE mixed_seller) AS mixed_seller_orders,
                (SELECT count(*) FROM actual_orders WHERE carrier_at IS NULL) AS missing_carrier_timestamp,
                (SELECT count(*) FROM actual_orders WHERE source_updated_at IS NULL) AS missing_source_updated_at,
                (SELECT count(*) FROM actual_items a JOIN raw_items r
                 ON a.order_id=r.id AND a.item_id=r.item WHERE r.seller<>?) AS other_seller_items
        """, (SELLER,)).fetchone())
        summary["order_status"] = dict(conn.execute("SELECT status, count(*) FROM actual_orders GROUP BY status"))
        summary["matched_orders"] = sum(row["match"] for row in by_order)
        mixed = {}
        for row in conn.execute("SELECT * FROM expected_orders WHERE mixed_seller ORDER BY order_id"):
            order = dict(row)
            order["items"] = [dict(item) for item in conn.execute("""
                SELECT item_id,sku,product_id,quantity,ship_by FROM expected_items WHERE order_id=?
                ORDER BY length(item_id),item_id
            """, (order["order_id"],))]
            mixed[order["order_id"]] = order
    return {"source": SOURCE, "shop_id": SELLER, "summary": summary, "differences": differences,
            "orders": by_order, "mixed_seller_expected": mixed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=Path("logs/ecommerce-olist-source/v2"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.db, args.source_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]))
    summary = report["summary"]
    if (any(report["differences"].values()) or summary["other_seller_items"] or
            (summary["matched_orders"], summary["actual_items"], summary["mixed_seller_orders"]) != (1854, 2033, 31)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
