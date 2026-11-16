"""Fixed Amazon replay and SQLite order transaction regressions."""

from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from pydantic import SecretStr
from requests import Response

from backend.config.config import settings
from backend.database import database, orders
from backend.routes.amazon_orders import LWA_URL, ORDERS_URL
from scripts import orders_import
from scripts.reconcile_olist_orders import compare


FIXTURE = json.loads(Path("tests/orders_amazon_fixture.json").read_text(encoding="utf-8"))
SHOP = "/api/orders/shops/amazon-us-fbm/fixture-us"
WINDOW = {"updated_after": "2026-01-01T00:00:00Z", "updated_before": "2026-01-04T00:00:00Z"}
ORDER_ID = "111-0000000-0000001"
REPORT_DIR = Path(os.environ.get("AD_ORDERS_REPORT_DIR", "logs/task41"))


class OrdersTest(unittest.TestCase):
    def setUp(self):
        from fast_server import app

        logs = Path.cwd() / "logs"
        logs.mkdir(exist_ok=True)
        directory = self.enterContext(TemporaryDirectory(prefix="orders-test-", dir=logs))
        self.db = Path(directory) / "orders.sqlite3"
        self.enterContext(patch.object(database, "DB_PATH", str(self.db)))
        for name, value in {
            "DIAGNOSIS_SANDBOX_DB_PATH": "", "DIAGNOSIS_MYSQL_ENABLED": False, "AMAZON_ORDERS_SHOP_ID": "fixture-us",
            "AMAZON_LWA_CLIENT_ID": SecretStr("fixture-client"), "AMAZON_LWA_CLIENT_SECRET": SecretStr("fixture-secret"),
            "AMAZON_LWA_REFRESH_TOKEN": SecretStr("fixture-refresh-token"), "JWT_SECRET": "task41-test-only-signing-key" * 2,
        }.items():
            self.enterContext(patch.object(settings, name, value))
        database.init_db()
        database.seed_data()
        self.client = TestClient(app)
        self.boss = self.login("boss", "admin123")
        self.lisi = self.login("lisi", "user123")
        with closing(database.get_db()) as conn:
            orders_import.provision(conn, orders.AMAZON, "fixture-us", "boss")
            orders_import.provision(conn, orders.AMAZON, "other-shop", "lisi")

    def login(self, username, password):
        reply = self.client.post("/api/login", json={"username": username, "password": password})
        return {"Authorization": "Bearer " + reply.json()["data"]["token"]}

    def replay(self, path, replies, *, headers=None):
        pending = iter(replies)
        requests_seen = []

        def send(_, request, **kwargs):
            url = urlsplit(request.url)
            query = {key: value[0] for key, value in parse_qs(url.query).items()}
            if "paginationToken" in query:
                self.assertEqual(query["paginationToken"], "fixture+page/2=")
                query["paginationToken"] = "[REDACTED]"
            observed = {"method": request.method, "url": request.url.split("?")[0], "query": query}
            if request.url == LWA_URL:
                self.assertEqual(request.method, "POST")
                self.assertEqual(parse_qs(request.body), {
                    "grant_type": ["refresh_token"], "client_id": ["fixture-client"],
                    "client_secret": ["fixture-secret"], "refresh_token": ["fixture-refresh-token"],
                })
                observed["form"] = {key: "refresh_token" if key == "grant_type" else "[REDACTED]"
                                    for key in parse_qs(request.body)}
            else:
                self.assertEqual(observed["url"], ORDERS_URL)
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.headers["x-amz-access-token"], FIXTURE["lwa"]["access_token"])
                self.assertRegex(request.headers["x-amz-date"], r"^\d{8}T\d{6}Z$")
                self.assertEqual(request.headers["User-Agent"], "AdAgent/3.0 (Language=Python)")
                observed["headers"] = {"x-amz-access-token": "[REDACTED]", "x-amz-date": request.headers["x-amz-date"],
                                       "User-Agent": request.headers["User-Agent"]}
                expected = {
                    "marketplaceIds": "ATVPDKIKX0DER", "fulfilledBy": "MERCHANT", "includedData": "FULFILLMENT",
                    "maxResultsPerPage": "1" if path.endswith("/preflight") else "100",
                }
                self.assertEqual({key: query[key] for key in expected}, expected)
                self.assertLess(query["lastUpdatedAfter"], query["lastUpdatedBefore"])
                if path.endswith("/sync"):
                    self.assertEqual(query["lastUpdatedAfter"], "2026-01-01T00:00:00.000000Z")
                    self.assertEqual(query["lastUpdatedBefore"], "2026-01-04T00:00:00.000000Z")
            self.assertFalse(kwargs["allow_redirects"])
            requests_seen.append(observed)
            status, payload, headers = next(pending)
            observed["response"] = {
                "status": status, "body": {**payload, "access_token": "[REDACTED]", "refresh_token": "[REDACTED]"}
                if request.url == LWA_URL and status == 200 else payload,
                "headers": headers,
            }
            response = Response()
            response.status_code = status
            response.headers.update(headers)
            response._content = json.dumps(payload).encode()
            return response

        with patch("requests.sessions.Session.send", autospec=True, side_effect=send):
            response = self.client.post(
                SHOP + path, headers=self.boss if headers is None else headers,
                json=WINDOW if path == "/sync" else None,
            )
        self.assertEqual(len(requests_seen), len(replies))
        return response, requests_seen

    def stored(self):
        with closing(sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return {
                table: [dict(row) for row in conn.execute(
                    f"SELECT * FROM {table} WHERE source=? AND shop_id=? ORDER BY {sort}",
                    (orders.AMAZON, "fixture-us"),
                )] for table, sort in (("shop_orders", "order_id"), ("shop_order_items", "order_id, item_id"))
            }

    def test_amazon_contract_replay(self):
        lwa = (200, FIXTURE["lwa"], {})
        page1, final = deepcopy(FIXTURE["pages"])
        old = deepcopy(page1["orders"][0])
        old["lastUpdatedTime"] = "2026-01-02T10:00:00-05:00"
        old["fulfillment"]["fulfillmentStatus"] = "UNSHIPPED"
        old["orderItems"][0]["product"]["sellerSku"] = "STALE"
        final["orders"] += [page1["orders"][0], old]
        altered = deepcopy(page1)
        altered["orders"][0]["lastUpdatedTime"] = "2026-01-03T17:00:00Z"
        altered["orders"][0]["orderItems"][0]["product"]["sellerSku"] = "UNCOMMITTED"
        trace = {}

        def run(name, path, replies, status, error=None, pages=None):
            response, requests_seen = self.replay(path, replies)
            receipt = response.json()["data"]
            self.assertEqual(response.status_code, status)
            self.assertEqual((receipt["error_code"], receipt["pages"]), (error, pages))
            self.assertEqual(receipt["outcome"], "failed" if error else "succeeded")
            trace[name] = {"status": status, "requests": requests_seen, "receipt": receipt}
            return response

        probe = {"orders": page1["orders"][:1], "pagination": page1["pagination"]}
        run("preflight", "/preflight", [lwa, (200, probe, {})], 200, pages=1)
        self.assertEqual(self.stored()["shop_orders"], [])
        first = [(200, page1, {}), (200, final, {})]
        run("two_pages", "/sync", [lwa, *first], 200, pages=2)
        committed = self.stored()
        self.assertEqual(len(committed["shop_orders"]), 2)
        self.assertEqual(len(committed["shop_order_items"]), 2)
        self.assertEqual(committed["shop_orders"][0]["status"], "SHIPPED")
        self.assertEqual(committed["shop_orders"][0]["ordered_at"], "2026-01-01T13:00:00.000000Z")
        self.assertEqual(committed["shop_orders"][0]["ship_by"], "2026-01-04T23:59:00.000000Z")
        self.assertIsNone(committed["shop_orders"][1]["ship_by"])
        self.assertEqual(committed["shop_order_items"][0]["sku"], "SKU-1")
        self.assertIsNone(committed["shop_order_items"][1]["sku"])
        self.assertIsNone(committed["shop_order_items"][0]["ship_by"])
        run("duplicate_pages", "/sync", [lwa, *first], 200, pages=2)
        self.assertEqual(trace["duplicate_pages"]["receipt"]["orders_written"], 0)
        run("late_older_window", "/sync", [lwa, (200, {"orders": [old]}, {})], 200, pages=1)
        self.assertEqual(trace["late_older_window"]["receipt"]["orders_written"], 0)
        with patch.object(settings, "AMAZON_LWA_REFRESH_TOKEN", SecretStr("")):
            run("missing_credentials", "/sync", [], 503, "amazon_credentials_missing", 0)
        run("invalid_lwa_grant", "/sync", [(400, {"error": "invalid_grant"}, {})],
            502, "amazon_authorization_failed", 0)
        run("orders_access_denied", "/sync", [lwa, (403, FIXTURE["denied"], {})],
            502, "amazon_authorization_failed", 0)
        result = run("rate_limit", "/sync", [lwa, (200, altered, {}), (429, FIXTURE["limited"], {"Retry-After": "180"})],
                     429, "amazon_rate_limited", 1)
        self.assertEqual(result.headers["Retry-After"], "180")
        run("failed_second_page", "/sync", [lwa, (200, altered, {}), (503, FIXTURE["unavailable"], {})],
            502, "amazon_upstream_error", 1)
        run("repeated_pagination_token", "/sync", [lwa, (200, altered, {}), (200, altered, {})],
            502, "amazon_pagination_loop", 2)
        run("malformed_second_page", "/sync", [lwa, (200, altered, {}), (200, {"payload": final}, {})],
            502, "amazon_invalid_response", 2)
        self.assertEqual(committed, self.stored())
        self.assertEqual(self.client.get(SHOP, headers=self.lisi).status_code, 404)
        self.assertEqual(self.client.get(SHOP + "/orders/" + ORDER_ID, headers=self.lisi).status_code, 404)
        self.assertEqual(self.client.get(SHOP, headers=self.boss).json()["data"]["totals"], {"orders": 2, "items": 2})
        self.assertEqual(self.client.post(SHOP + "/sync", headers=self.lisi, json=WINDOW).status_code, 404)
        self.assertEqual(self.client.get(SHOP).status_code, 401)
        with closing(database.get_db()) as conn, conn:
            orders.write_orders(conn, orders.AMAZON, "other-shop",
                                [orders.Order(ORDER_ID, "PENDING", None, None, None, None, None, ())],
                                synced_at="fixture")
        other = SHOP.replace("fixture-us", "other-shop") + "/orders/" + ORDER_ID
        self.assertEqual(self.client.get(other, headers=self.lisi).json()["data"]["status"], "PENDING")
        self.assertEqual(self.client.get(other, headers=self.boss).status_code, 404)
        updated = altered["orders"][0]
        updated["orderItems"] = [{"orderItemId": "replacement", "quantityOrdered": 3, "product": {"sellerSku": "SKU-NEW"}}]
        run("newer_update_replaces_lines", "/sync", [lwa, (200, {"orders": [updated]}, {})], 200, pages=1)
        updated_order = self.client.get(SHOP + "/orders/" + ORDER_ID, headers=self.boss).json()["data"]
        self.assertEqual([item["sku"] for item in updated_order["items"]], ["SKU-NEW"])
        self.assertEqual(self.stored()["shop_order_items"][0]["item_id"], "replacement")
        self.login("boss", "admin123")
        self.assertEqual(self.client.get(SHOP, headers=self.boss).status_code, 401)
        output = REPORT_DIR / "amazon-replay.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"cases": trace, "committed_before_failures": committed,
                                      "committed_after_newer_update": self.stored()}, indent=2) + "\n", encoding="utf-8")

    def test_amazon_incomplete_response_replay(self):
        lwa = (200, FIXTURE["lwa"], {})
        first, final = deepcopy(FIXTURE["pages"])
        original = first["orders"][0]
        trace = {}

        def get(path=SHOP):
            response = self.client.get(path, headers=self.boss)
            self.assertEqual(response.status_code, 200)
            return response.json()["data"]

        def run(name, payloads, error=None, *, path="/sync"):
            response, requests_seen = self.replay(path, [lwa, *((200, payload, {}) for payload in payloads)])
            receipt = response.json()["data"]
            self.assertEqual(response.status_code, 502 if error else 200)
            self.assertEqual(receipt["error_code"], error)
            self.assertEqual(receipt["outcome"], "failed" if error else "succeeded")
            self.assertEqual(receipt["pages"], len(payloads))
            self.assertEqual(get()["runs"][0], receipt)
            if error:
                self.assertEqual(receipt["upstream_status"], 200)
                self.assertEqual((receipt["orders_written"], receipt["items_written"]), (0, 0))
            trace[name] = {"status": response.status_code, "requests": requests_seen, "receipt": receipt}
            return receipt

        for name, field_path, error in [
            ("missing_marketplace", ("salesChannel", "marketplaceId"), "amazon_invalid_marketplace"),
            ("missing_last_updated", ("lastUpdatedTime",), "amazon_invalid_last_updated_time"),
            ("missing_fulfilled_by", ("fulfillment", "fulfilledBy"), "amazon_invalid_fulfillment"),
            ("missing_created_time", ("createdTime",), "amazon_invalid_created_time"),
        ]:
            raw = deepcopy(original)
            container = raw
            for field in field_path[:-1]:
                container = container[field]
            del container[field_path[-1]]
            run(name, [{"orders": [raw]}], error)
        for name, pagination in [
            ("missing_next_token", {}), ("null_pagination", None),
            ("blank_next_token", {"nextToken": " "}), ("numeric_next_token", {"nextToken": 3}),
        ]:
            run(name, [{"orders": [original], "pagination": pagination}], "amazon_invalid_pagination")
        run("preflight_missing_next_token", [{"orders": [original], "pagination": {}}],
            "amazon_invalid_pagination", path="/preflight")
        invalid = deepcopy(original)
        invalid["fulfillment"]["fulfilledBy"] = "UNKNOWN"
        run("unknown_fulfillment", [{"orders": [invalid]}], "amazon_invalid_fulfillment")
        invalid = deepcopy(original)
        invalid["lastUpdatedTime"] = "2026-01-02T15:30:00"
        run("last_updated_without_offset", [{"orders": [invalid]}], "amazon_invalid_last_updated_time")
        empty_db = self.stored()
        self.assertEqual(empty_db, {"shop_orders": [], "shop_order_items": []})
        with closing(sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            successes = conn.execute("SELECT count(*) FROM order_ingestion_runs WHERE outcome='succeeded'").fetchone()[0]
        self.assertEqual(successes, 0)

        # Out-of-scope orders still get filtered before the required time checks.
        for raw in first["orders"][1:]:
            del raw["createdTime"]
        final["orders"].append(deepcopy(final["orders"][0]))
        final["orders"][-1]["orderId"] = "111-0000000-0000005"
        del final["orders"][-1]["fulfillment"]["shipByWindow"]
        final["orders"][-1]["orderItems"][0]["orderItemId"] = "line-5"
        receipt = run("valid_multi_page_optional_deadline", [first, final])
        self.assertEqual((receipt["orders_written"], receipt["items_written"]), (3, 3))
        valid = self.stored()
        self.assertEqual({row["order_id"] for row in valid["shop_orders"]},
                         {ORDER_ID, "111-0000000-0000004", "111-0000000-0000005"})
        self.assertEqual(get()["totals"], {"orders": 3, "items": 3})
        optional_details = []
        for order_id, line in [("111-0000000-0000004", "line-4"), ("111-0000000-0000005", "line-5")]:
            order = get(SHOP + "/orders/" + order_id)
            self.assertIsNone(order["ship_by"])
            self.assertEqual([item["item_id"] for item in order["items"]], [line])
            optional_details.append(order)

        before = {"sql": self.stored(), "detail": get(SHOP + "/orders/" + ORDER_ID)}
        updated = deepcopy(original)
        updated["lastUpdatedTime"] = "2026-01-03T17:00:00Z"
        updated["fulfillment"]["fulfillmentStatus"] = "UNSHIPPED"
        updated["orderItems"] = [{"orderItemId": "replacement", "quantityOrdered": 4, "product": {"sellerSku": "NEW"}}]
        broken = deepcopy(final)
        del broken["orders"][0]["lastUpdatedTime"]
        run("incomplete_second_page_preserves_snapshot",
            [{"orders": [updated], "pagination": first["pagination"]}, broken], "amazon_invalid_last_updated_time")
        after = {"sql": self.stored(), "detail": get(SHOP + "/orders/" + ORDER_ID)}
        self.assertEqual(after, before)

        output = REPORT_DIR / "amazon-incomplete-replay.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({
            "cases": trace, "after_initial_failures": {"sql": empty_db, "success_receipts": successes},
            "before_second_page_failure": before, "after_second_page_failure": after,
            "optional_deadline_get": optional_details,
        }, indent=2) + "\n", encoding="utf-8")
        if export := os.environ.get("AD_ORDERS_REPLAY_DB"):
            with (
                closing(sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True)) as source,
                closing(sqlite3.connect(export)) as target,
            ):
                source.backup(target)

    def test_olist_real_snapshot_and_interruption(self):
        with closing(database.get_db()) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            orders_import.provision(conn, orders.OLIST, orders_import.SELLER, "boss")
            for _ in range(2):
                result = orders_import.import_olist(conn, orders_import.SOURCE_DIR)
                self.assertEqual((result["orders_written"], result["items_written"]), (1854, 2033))
            report = compare(self.db, orders_import.SOURCE_DIR)
            self.assertEqual(report["summary"]["matched_orders"], 1854)
            self.assertEqual(report["summary"]["other_seller_items"], 0)
            self.assertFalse(any(report["differences"].values()))
            scope = "/api/orders/shops/olist-v2/" + orders_import.SELLER
            order_id = next(iter(report["mixed_seller_expected"]))
            detail = scope + "/orders/" + order_id
            before = self.client.get(detail, headers=self.boss).json()

            def interrupted(_):
                yield orders.Order("partial", "test", None, None, None, None, False, ())
                self.assertEqual(self.client.get(detail, headers=self.boss).json(), before)
                raise InterruptedError("fixture stops after one uncommitted order")

            with patch.object(orders_import, "olist_records", interrupted), self.assertRaises(InterruptedError):
                orders_import.import_olist(conn, orders_import.SOURCE_DIR)
            self.assertEqual(self.client.get(detail, headers=self.boss).json(), before)
            self.assertEqual(self.client.get(scope + "/orders/partial", headers=self.boss).status_code, 404)
            data = self.client.get(scope, headers=self.boss).json()["data"]
            self.assertEqual(data["totals"], {"orders": 1854, "items": 2033})
            self.assertEqual(data["runs"][0]["error_code"], "olist_import_failed")
            for order_id, expected in report["mixed_seller_expected"].items():
                actual = self.client.get(scope + "/orders/" + order_id, headers=self.boss).json()["data"]
                self.assertEqual({key: actual[key] for key in expected}, expected)
            missing = conn.execute(
                "SELECT order_id FROM shop_orders WHERE source=? AND carrier_at IS NULL LIMIT 1", (orders.OLIST,),
            ).fetchone()[0]
            order = self.client.get(scope + "/orders/" + missing, headers=self.boss).json()["data"]
            self.assertIsNone(order["carrier_at"])
            self.assertIsNone(order["source_updated_at"])
            self.assertEqual(self.client.get(scope, headers=self.lisi).status_code, 404)
            self.assertEqual(self.client.get(detail, headers=self.lisi).status_code, 404)


if __name__ == "__main__":
    unittest.main()
