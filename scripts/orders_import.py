"""Provision a shop for an existing account or import the fixed Olist v2 seller."""

import argparse
import csv
import hashlib
import io
import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

from backend.config.config import settings
from backend.database import orders
from backend.database.database import get_db


SELLER = "6560211a19b47992c3666cc44a7e94c0"
SOURCE_DIR = Path("logs/ecommerce-olist-source/v2")
ORDERS = "olist_orders_dataset.csv"
ITEMS = "olist_order_items_dataset.csv"
HASHES = {
    ORDERS: (
        "3fbb7ac9469098e4e216f3b555d3aa1de543266fd1bcccb5a8c02d6240025692",
        "8df58ef3d2d7e9944010f7beecd9b75367f5588ec6e3c91cec19ae3345ef9ecf",
    ),
    ITEMS: (
        "02d4500597b8ae0a798bbfccf4f49b5f5db12537b5085c0a08613cecbd7070c0",
        "0bc4d068c4fe38cbb01bd90e8746e3c613fe7b4baef75fab7b0e329701c3e279",
    ),
}


def verified_rows(directory, name):
    archive = directory / (name + ".zip")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != HASHES[name][0]:
            raise ValueError(f"ZIP SHA-256 不匹配 ({name})")
        with ZipFile(stream) as zipped:
            payload = zipped.read(name)
    if hashlib.sha256(payload).hexdigest() != HASHES[name][1]:
        raise ValueError(f"CSV SHA-256 不匹配 ({name})")
    with io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def olist_records(directory):
    own = {}
    other_orders = set()
    for row in verified_rows(directory, ITEMS):
        order_id = row["order_id"]
        if row["seller_id"] == SELLER:
            own.setdefault(order_id, []).append(orders.Item(
                row["order_item_id"], None, row["product_id"] or None, 1, row["shipping_limit_date"] or None,
            ))
        else:
            other_orders.add(order_id)
    for row in verified_rows(directory, ORDERS):
        order_id = row["order_id"]
        if order_id in own:
            yield orders.Order(
                order_id, row["order_status"] or None, row["order_purchase_timestamp"] or None,
                None, None, row["order_delivered_carrier_date"] or None, order_id in other_orders,
                tuple(own.pop(order_id)),
            )
    if own:
        raise ValueError("Olist 行项找不到对应的订单")


def provision(conn, source, shop_id, username):
    with conn:
        user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if user is None:
            raise ValueError("请指定一个已存在的应用账号")
        conn.execute("INSERT OR IGNORE INTO order_shops VALUES (?, ?)", (source, shop_id))
        conn.execute("INSERT OR IGNORE INTO order_shop_access VALUES (?, ?, ?)", (source, shop_id, user["id"]))


def import_olist(conn, directory):
    try:
        with conn:
            written = orders.write_orders(
                conn, orders.OLIST, SELLER, olist_records(directory),
                synced_at=orders.utc_text(datetime.now(UTC)), replace_snapshot=True,
            )
            if written != (1854, 2033):
                raise ValueError(f"Olist v2 卖家范围数量不符 ({written})")
            return orders.record_run(conn, orders.OLIST, SELLER, "import", written=written)
    except Exception:
        with conn:
            orders.record_run(conn, orders.OLIST, SELLER, "import", error_code="olist_import_failed", written=(0, 0))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=[orders.OLIST, orders.AMAZON])
    parser.add_argument("--username", required=True, help="existing application account to grant shop access")
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    args = parser.parse_args()
    if settings.DIAGNOSIS_MYSQL_ENABLED:
        parser.error("订单功能使用 SQLite 应用库")
    shop_id = SELLER if args.source == orders.OLIST else settings.AMAZON_ORDERS_SHOP_ID.strip()
    if not shop_id or len(shop_id) > 128 or not shop_id.isascii() or not all(
        char.isalnum() or char in "-_" for char in shop_id
    ):
        parser.error("请配置有效的 AMAZON_ORDERS_SHOP_ID")
    with closing(get_db()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='users'").fetchone() is None:
            parser.error("请先启动 FastAPI 初始化应用库和账号")
        orders.init_schema(conn)
        provision(conn, args.source, shop_id, args.username)
        result = import_olist(conn, args.source_dir) if args.source == orders.OLIST else {
            "source": args.source, "shop_id": shop_id, "access_granted": True,
        }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
