"""Trusted order-to-Listing projection and its committed publication feed."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from backend.database import listing_operations as listing, orders


COUNTED_STATUSES = ("Shipped", "SHIPPED")
TARGET_FIELDS = ("shop_id", "marketplace_id", "sku")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS order_sales_sites (
        source TEXT NOT NULL, shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, timezone TEXT NOT NULL,
        PRIMARY KEY (source, shop_id, marketplace_id), UNIQUE (shop_id, marketplace_id),
        FOREIGN KEY (source, shop_id) REFERENCES order_shops (source, shop_id),
        FOREIGN KEY (shop_id, marketplace_id) REFERENCES listing_test_shops (shop_id, marketplace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS order_sales_mappings (
        source TEXT NOT NULL, shop_id TEXT NOT NULL, product_id TEXT NOT NULL, sku TEXT NOT NULL,
        marketplace_id TEXT NOT NULL,
        PRIMARY KEY (source, shop_id, product_id, sku),
        FOREIGN KEY (source, shop_id, marketplace_id) REFERENCES order_sales_sites (source, shop_id, marketplace_id),
        FOREIGN KEY (shop_id, marketplace_id, sku) REFERENCES listing_test_products (shop_id, marketplace_id, sku)
    )""",
    """CREATE TABLE IF NOT EXISTS order_sales_coverage (
        source TEXT NOT NULL, shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, as_of TEXT NOT NULL,
        declared_by_user_id INTEGER NOT NULL REFERENCES users(id), declaration TEXT NOT NULL,
        PRIMARY KEY (source, shop_id, marketplace_id, as_of),
        FOREIGN KEY (source, shop_id, marketplace_id) REFERENCES order_sales_sites (source, shop_id, marketplace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS order_sales_inputs (
        id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES order_ingestion_runs(id),
        source TEXT NOT NULL, shop_id TEXT NOT NULL, order_id TEXT NOT NULL, source_updated_at TEXT NOT NULL,
        stored_updated_at TEXT, outcome TEXT NOT NULL, original TEXT NOT NULL, origin TEXT NOT NULL, issues TEXT NOT NULL,
        FOREIGN KEY (source, shop_id) REFERENCES order_shops (source, shop_id)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS order_sales_inputs_accepted
        ON order_sales_inputs (source, shop_id, order_id, stored_updated_at) WHERE stored_updated_at IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS order_sales_inputs_batch ON order_sales_inputs (batch_id, id)""",
    """CREATE TABLE IF NOT EXISTS order_sales_publications (
        sequence INTEGER PRIMARY KEY, publication_id TEXT NOT NULL UNIQUE,
        snapshot_id TEXT NOT NULL UNIQUE REFERENCES listing_order_sales_snapshots(snapshot_id),
        batch_id INTEGER NOT NULL REFERENCES order_ingestion_runs(id),
        published_by_user_id INTEGER NOT NULL REFERENCES users(id),
        evidence TEXT NOT NULL, removed TEXT NOT NULL
    )""",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("order_timestamp_requires_offset")
    return result.astimezone(UTC)


def _target(shop_id, marketplace_id, sku):
    return dict(zip(TARGET_FIELDS, (shop_id, marketplace_id, sku)))


def register_site(conn, user_id, source, shop_id, marketplace_id, *, timezone, mappings, coverage):
    """Register trusted server configuration in a caller-owned immediate transaction.

    SKU identity and the site's calendar are immutable. Coverage declarations are
    full replacements ordered by their source cutoff. Empty days withdraw coverage.
    """
    if not conn.in_transaction:
        raise ValueError("order_sales_registration_requires_transaction")
    if source != orders.AMAZON:
        raise ValueError("unsupported_order_sales_source")
    if not orders.has_access(conn, user_id, source, shop_id):
        raise PermissionError("order_sales_scope_unreadable")
    if not mappings or not all(isinstance(sku, str) and sku.strip() and len(sku) <= 128
                               and (product is None or isinstance(product, str) and product.strip())
                               for product, sku in mappings):
        raise ValueError("invalid_order_sales_mapping")
    if any(listing.scope_error(conn, user_id, shop_id, marketplace_id, sku) for _, sku in mappings):
        raise PermissionError("order_sales_scope_unreadable")

    zone = ZoneInfo(timezone)
    as_of = _timestamp(coverage["as_of"])
    as_of_text = orders.utc_text(as_of)
    reference = coverage["reference"]
    checksum = coverage["sha256"]
    days = coverage["days"]
    if (not isinstance(reference, str) or not reference.strip()
            or not isinstance(checksum, str) or len(checksum) != 64
            or any(char not in "0123456789abcdef" for char in checksum)
            or not isinstance(days, dict)):
        raise ValueError("invalid_order_sales_coverage")
    for day, line in days.items():
        parsed = date.fromisoformat(day)
        if (parsed.isoformat() != day or type(line) is not int or line < 1
                or datetime.combine(parsed + timedelta(days=1), time.min, zone) > as_of):
            raise ValueError("invalid_order_sales_coverage_day")

    scope = (source, shop_id, marketplace_id)
    site = conn.execute("SELECT timezone FROM order_sales_sites WHERE source=? AND shop_id=? AND marketplace_id=?",
                        scope).fetchone()
    if site is None:
        conn.execute("INSERT INTO order_sales_sites VALUES (?,?,?,?)", (*scope, timezone))
    elif site["timezone"] != timezone:
        raise ValueError("order_sales_site_timezone_conflict")

    for product, sku in mappings:
        key = (source, shop_id, product or "", sku)
        conn.execute("INSERT INTO order_sales_mappings VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", (*key, marketplace_id))
        mapped = conn.execute("""SELECT marketplace_id FROM order_sales_mappings
            WHERE source=? AND shop_id=? AND product_id=? AND sku=?""", key).fetchone()
        if mapped["marketplace_id"] != marketplace_id:
            raise ValueError("ambiguous_order_sales_mapping")

    declaration = _json({"as_of": as_of_text, "reference": reference, "sha256": checksum, "days": days})
    old = conn.execute("""SELECT as_of,declaration FROM order_sales_coverage
        WHERE source=? AND shop_id=? AND marketplace_id=? ORDER BY as_of DESC LIMIT 1""", scope).fetchone()
    if old is None or as_of_text > old["as_of"]:
        conn.execute("INSERT INTO order_sales_coverage VALUES (?,?,?,?,?,?)", (*scope, as_of_text, user_id, declaration))
    elif old["as_of"] != as_of_text or old["declaration"] != declaration:
        raise ValueError("stale_or_conflicting_order_sales_coverage")


def _normalized(order):
    return order._replace(
        ordered_at=orders.utc_text(_timestamp(order.ordered_at)) if order.ordered_at else None,
        source_updated_at=orders.utc_text(_timestamp(order.source_updated_at)),
        items=tuple(sorted(order.items, key=lambda item: item.item_id)),
    )


def _stored_order(conn, scope, order_id):
    row = orders.read_order(conn, *scope, order_id)
    if row is None:
        return None, None
    old = orders.Order(*(row[field] for field in orders.Order._fields[:-1]),
                       tuple(orders.Item(**item) for item in row["items"]))
    return (_normalized(old) if old.source_updated_at else old), row["source_updated_at"]


def _order_json(order):
    return {**order._asdict(), "items": [item._asdict() for item in order.items]}


def _select_inputs(conn, scope, records, cutoff):
    grouped = defaultdict(list)
    for record in records:
        order, origin = record if isinstance(record, orders.FetchedOrder) else (record, {"kind": "normalized_source"})
        order = _normalized(order)
        grouped[order.order_id].append((order, origin))

    for order_id, candidates in sorted(grouped.items()):
        previous, stored_stamp = _stored_order(conn, scope, order_id)
        prior = previous.source_updated_at if previous else None
        latest = max((order.source_updated_at for order, _ in candidates if order.source_updated_at <= cutoff),
                     default="")
        top = {order for order, _ in candidates if order.source_updated_at == latest}
        traced = stored_stamp is not None and conn.execute("""SELECT 1 FROM order_sales_inputs
            WHERE source=? AND shop_id=? AND order_id=? AND stored_updated_at=?""",
            (*scope, order_id, stored_stamp)).fetchone() is not None
        accepted_new = False
        for order, origin in candidates:
            revision = order.source_updated_at
            accepted_stamp = None
            if revision > cutoff:
                outcome = "after_cutoff"
            elif prior is not None and revision < prior:
                outcome = "stale_revision"
            elif revision == prior:
                if order != previous:
                    outcome = "conflicting_revision"
                elif traced:
                    outcome = "duplicate_revision"
                else:
                    outcome, accepted_stamp, traced = "verified_existing", stored_stamp, True
            elif revision != latest:
                outcome = "superseded_in_batch"
            elif len(top) > 1:
                outcome = "conflicting_revision"
            elif accepted_new:
                outcome = "duplicate_revision"
            else:
                outcome, accepted_stamp = "accepted", revision
                accepted_new = True
            yield order, origin, outcome, accepted_stamp


def _projection_config(conn, user_id, scope, horizon):
    sites = {}
    for row in conn.execute("""SELECT s.marketplace_id,s.timezone,o.user_id,c.declared_by_user_id,c.declaration
        FROM order_sales_sites s JOIN listing_test_shops o USING (shop_id,marketplace_id)
        LEFT JOIN order_sales_coverage c ON c.source=s.source AND c.shop_id=s.shop_id
            AND c.marketplace_id=s.marketplace_id AND c.as_of=(SELECT max(x.as_of)
                FROM order_sales_coverage x WHERE x.source=s.source AND x.shop_id=s.shop_id
                    AND x.marketplace_id=s.marketplace_id AND x.as_of<=?)
        WHERE s.source=? AND s.shop_id=?""", (horizon, *scope)):
        coverage = json.loads(row["declaration"]) if row["declaration"] else None
        if coverage is not None:
            coverage["declared_by_user_id"] = row["declared_by_user_id"]
        sites[row["marketplace_id"]] = (row["user_id"] == user_id, ZoneInfo(row["timezone"]), coverage)
    mappings = {(row["product_id"], row["sku"]): row["marketplace_id"] for row in conn.execute(
        "SELECT product_id,sku,marketplace_id FROM order_sales_mappings WHERE source=? AND shop_id=?", scope)}
    return sites, mappings


def _resolution(sites, mappings, order, item, *, readable_only=True):
    site = mappings.get((item.product_id or "", item.sku))
    if site is None:
        return None, None, "unmapped_item"
    readable, zone, coverage = sites[site]
    if readable_only and not readable:
        return None, None, "listing_unreadable"
    if order.status not in COUNTED_STATUSES:
        return site, None, "status_not_counted"
    if order.ordered_at is None or item.quantity is None or item.quantity < 0:
        return site, None, "missing_sales_fields"
    day = _timestamp(order.ordered_at).astimezone(zone).date().isoformat()
    if coverage is None or day not in coverage["days"]:
        return site, day, "day_not_covered"
    return site, day, None


def import_sync(conn, user_id, source, shop_id, records, *, window, pages):
    """Commit normalized originals, effective orders, snapshots and notifications together."""
    scope = source, shop_id
    if not conn.in_transaction or not orders.has_access(conn, user_id, *scope):
        raise PermissionError("order_sales_import_scope_unreadable")
    cutoff = window[1]
    selected = list(_select_inputs(conn, scope, records, cutoff))
    accepted = [order for order, _, outcome, _ in selected if outcome == "accepted"]
    written = orders.write_orders(conn, *scope, accepted, synced_at=orders.utc_text(datetime.now(UTC)))
    if written != (len(accepted), sum(len(order.items) for order in accepted)):
        raise orders.IngestionConflict("order_sales_storage_conflict")
    batch = orders.record_run(conn, *scope, "sync", window=window, pages=pages, written=written)
    horizon = conn.execute("""SELECT max(window_end) FROM order_ingestion_runs WHERE source=? AND shop_id=?
        AND operation='sync' AND outcome='succeeded'""", scope).fetchone()[0]
    sites, mappings = _projection_config(conn, user_id, scope, horizon)
    for order, origin, outcome, accepted_stamp in selected:
        issues = {item.item_id: reason for item in order.items
                  if (reason := _resolution(sites, mappings, order, item)[2]) is not None}
        conn.execute("""INSERT INTO order_sales_inputs
            (batch_id,source,shop_id,order_id,source_updated_at,stored_updated_at,outcome,original,origin,issues)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (batch["id"], *scope, order.order_id, order.source_updated_at,
                                            accepted_stamp, outcome, _json(_order_json(order)), _json(origin), _json(issues)))
    _publish(conn, user_id, scope, sites, mappings, batch["id"])
    return batch


def _current_lines(conn, scope):
    return conn.execute("""SELECT o.order_id,o.status,o.ordered_at,o.source_updated_at,
            i.item_id,i.product_id,i.sku,i.quantity,i.ship_by,a.id AS input_id,a.batch_id,a.origin
        FROM shop_orders o JOIN shop_order_items i USING (source,shop_id,order_id)
        LEFT JOIN order_sales_inputs a ON a.source=o.source AND a.shop_id=o.shop_id AND a.order_id=o.order_id
            AND a.stored_updated_at=o.source_updated_at AND a.stored_updated_at IS NOT NULL
        WHERE o.source=? AND o.shop_id=? ORDER BY o.order_id,i.item_id""", scope)


def _removed(conn, scope, previous, inputs):
    if previous is None:
        return []
    current = {(line["input_id"], line["item_id"], line["day"]) for line in inputs}
    result = []
    for old in json.loads(previous["evidence"])["inputs"]:
        if (old["input_id"], old["item_id"], old["day"]) in current:
            continue
        replacement = conn.execute("""SELECT a.id AS input_id,a.batch_id,a.source_updated_at
            FROM shop_orders o JOIN order_sales_inputs a ON a.source=o.source AND a.shop_id=o.shop_id
                AND a.order_id=o.order_id AND a.stored_updated_at=o.source_updated_at
            WHERE o.source=? AND o.shop_id=? AND o.order_id=? AND a.stored_updated_at IS NOT NULL""",
            (*scope, old["order_id"])).fetchone()
        result.append({"previous": old, "replacement": dict(replacement) if replacement else None})
    return result


def _publish(conn, user_id, scope, sites, mappings, batch_id):
    source, shop_id = scope
    configured = defaultdict(list)
    for (product, sku), site in mappings.items():
        _, _, coverage = sites[site]
        if coverage is not None:
            configured[site, sku].append(product or None)
    if not configured:
        return

    inputs = defaultdict(list)
    unverified = defaultdict(set)
    for row in _current_lines(conn, scope):
        order = orders.Order(row["order_id"], row["status"], row["ordered_at"], row["source_updated_at"],
                             None, None, None, ())
        item = orders.Item(*(row[field] for field in orders.Item._fields))
        site, day, issue = _resolution(sites, mappings, order, item, readable_only=False)
        if issue is not None:
            continue
        key = site, item.sku
        if row["input_id"] is None:
            unverified[key].add(day)
        else:
            origin = json.loads(row["origin"])
            inputs[key].append({**item._asdict(), "day": day, "order_id": order.order_id, "status": order.status,
                                "ordered_at": order.ordered_at, "source_updated_at": order.source_updated_at,
                                "input_id": row["input_id"], "batch_id": row["batch_id"], "origin": origin})

    for (site, sku), products in sorted(configured.items()):
        key = site, sku
        target = _target(shop_id, site, sku)
        readable, zone, coverage = sites[site]
        series = dict.fromkeys(coverage["days"].keys() - unverified[key], 0)
        lines = [line for line in inputs[key] if line["day"] in series]
        for line in lines:
            series[line["day"]] += line["quantity"]
        evidence = {"timezone": zone.key, "counted_statuses": COUNTED_STATUSES, "coverage": coverage,
                    "mapping": sorted(products, key=lambda value: value or ""), "inputs": lines,
                    "unverified_days": sorted(unverified[key])}
        source_revision = _digest(evidence)
        pending_key = (*scope, site, sku)
        previous = conn.execute("""SELECT s.source,s.source_revision,p.evidence FROM listing_order_sales_snapshots s
            LEFT JOIN order_sales_publications p USING (snapshot_id)
            WHERE s.shop_id=? AND s.marketplace_id=? AND s.sku=? ORDER BY s.version DESC LIMIT 1""",
            (shop_id, site, sku)).fetchone()
        if not readable and (previous is None or (previous["source"], previous["source_revision"]) != (source, source_revision)):
            conn.execute("""INSERT INTO order_sales_pending VALUES (?,?,?,?,?,?)
                ON CONFLICT(source,shop_id,marketplace_id,sku) DO UPDATE SET
                    source_revision=excluded.source_revision,batch_id=excluded.batch_id
                WHERE excluded.source_revision!=order_sales_pending.source_revision""",
                (*pending_key, source_revision, batch_id))
            continue
        conn.execute("DELETE FROM order_sales_pending WHERE source=? AND shop_id=? AND marketplace_id=? AND sku=?",
                     pending_key)
        if not readable:
            continue
        snapshot = listing.publish_order_sales(conn, user_id, target, source, series, source_revision=source_revision)
        snapshot_id = snapshot["snapshot_id"]
        exists = conn.execute("SELECT 1 FROM order_sales_publications WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if not exists:
            conn.execute("""INSERT INTO order_sales_publications
                (publication_id,snapshot_id,batch_id,published_by_user_id,evidence,removed) VALUES (?,?,?,?,?,?)""",
                (f"osp_{snapshot_id}", snapshot_id, batch_id, user_id, _json(evidence),
                 _json(_removed(conn, scope, previous if previous and previous["evidence"] else None, lines))))


_VISIBLE_PUBLICATIONS = """
    FROM order_sales_publications p JOIN listing_order_sales_snapshots s USING (snapshot_id)
    JOIN listing_test_shops l USING (shop_id,marketplace_id)
    JOIN order_ingestion_runs b ON b.id=p.batch_id
    JOIN order_shop_access a ON a.source=s.source AND a.shop_id=s.shop_id AND a.user_id=l.user_id
    WHERE l.user_id=?
"""
_PUBLICATION_COLUMNS = """p.sequence,p.publication_id,p.snapshot_id,p.batch_id,p.published_by_user_id,
    b.finished_at AS published_at,s.shop_id,s.marketplace_id,s.sku,s.version,s.source,s.digest,s.source_revision"""


def _publication(row):
    result = dict(row)
    result["target"] = {field: result.pop(field) for field in TARGET_FIELDS}
    return result


def list_publications(conn, user_id, *, after=0, limit=100):
    """List committed notifications visible to the current order and Listing reader."""
    return [_publication(row) for row in conn.execute(f"""SELECT {_PUBLICATION_COLUMNS}
        {_VISIBLE_PUBLICATIONS} AND p.sequence>? ORDER BY p.sequence LIMIT ?""", (user_id, after, limit))]


def read_publication(conn, user_id, publication_id):
    """Read the immutable snapshot and its scoped provenance under current authorization."""
    row = conn.execute(f"""SELECT {_PUBLICATION_COLUMNS},p.evidence,p.removed
        {_VISIBLE_PUBLICATIONS} AND p.publication_id=?""", (user_id, publication_id)).fetchone()
    if row is None:
        return None
    result = _publication(row)
    published = listing.read_order_sales_snapshot(conn, user_id, result["target"], result["snapshot_id"])
    evidence = json.loads(result.pop("evidence"))
    if published is None or _digest(evidence) != result["source_revision"]:
        return None
    batch = conn.execute("SELECT * FROM order_ingestion_runs WHERE id=?", (result["batch_id"],)).fetchone()
    return {**result, "series": published["series"], "batch": dict(batch), "evidence": evidence,
            "removed": json.loads(result["removed"])}


def read_import(conn, user_id, source, shop_id, batch_id, *, after=0, limit=100):
    if not orders.has_access(conn, user_id, source, shop_id):
        return None
    batch = conn.execute("SELECT * FROM order_ingestion_runs WHERE source=? AND shop_id=? AND id=?",
                         (source, shop_id, batch_id)).fetchone()
    if batch is None:
        return None
    inputs = []
    for row in conn.execute("""SELECT id,order_id,source_updated_at,stored_updated_at,outcome,original,origin,issues
        FROM order_sales_inputs WHERE batch_id=? AND id>? ORDER BY id LIMIT ?""", (batch_id, after, limit)):
        inputs.append({**dict(row), **{field: json.loads(row[field]) for field in ("original", "origin", "issues")}})
    return {"batch": dict(batch), "inputs": inputs}
