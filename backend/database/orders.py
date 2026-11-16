"""Shop-scoped orders and atomic source imports for the SQLite application."""

from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple


OLIST = "olist-v2"
AMAZON = "amazon-us-fbm"
OLIST_SALES_EVIDENCE = "olist_sales_v1"
OLIST_SALES_STATUSES = ("delivered", "shipped")
SOURCES = {
    OLIST: "Olist v2 · 公开历史订单",
    AMAZON: "Amazon 美国站 · FBM",
}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS order_shops (
        source TEXT NOT NULL, shop_id TEXT NOT NULL,
        PRIMARY KEY (source, shop_id)
    )""",
    """CREATE TABLE IF NOT EXISTS order_shop_access (
        source TEXT NOT NULL, shop_id TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
        PRIMARY KEY (source, shop_id, user_id),
        FOREIGN KEY (source, shop_id) REFERENCES order_shops(source, shop_id)
    )""",
    """CREATE TABLE IF NOT EXISTS shop_orders (
        source TEXT NOT NULL, shop_id TEXT NOT NULL, order_id TEXT NOT NULL,
        status TEXT, ordered_at TEXT, source_updated_at TEXT, synced_at TEXT NOT NULL,
        ship_by TEXT, carrier_at TEXT, mixed_seller INTEGER,
        PRIMARY KEY (source, shop_id, order_id),
        FOREIGN KEY (source, shop_id) REFERENCES order_shops(source, shop_id)
    )""",
    """CREATE TABLE IF NOT EXISTS shop_order_items (
        source TEXT NOT NULL, shop_id TEXT NOT NULL, order_id TEXT NOT NULL, item_id TEXT NOT NULL,
        sku TEXT, product_id TEXT, quantity INTEGER, ship_by TEXT,
        PRIMARY KEY (source, shop_id, order_id, item_id),
        FOREIGN KEY (source, shop_id, order_id)
            REFERENCES shop_orders(source, shop_id, order_id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS shop_orders_by_date
        ON shop_orders(source, shop_id, ordered_at DESC, order_id DESC)""",
    """CREATE TABLE IF NOT EXISTS order_ingestion_runs (
        id INTEGER PRIMARY KEY, source TEXT NOT NULL, shop_id TEXT NOT NULL, operation TEXT NOT NULL,
        outcome TEXT NOT NULL, finished_at TEXT NOT NULL, error_code TEXT, upstream_status INTEGER,
        window_start TEXT, window_end TEXT, pages INTEGER, orders_written INTEGER, items_written INTEGER,
        FOREIGN KEY (source, shop_id) REFERENCES order_shops(source, shop_id)
    )""",
    """CREATE INDEX IF NOT EXISTS order_runs_by_shop
        ON order_ingestion_runs(source, shop_id, id DESC)""",
)


class Item(NamedTuple):
    item_id: str
    sku: str | None
    product_id: str | None
    quantity: int | None
    ship_by: str | None


class Order(NamedTuple):
    order_id: str
    status: str | None
    ordered_at: str | None
    source_updated_at: str | None
    ship_by: str | None
    carrier_at: str | None
    mixed_seller: bool | None
    items: tuple[Item, ...]


def utc_text(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def init_schema(conn):
    for statement in SCHEMA:
        conn.execute(statement)


def has_access(conn, user_id, source, shop_id):
    return conn.execute(
        "SELECT 1 FROM order_shop_access WHERE source=? AND shop_id=? AND user_id=?",
        (source, shop_id, user_id),
    ).fetchone() is not None


def shops(conn, user_id):
    return [
        {**dict(row), "source_label": SOURCES[row["source"]]}
        for row in conn.execute(
            "SELECT source, shop_id FROM order_shop_access WHERE user_id=? ORDER BY source, shop_id",
            (user_id,),
        )
    ]


def list_orders(conn, source, shop_id, *, status, search, limit, offset):
    scope = (source, shop_id)
    filters = "source=? AND shop_id=?"
    params = list(scope)
    if status is not None:
        filters += " AND status=?"
        params.append(status)
    if search:
        filters += " AND instr(order_id, ?) > 0"
        params.append(search)
    totals = conn.execute(
        """SELECT count(*) AS orders,
            (SELECT count(*) FROM shop_order_items WHERE source=? AND shop_id=?) AS items
            FROM shop_orders WHERE source=? AND shop_id=?""",
        (*scope, *scope),
    ).fetchone()
    rows = conn.execute(
        f"""SELECT order_id, status, ordered_at, source_updated_at, synced_at, ship_by, carrier_at, mixed_seller,
            (SELECT count(*) FROM shop_order_items i
             WHERE i.source=o.source AND i.shop_id=o.shop_id AND i.order_id=o.order_id) AS item_count
            FROM shop_orders o WHERE {filters} ORDER BY ordered_at DESC, order_id DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    )
    return {
        "source": source, "shop_id": shop_id, "source_label": SOURCES[source],
        "totals": dict(totals),
        "matching_orders": conn.execute(f"SELECT count(*) FROM shop_orders WHERE {filters}", params).fetchone()[0],
        "statuses": [row[0] for row in conn.execute(
            "SELECT DISTINCT status FROM shop_orders WHERE source=? AND shop_id=? AND status IS NOT NULL ORDER BY status",
            scope,
        )],
        "orders": [dict(row) for row in rows],
        "runs": [dict(row) for row in conn.execute(
            """SELECT id, operation, outcome, finished_at, error_code, upstream_status, window_start, window_end,
                pages, orders_written, items_written
                FROM order_ingestion_runs WHERE source=? AND shop_id=? ORDER BY id DESC LIMIT 10""",
            scope,
        )],
    }


def read_order(conn, source, shop_id, order_id):
    scope = (source, shop_id, order_id)
    row = conn.execute(
        "SELECT * FROM shop_orders WHERE source=? AND shop_id=? AND order_id=?", scope,
    ).fetchone()
    if row is None:
        return None
    return {
        **dict(row), "source_label": SOURCES[source],
        "items": [dict(item) for item in conn.execute(
            """SELECT item_id, sku, product_id, quantity, ship_by FROM shop_order_items
                WHERE source=? AND shop_id=? AND order_id=? ORDER BY length(item_id), item_id""", scope,
        )],
    }


def olist_sales_products(conn, shop_id):
    return [dict(row) for row in conn.execute(
        """SELECT i.product_id, min(substr(o.ordered_at, 1, 10)) AS first_date,
                  max(substr(o.ordered_at, 1, 10)) AS last_date
           FROM shop_order_items i JOIN shop_orders o USING (source, shop_id, order_id)
           WHERE i.source=? AND i.shop_id=? AND i.product_id IS NOT NULL AND o.ordered_at IS NOT NULL
           GROUP BY i.product_id ORDER BY count(*) DESC, i.product_id""",
        (OLIST, shop_id),
    )]


def has_olist_product(conn, shop_id, product_id):
    return conn.execute(
        "SELECT 1 FROM shop_order_items WHERE source=? AND shop_id=? AND product_id=? LIMIT 1",
        (OLIST, shop_id, product_id),
    ).fetchone() is not None


def olist_sales(conn, shop_id, product_id, baseline, current, status):
    """Count selected seller's items on the original purchase calendar day."""
    included = OLIST_SALES_STATUSES if status is None else (status,)
    placeholders = ",".join("?" for _ in included)
    rows = conn.execute(
        f"""SELECT substr(o.ordered_at, 1, 10) AS day, sum(i.quantity) AS units,
                   count(DISTINCT o.order_id) AS orders
            FROM shop_order_items i JOIN shop_orders o USING (source, shop_id, order_id)
            WHERE i.source=? AND i.shop_id=? AND i.product_id=? AND o.status IN ({placeholders})
                AND (substr(o.ordered_at, 1, 10) BETWEEN ? AND ?
                     OR substr(o.ordered_at, 1, 10) BETWEEN ? AND ?)
            GROUP BY day""",
        (OLIST, shop_id, product_id, *included, *(day.isoformat() for day in (*baseline, *current))),
    )
    daily = {row["day"]: (row["units"], row["orders"]) for row in rows}

    def window(start: date, end: date):
        points = []
        for offset in range((end - start).days + 1):
            day = (start + timedelta(days=offset)).isoformat()
            units, count = daily.get(day, (0, 0))
            points.append({"date": day, "units": units, "orders": count})
        return {
            "start_date": start.isoformat(), "end_date": end.isoformat(), "points": points,
            "totals": {metric: sum(point[metric] for point in points) for metric in ("units", "orders")},
        }

    before, after = window(*baseline), window(*current)
    change = {}
    for metric, old in before["totals"].items():
        delta = after["totals"][metric] - old
        change[metric] = {"absolute": delta, "relative_pct": 100 * delta / old if old else None}
    return {
        "source": OLIST, "source_label": SOURCES[OLIST], "shop_id": shop_id,
        "product_id": product_id, "included_statuses": list(included), "date_basis": "olist_purchase_date",
        "baseline": before, "current": after, "change": change,
    }


def write_orders(conn, source, shop_id, records, *, synced_at, replace_snapshot=False):
    """Caller owns the transaction. Replace each accepted order and its lines together."""
    scope = (source, shop_id)
    if replace_snapshot:
        conn.execute("DELETE FROM shop_orders WHERE source=? AND shop_id=?", scope)
    orders_written = items_written = 0
    for order in records:
        key = (*scope, order.order_id)
        changed = conn.execute(
            """INSERT INTO shop_orders
                (source, shop_id, order_id, status, ordered_at, source_updated_at, ship_by, carrier_at, mixed_seller, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, shop_id, order_id) DO UPDATE SET
                    status=excluded.status, ordered_at=excluded.ordered_at, source_updated_at=excluded.source_updated_at,
                    ship_by=excluded.ship_by, carrier_at=excluded.carrier_at, mixed_seller=excluded.mixed_seller,
                    synced_at=excluded.synced_at
                WHERE excluded.source_updated_at > shop_orders.source_updated_at
                   OR (excluded.source_updated_at IS NOT NULL AND shop_orders.source_updated_at IS NULL)""",
            (*scope, *order[:-1], synced_at),
        ).rowcount
        if changed:
            conn.execute("DELETE FROM shop_order_items WHERE source=? AND shop_id=? AND order_id=?", key)
            conn.executemany(
                """INSERT INTO shop_order_items
                    (source, shop_id, order_id, item_id, sku, product_id, quantity, ship_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ((*key, *item) for item in order.items),
            )
            orders_written += 1
            items_written += len(order.items)
    return orders_written, items_written


def record_run(conn, source, shop_id, operation, *, error_code=None, upstream_status=None,
               window=(None, None), pages=None, written=(None, None)):
    finished_at = utc_text(datetime.now(UTC))
    outcome = "failed" if error_code else "succeeded"
    run = conn.execute(
        """INSERT INTO order_ingestion_runs
            (source, shop_id, operation, outcome, finished_at, error_code, upstream_status,
             window_start, window_end, pages, orders_written, items_written)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, shop_id, operation, outcome, finished_at, error_code, upstream_status, *window, pages, *written),
    )
    return {
        "id": run.lastrowid, "operation": operation, "outcome": outcome, "finished_at": finished_at,
        "error_code": error_code, "upstream_status": upstream_status, "window_start": window[0],
        "window_end": window[1], "pages": pages, "orders_written": written[0], "items_written": written[1],
    }
