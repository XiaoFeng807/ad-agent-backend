"""Durable delivery of committed order and report changes to the B executor."""

import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.database import background_inspections as inspections, chat_groups
from backend.database import listing_operations as listings, order_sales, orders
from backend.observability import add_trace_step, end_trace, start_trace


def upgrade_sqlite(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS inspection_subscriptions (
        subscription_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        conversation_id TEXT NOT NULL REFERENCES chat_groups(conversation_id),
        source TEXT NOT NULL, shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL,
        windows TEXT NOT NULL, allow_listing_proposal INTEGER NOT NULL CHECK (allow_listing_proposal IN (0, 1)),
        order_after INTEGER NOT NULL DEFAULT 0, report_after INTEGER NOT NULL DEFAULT 0,
        order_basis TEXT,
        UNIQUE (user_id, conversation_id, source, shop_id, marketplace_id, sku, windows)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS inspection_subscription_deliveries (
        subscription_id TEXT NOT NULL REFERENCES inspection_subscriptions(subscription_id),
        kind TEXT NOT NULL CHECK (kind IN ('order', 'report')), position INTEGER NOT NULL, event_id TEXT NOT NULL,
        publication_id TEXT, outcome TEXT NOT NULL, run_id TEXT REFERENCES background_inspections(run_id),
        trace_id TEXT NOT NULL, PRIMARY KEY (subscription_id, kind, position)
    )""")


def _visible(conn, user_id, source, scope):
    return orders.has_access(conn, user_id, source, scope[0]) and not listings.scope_error(conn, user_id, *scope)


def register(user_id, conversation_id, source, target, windows, allow_listing_proposal):
    scope = listings._key(target)
    if source not in orders.SOURCES:
        raise listings.ListingInvalid("invalid_order_source")
    b_start, b_end, c_start, c_end = map(listings._date, windows)
    if not 0 <= (b_end - b_start).days == (c_end - c_start).days < 90 or b_end >= c_start:
        raise listings.ListingInvalid("invalid_listing_sales_window")
    with chat_groups._write() as conn:
        group = chat_groups._group(conn, user_id, conversation_id)
        if group["archived"]:
            raise chat_groups.GroupConflict("请先恢复归档工作群")
        if not _visible(conn, user_id, source, scope):
            raise listings.ListingNotFound
        if c_end >= chat_groups.alert_review.now().astimezone(ZoneInfo(listings.SALES_TIMEZONE)).date():
            raise listings.ListingInvalid("请提供两个已结束的销量比较窗口")
        scope_key = (user_id, conversation_id, source, *scope, inspections._json(windows))
        conn.execute("""INSERT INTO inspection_subscriptions
            (subscription_id,user_id,conversation_id,source,shop_id,marketplace_id,sku,windows,allow_listing_proposal)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,conversation_id,source,shop_id,marketplace_id,sku,windows)
            DO NOTHING""", (uuid4().hex, *scope_key, allow_listing_proposal))
        row = conn.execute("""SELECT subscription_id,allow_listing_proposal FROM inspection_subscriptions
            WHERE (user_id,conversation_id,source,shop_id,marketplace_id,sku,windows)=(?,?,?,?,?,?,?)""",
                           scope_key).fetchone()
        if bool(row["allow_listing_proposal"]) != allow_listing_proposal:
            raise chat_groups.GroupConflict("inspection_subscription_actions_conflict")
        return row["subscription_id"]


_ORDER_FEED = """FROM listing_order_sales_snapshots s JOIN order_sales_publications p USING(snapshot_id)
    WHERE (s.shop_id,s.marketplace_id,s.sku,s.source)=(?,?,?,?) AND p.sequence>?"""


def _work(conn):
    return [row["subscription_id"] for row in conn.execute("""SELECT a.subscription_id
        FROM inspection_subscriptions a JOIN chat_groups g USING(conversation_id)
        JOIN order_shop_access o ON (o.source,o.shop_id,o.user_id)=(a.source,a.shop_id,a.user_id)
        JOIN listing_test_shops l ON (l.shop_id,l.marketplace_id,l.user_id)=(a.shop_id,a.marketplace_id,a.user_id)
        JOIN listing_test_products product ON (product.shop_id,product.marketplace_id,product.sku)=
            (a.shop_id,a.marketplace_id,a.sku)
        WHERE g.user_id=a.user_id AND NOT g.archived AND (
            EXISTS (SELECT 1 FROM listing_order_sales_snapshots s JOIN order_sales_publications p USING(snapshot_id)
                WHERE (s.shop_id,s.marketplace_id,s.sku,s.source)=(a.shop_id,a.marketplace_id,a.sku,a.source)
                AND p.sequence>a.order_after)
            OR EXISTS (SELECT 1 FROM search_term_report_imports r
                WHERE r.conversation_id=a.conversation_id AND r.version>a.report_after))
        ORDER BY a.rowid""")]


def _order_basis(publication, windows):
    b_start, b_end, c_start, c_end = windows

    def included(day):
        return b_start <= day <= b_end or c_start <= day <= c_end

    evidence = publication["evidence"]
    series = {day: units for day, units in publication["series"].items() if included(day)}
    coverage = sorted(day for day in evidence["coverage"]["days"] if included(day))
    unknown = [day for day in evidence["unverified_days"] if included(day)]
    if not (series or coverage or unknown):
        return None
    inputs = sorted(row["input_id"] for row in evidence["inputs"] if included(row["day"]))
    return listings._digest([series, coverage, unknown, inputs, evidence["counted_statuses"],
                             evidence["mapping"], evidence["timezone"]])


def _next_order(conn, subscription, scope):
    return conn.execute("SELECT p.sequence AS position,p.publication_id AS event_id " + _ORDER_FEED
                        + " ORDER BY p.sequence LIMIT 1", (*scope, subscription["source"],
                                                           subscription["order_after"])).fetchone()


def _next_report(conn, subscription):
    return conn.execute("""SELECT version AS position,import_id AS event_id,manifest
        FROM search_term_report_imports WHERE conversation_id=? AND version>? ORDER BY version LIMIT 1""",
                        (subscription["conversation_id"], subscription["report_after"])).fetchone()


def _current_publication(conn, scope):
    return conn.execute("""SELECT p.publication_id,s.source FROM listing_order_sales_snapshots s
        LEFT JOIN order_sales_publications p USING(snapshot_id)
        WHERE (s.shop_id,s.marketplace_id,s.sku)=(?,?,?) ORDER BY s.version DESC LIMIT 1""", scope).fetchone()


def _deliver(conn, subscription, group, kind, event, scope, model):
    windows = json.loads(subscription["windows"])
    order_basis = subscription["order_basis"]
    publication_id = None
    run_id = None
    trace_id = None
    manifest = json.loads(event["manifest"]) if kind == "report" else None
    current = _current_publication(conn, scope)
    if current is not None and current["source"] == subscription["source"]:
        publication_id = current["publication_id"]
    if kind == "order" and publication_id != event["event_id"]:
        outcome = "superseded"
        publication_id = event["event_id"]
    elif manifest and ((manifest["shop_id"], manifest["marketplace"]) != scope[:2]
                       or manifest["end_date"] < windows[-2] or manifest["start_date"] > windows[-1]):
        outcome = "report_unused"
    elif publication_id is None:
        outcome = "no_publication"
    else:
        if conn.execute("""SELECT 1 FROM order_sales_pending
            WHERE (shop_id,marketplace_id,sku)=(?,?,?)""", scope).fetchone():
            return None
        record = order_sales.read_publication(conn, subscription["user_id"], publication_id)
        if record is None:
            return None
        next_basis = _order_basis(record, windows) if kind == "order" else order_basis
        if kind == "order" and next_basis == order_basis:
            outcome = "outside_window"
        else:
            trace_id = start_trace(user_id=subscription["user_id"], source="automatic_listing_publication")
            try:
                run_id, created = inspections._accept_publication(
                    conn, group, publication_id, windows, True, model, record, trace_id, None,
                    expected_report=event["event_id"] if kind == "report" else None,
                    allow_listing_proposal=bool(subscription["allow_listing_proposal"]),
                    subscription_id=subscription["subscription_id"])
                outcome = "accepted" if created else "replayed"
            except inspections.PublicationReportUnused as error:
                outcome = str(error)
            except (chat_groups.GroupConflict, chat_groups.GroupCapacityExceeded):
                end_trace(trace_id, "deferred")
                raise
            except BaseException:
                end_trace(trace_id, "failed")
                raise
        order_basis = next_basis
    subscription_id = subscription["subscription_id"]
    trace_id = trace_id or start_trace(user_id=subscription["user_id"], source="automatic_listing_publication")
    conn.execute("""INSERT INTO inspection_subscription_deliveries
        (subscription_id,kind,position,event_id,publication_id,outcome,run_id,trace_id) VALUES (?,?,?,?,?,?,?,?)""",
                 (subscription_id, kind, event["position"], event["event_id"], publication_id, outcome, run_id, trace_id))
    order_after = event["position"] if kind == "order" else subscription["order_after"]
    report_after = event["position"] if kind == "report" else subscription["report_after"]
    conn.execute("""UPDATE inspection_subscriptions SET order_after=?,report_after=?,order_basis=?
        WHERE subscription_id=?""", (order_after, report_after, order_basis, subscription_id))
    return {"subscription_id": subscription_id, "kind": kind, "position": event["position"],
            "event_id": event["event_id"], "publication_id": publication_id, "outcome": outcome,
            "run_id": run_id, "trace_id": trace_id}


def _consume(subscription_id, model):
    with chat_groups._write() as conn:
        row = conn.execute("SELECT * FROM inspection_subscriptions WHERE subscription_id=?",
                           (subscription_id,)).fetchone()
        scope = tuple(row[key] for key in listings._TARGET_KEYS)
        group = chat_groups._group(conn, row["user_id"], row["conversation_id"])
        if (group["archived"] or chat_groups._running(conn, row["conversation_id"])
                or not _visible(conn, row["user_id"], row["source"], scope)):
            return None
        kind, event = "order", _next_order(conn, row, scope)
        if event is None:
            kind, event = "report", _next_report(conn, row)
        if event is None:
            return None
        delivery = conn.execute("""SELECT * FROM inspection_subscription_deliveries
            WHERE (subscription_id,kind,position)=(?,?,?)""", (subscription_id, kind, event["position"])).fetchone()
        if delivery is None:
            return _deliver(conn, row, group, kind, event, scope, model)
        cursor = "order_after" if kind == "order" else "report_after"
        conn.execute(f"UPDATE inspection_subscriptions SET {cursor}=? WHERE subscription_id=?",
                     (event["position"], subscription_id))
        return {**dict(delivery), "outcome": "duplicate",
                "trace_id": start_trace(user_id=row["user_id"], source="automatic_listing_publication")}


def consume(model):
    """Advance one event per eligible subscription, with B admission in the same write."""
    with inspections._read() as conn:
        subscriptions = _work(conn)
    progressed = False
    for subscription_id in subscriptions:
        try:
            delivery = _consume(subscription_id, model)
        except (chat_groups.GroupNotFound, chat_groups.GroupConflict, chat_groups.GroupCapacityExceeded):
            continue
        if delivery:
            progressed = True
            trace_id = delivery.pop("trace_id")
            add_trace_step(trace_id, "publication_delivery", delivery["outcome"], evidence=delivery)
            end_trace(trace_id, delivery["outcome"])
    return progressed
