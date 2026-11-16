"""Check shop-authorized sales against the published Olist CSV files."""

from contextlib import closing
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.config.config import settings
from backend.database import database, orders
from scripts import orders_import
from scripts.reconcile_olist_orders import load_csv


PRODUCT = "d017a2151d543a9885604dc62a3d9dcc"
PARAMETERS = {
    "product_id": PRODUCT,
    "baseline_start": "2018-04-11", "baseline_end": "2018-04-24",
    "current_start": "2018-04-25", "current_end": "2018-05-08",
}
SCOPE = "/api/orders/shops/olist-v2/" + orders_import.SELLER + "/sales"


def csv_window(source, product, start, end, statuses):
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    days = {
        (start_date + timedelta(days=offset)).isoformat(): [0, set()]
        for offset in range((end_date - start_date).days + 1)
    }
    # Select source rows in SQL, then count each item's unit and deduplicate its order independently.
    rows = source.execute(
        """SELECT o.id, o.purchased, o.status FROM raw_orders AS o
           WHERE o.id IN (SELECT id FROM raw_items WHERE seller=? AND product=?)""",
        (orders_import.SELLER, product),
    )
    for order_id, purchased, status in rows:
        day = purchased.split(" ")[0]
        if day in days and status in statuses:
            count = source.execute(
                "SELECT count(*) FROM raw_items WHERE id=? AND seller=? AND product=?",
                (order_id, orders_import.SELLER, product),
            ).fetchone()[0]
            days[day][0] += count
            days[day][1].add(order_id)
    points = [{"date": day, "units": units, "orders": len(ids)} for day, (units, ids) in days.items()]
    return {
        "start_date": start, "end_date": end, "points": points,
        "totals": {"units": sum(row["units"] for row in points), "orders": sum(row["orders"] for row in points)},
    }


class OlistSalesTests(unittest.TestCase):
    def setUp(self):
        from fast_server import app

        directory = self.enterContext(TemporaryDirectory(prefix="olist-sales-", dir=Path.cwd() / "logs"))
        self.enterContext(patch.object(database, "DB_PATH", str(Path(directory) / "orders.sqlite3")))
        for key, value in {
            "DIAGNOSIS_SANDBOX_DB_PATH": "", "DIAGNOSIS_MYSQL_ENABLED": False,
            "JWT_SECRET": "task42-local-test-key-olist-sales" * 2,
        }.items():
            self.enterContext(patch.object(settings, key, value))
        database.init_db()
        database.seed_data()
        with closing(database.get_db()) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            orders_import.provision(conn, orders.OLIST, orders_import.SELLER, "boss")
            orders_import.import_olist(conn, orders_import.SOURCE_DIR)
        self.client = TestClient(app)
        self.owner = self.login("boss", "admin123")
        self.other = self.login("lisi", "user123")
        self.source = self.enterContext(closing(sqlite3.connect(":memory:")))
        self.source.executescript("""
            CREATE TABLE raw_orders(id,customer,status,purchased,approved,carrier,delivered,estimated);
            CREATE TABLE raw_items(id,item,product,seller,ship_by,price,freight);
            CREATE INDEX raw_item_order ON raw_items(id,seller,product);
        """)
        load_csv(self.source, orders_import.SOURCE_DIR, "olist_orders_dataset.csv", "raw_orders", "?,?,?,?,?,?,?,?")
        load_csv(self.source, orders_import.SOURCE_DIR, "olist_order_items_dataset.csv", "raw_items", "?,?,?,?,?,?,?")

    def login(self, username, password):
        response = self.client.post("/api/login", json={"username": username, "password": password})
        return {"Authorization": "Bearer " + response.json()["data"]["token"]}

    def test_sales_against_source_csv(self):
        scenarios = {
            "selected_seller_and_multi_item": PARAMETERS,
            "delivered_only": {
                **PARAMETERS, "baseline_start": "2017-05-19", "baseline_end": "2017-06-01",
                "current_start": "2017-06-02", "current_end": "2017-06-15", "status": "delivered",
            },
            "shipped_only_and_zero_baseline": {
                **PARAMETERS, "baseline_start": "2017-05-19", "baseline_end": "2017-06-01",
                "current_start": "2017-06-02", "current_end": "2017-06-15", "status": "shipped",
            },
            "no_matching_dates": {
                **PARAMETERS, "baseline_start": "2015-01-01", "baseline_end": "2015-01-14",
                "current_start": "2015-01-15", "current_end": "2015-01-28",
            },
        }
        evidence = {}
        for name, params in scenarios.items():
            with self.subTest(name=name):
                response = self.client.get(SCOPE, headers=self.owner, params=params)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                data = response.json()["data"]
                statuses = [params["status"]] if "status" in params else ["delivered", "shipped"]
                self.assertEqual(data["included_statuses"], statuses)
                self.assertEqual(data["source"], "olist-v2")
                self.assertEqual(data["product_id"], PRODUCT)
                self.assertEqual(data["date_basis"], "olist_purchase_date")
                expected = {
                    key: csv_window(self.source, PRODUCT, params[f"{key}_start"], params[f"{key}_end"], statuses)
                    for key in ("baseline", "current")
                }
                for key, chart_window in expected.items():
                    self.assertEqual(data[key], chart_window)
                for metric in ("units", "orders"):
                    old = expected["baseline"]["totals"][metric]
                    delta = expected["current"]["totals"][metric] - old
                    self.assertEqual(data["change"][metric], {"absolute": delta, "relative_pct": delta * 100 / old if old else None})
                evidence[name] = data
        mixed = evidence["selected_seller_and_multi_item"]["baseline"]["points"][-1]
        self.assertEqual(mixed["date"], "2018-04-24")
        self.assertGreater(mixed["units"], mixed["orders"])
        self.assertGreater(evidence["shipped_only_and_zero_baseline"]["current"]["totals"]["units"], 0)
        self.assertEqual(evidence["no_matching_dates"]["current"]["totals"], {"units": 0, "orders": 0})
        if output := os.environ.get("AD_OLIST_SALES_REPORT"):
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_sales_filters_and_permissions(self):
        products = self.client.get(SCOPE + "/products", headers=self.owner).json()["data"]
        self.assertIn(PRODUCT, {row["product_id"] for row in products})
        first, last = self.source.execute(
            """SELECT min(purchased), max(purchased) FROM raw_orders WHERE id IN (
               SELECT id FROM raw_items WHERE seller=? AND product=?)""",
            (orders_import.SELLER, PRODUCT),
        ).fetchone()
        product = next(row for row in products if row["product_id"] == PRODUCT)
        self.assertEqual((product["first_date"], product["last_date"]), (first[:10], last[:10]))
        self.assertEqual(self.client.get(SCOPE + "/products", headers=self.other).status_code, 404)
        self.assertEqual(self.client.get(SCOPE, params=PARAMETERS, headers=self.other).status_code, 404)
        self.assertEqual(self.client.get(SCOPE, params=PARAMETERS).status_code, 401)
        foreign_product = self.source.execute(
            "SELECT product FROM raw_items WHERE product NOT IN (SELECT product FROM raw_items WHERE seller=?) LIMIT 1",
            (orders_import.SELLER,),
        ).fetchone()[0]
        for product_id in ("missing-product", foreign_product):
            with self.subTest(product_id=product_id):
                response = self.client.get(SCOPE, params={**PARAMETERS, "product_id": product_id}, headers=self.owner)
                self.assertEqual((response.status_code, response.json()), (404, {"detail": "商品不存在"}))
        for invalid in (
            {"baseline_start": "2018-04-31"}, {"current_start": PARAMETERS["baseline_end"]},
            {"current_end": "2018-05-09"}, {"status": "canceled"}, {"product_id": ""},
        ):
            with self.subTest(invalid=invalid):
                response = self.client.get(SCOPE, params={**PARAMETERS, **invalid}, headers=self.owner)
                self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
