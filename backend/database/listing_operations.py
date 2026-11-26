"""Evidence-backed edits to explicitly seeded local test listings."""

import argparse
import hashlib
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.database import orders, product_documents
from backend.observability import add_trace_step


KIND = "listing_operations_v1"
INSPECTION_KIND = "listing_inspection_v1"
SELECTION_KIND = "listing_product_selection_v1"
SOURCE = "local_listing_test"
SALES_TIMEZONE = "Asia/Shanghai"
TEST_SHOP = "task42-test"
TEST_MARKETPLACE = "US"
TEST_SKU = "TASK42-BOTTLE"
TEST_BASELINE = ("2026-10-30", "2026-11-05")
TEST_CURRENT = ("2026-11-06", "2026-11-12")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS listing_test_shops (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
        PRIMARY KEY (shop_id, marketplace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS listing_test_products (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0), title TEXT NOT NULL, bullets TEXT NOT NULL,
        price_cents INTEGER NOT NULL, inventory_units INTEGER NOT NULL,
        PRIMARY KEY (shop_id, marketplace_id, sku),
        FOREIGN KEY (shop_id, marketplace_id) REFERENCES listing_test_shops (shop_id, marketplace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS listing_test_catalog_aliases (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL, alias TEXT NOT NULL,
        PRIMARY KEY (shop_id, marketplace_id, sku, alias),
        FOREIGN KEY (shop_id, marketplace_id) REFERENCES listing_test_shops (shop_id, marketplace_id)
    )""",
    """CREATE TABLE IF NOT EXISTS listing_test_facts (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0), facts TEXT NOT NULL,
        PRIMARY KEY (shop_id, marketplace_id, sku),
        FOREIGN KEY (shop_id, marketplace_id, sku) REFERENCES listing_test_products (shop_id, marketplace_id, sku)
    )""",
    """CREATE TABLE IF NOT EXISTS listing_test_sales (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0), series TEXT NOT NULL,
        PRIMARY KEY (shop_id, marketplace_id, sku),
        FOREIGN KEY (shop_id, marketplace_id, sku) REFERENCES listing_test_products (shop_id, marketplace_id, sku)
    )""",
    """CREATE TABLE IF NOT EXISTS listing_order_sales_snapshots (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0), snapshot_id TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL, digest TEXT NOT NULL, source_revision TEXT NOT NULL, series TEXT NOT NULL,
        PRIMARY KEY (shop_id, marketplace_id, sku, version),
        FOREIGN KEY (shop_id, marketplace_id, sku) REFERENCES listing_test_products (shop_id, marketplace_id, sku),
        FOREIGN KEY (source, shop_id) REFERENCES order_shops (source, shop_id)
    )""",
    """CREATE TABLE IF NOT EXISTS order_sales_pending (
        source TEXT NOT NULL, shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL,
        source_revision TEXT NOT NULL, batch_id INTEGER NOT NULL REFERENCES order_ingestion_runs(id),
        PRIMARY KEY (source,shop_id,marketplace_id,sku),
        FOREIGN KEY (source,shop_id) REFERENCES order_shops (source,shop_id),
        FOREIGN KEY (shop_id,marketplace_id,sku) REFERENCES listing_test_products (shop_id,marketplace_id,sku)
    )""",
    """CREATE TABLE IF NOT EXISTS listing_test_required_facts (
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL, fields TEXT NOT NULL,
        PRIMARY KEY (shop_id, marketplace_id, sku),
        FOREIGN KEY (shop_id, marketplace_id, sku) REFERENCES listing_test_products (shop_id, marketplace_id, sku)
    )""",
    """CREATE TABLE IF NOT EXISTS listing_change_proposals (
        action_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES chat_groups(conversation_id),
        run_id TEXT NOT NULL REFERENCES chat_group_runs(run_id),
        entry_id INTEGER NOT NULL UNIQUE REFERENCES chat_group_entries(id),
        shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL, snapshot TEXT NOT NULL,
        FOREIGN KEY (shop_id, marketplace_id, sku) REFERENCES listing_test_products (shop_id, marketplace_id, sku)
    )""",
    """CREATE INDEX IF NOT EXISTS listing_change_proposals_group ON listing_change_proposals(conversation_id)""",
    """CREATE TABLE IF NOT EXISTS listing_change_receipts (
        receipt_id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE REFERENCES listing_change_proposals(action_id),
        approved_by_user_id INTEGER NOT NULL REFERENCES users(id), approved_by_name TEXT NOT NULL,
        committed_version INTEGER NOT NULL, committed_bullets TEXT NOT NULL,
        execution_trace_id TEXT NOT NULL, approved_at TEXT NOT NULL
    )""",
)

_TARGET_KEYS = ("shop_id", "marketplace_id", "sku")
_WINDOW_KEYS = ("baseline_start", "baseline_end", "current_start", "current_end")
_ORIGINAL_BULLETS = ["Bottle for daily use.", "Simple cap.", "Take it with you.", "Bottle for water.", "Black color."]


class ListingNotFound(LookupError):
    pass


class ListingConflict(RuntimeError):
    pass


class ListingSalesChanged(ListingConflict):
    code = "listing_sales_basis_changed"

    def __init__(self, units):
        super().__init__(self.code)
        self.previous_units, self.current_units = units


class ListingInvalid(ValueError):
    pass


def _json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(data):
    return hashlib.sha256(_json(data).encode("utf-8")).hexdigest()


def _scope(shop_id, marketplace_id, sku):
    return dict(zip(_TARGET_KEYS, (shop_id, marketplace_id, sku)))


def _key(target):
    if not isinstance(target, dict) or set(target) != set(_TARGET_KEYS):
        raise ListingInvalid("invalid_listing_scope")
    result = tuple(target[name] for name in _TARGET_KEYS)
    if any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in result):
        raise ListingInvalid("invalid_listing_scope")
    return result


def find_targets(conn, user_id, sku=None):
    rows = conn.execute(
        """SELECT p.shop_id, p.marketplace_id, p.sku, p.title FROM listing_test_products p
           JOIN listing_test_shops s ON s.shop_id=p.shop_id AND s.marketplace_id=p.marketplace_id
           WHERE s.user_id=? AND (? IS NULL OR p.sku=?) ORDER BY p.shop_id, p.marketplace_id, p.sku""",
        (user_id, sku, sku),
    ).fetchall()
    return [dict(row) for row in rows]


def product_choices(conn, user_id, shop_id, marketplace_id, *, include_title=True):
    columns = "p.title, p.sku" if include_title else "p.sku"
    rows = conn.execute(
        f"""SELECT {columns} FROM listing_test_products p
           JOIN listing_test_shops s USING (shop_id, marketplace_id)
           WHERE s.user_id=? AND p.shop_id=? AND p.marketplace_id=? ORDER BY p.sku""",
        (user_id, shop_id, marketplace_id),
    ).fetchall()
    return [dict(row) for row in rows]


def owned_targets(conn, user_id):
    return [dict(row) for row in conn.execute(
        """SELECT p.shop_id, p.marketplace_id, p.sku FROM listing_test_products p
           JOIN listing_test_shops s USING (shop_id, marketplace_id)
           WHERE s.user_id=? ORDER BY p.shop_id, p.marketplace_id, p.sku""", (user_id,),
    )]


def _source(conn, user_id, target, *, kind):
    table = {"listing": "listing_test_products", "facts": "listing_test_facts", "sales": "listing_test_sales",
             "required_facts": "listing_test_required_facts"}[kind]
    return conn.execute(
        f"""SELECT p.* FROM {table} p JOIN listing_test_shops s
             ON s.shop_id=p.shop_id AND s.marketplace_id=p.marketplace_id
             WHERE s.user_id=? AND p.shop_id=? AND p.marketplace_id=? AND p.sku=?""",
        (user_id, *_key(target)),
    ).fetchone()


_READABLE_PRODUCTS = """
    FROM listing_test_products p JOIN listing_test_shops s USING (shop_id, marketplace_id)
    WHERE s.user_id=?
"""


def readable_product_scopes(conn, user_id, skus):
    """Resolve query identifiers against the same account scope as a bound product."""
    if not skus:
        return []
    placeholders = ",".join("?" for _ in skus)
    return [tuple(row) for row in conn.execute(
        f"""SELECT p.shop_id, p.marketplace_id, p.sku {_READABLE_PRODUCTS}
            AND p.sku COLLATE NOCASE IN ({placeholders})""", (user_id, *skus),
    )]


def _product_catalog(conn, user_id, readable_products):
    products = {}
    rows = conn.execute(
        f"""WITH readable AS (
                SELECT DISTINCT p.shop_id, p.marketplace_id, p.sku, p.title {readable_products}
            )
            SELECT p.*, a.sku AS alias_sku, a.alias FROM readable p
            LEFT JOIN listing_test_catalog_aliases a ON a.shop_id=p.shop_id AND a.marketplace_id=p.marketplace_id
                AND (a.sku='' OR a.sku=p.sku)
            ORDER BY p.shop_id, p.marketplace_id, p.sku, a.alias""", (user_id,),
    )
    for row in rows:
        scope = (row["shop_id"], row["marketplace_id"], row["sku"])
        item = products.setdefault(scope, {"title": row["title"], "shop_aliases": [], "product_aliases": []})
        if row["alias"]:
            item["product_aliases" if row["alias_sku"] else "shop_aliases"].append(row["alias"])
    return products


def readable_product_catalog(conn, user_id):
    """Read names only for product scopes visible to this account. An empty alias SKU denotes a shop name."""
    return _product_catalog(conn, user_id, _READABLE_PRODUCTS)


def scope_error(conn, user_id, shop_id, marketplace_id, sku):
    if conn.execute(
        f"SELECT 1 {_READABLE_PRODUCTS} AND p.shop_id=? AND p.marketplace_id=? AND p.sku=?",
        (user_id, shop_id, marketplace_id, sku),
    ).fetchone() is None:
        return "listing_not_found"
    return None


def read_listing(conn, user_id, shop_id, marketplace_id, sku):
    target = _scope(shop_id, marketplace_id, sku)
    row = _source(conn, user_id, target, kind="listing")
    if row is None:
        return None
    return {"source": SOURCE, "target": target, "version": row["version"], "title": row["title"],
            "bullets": json.loads(row["bullets"]), "price_cents": row["price_cents"],
            "inventory_units": row["inventory_units"]}


def read_facts(conn, user_id, shop_id, marketplace_id, sku):
    target = _scope(shop_id, marketplace_id, sku)
    row = _source(conn, user_id, target, kind="facts")
    if row is None:
        return None
    facts = json.loads(row["facts"])
    sources = {fact["fact_id"]: product_documents.fact_source(conn, user_id, _key(target), fact["original"])
               for fact in facts if "original" in fact}
    evidence = {"source": SOURCE, "target": target, "version": row["version"], "facts": facts}
    if sources:
        evidence["original_sources"] = sources
    if required := _source(conn, user_id, target, kind="required_facts"):
        evidence["required_fields"] = json.loads(required["fields"])
    return evidence


def missing_fact_fields(evidence):
    return {key: label for key, label in evidence.get("required_fields", {}).items()
            if key not in {row["fact_id"] for row in evidence["facts"]}}


def _date(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ListingInvalid("invalid_listing_sales_window") from None
    if parsed.isoformat() != value:
        raise ListingInvalid("invalid_listing_sales_window")
    return parsed


def _window(sales, start, end):
    span = (end - start).days + 1
    if not 1 <= span <= 90:
        raise ListingInvalid("invalid_listing_sales_window")
    days = [(start + timedelta(days=offset)).isoformat() for offset in range(span)]
    points = [{"date": day, "units": sales.get(day)} for day in days]
    covered = [point["units"] for point in points if point["units"] is not None]
    return {"start_date": days[0], "end_date": days[-1], "days": points, "covered_days": len(covered),
            "units": sum(covered) if len(covered) == span else None}


def _current_order_sales(conn, target):
    return conn.execute("""SELECT * FROM listing_order_sales_snapshots
        WHERE shop_id=? AND marketplace_id=? AND sku=? ORDER BY version DESC LIMIT 1""", _key(target)).fetchone()


def publish_order_sales(conn, user_id, target, source, series, *, source_revision):
    """Publish covered store days from a trusted order reader in its write transaction.

    The publisher supplies the site mapping and the immutable upstream revision.
    Days absent from the series have unknown coverage. A repeated publication
    of the current snapshot returns its existing identity.
    """
    key = _key(target)
    if not conn.in_transaction:
        raise ListingInvalid("listing_sales_publication_requires_transaction")
    if (not isinstance(source, str) or source not in orders.SOURCES
            or not isinstance(source_revision, str) or not source_revision.strip()
            or not isinstance(series, dict) or any(type(units) is not int or units < 0 for units in series.values())):
        raise ListingInvalid("invalid_listing_order_sales")
    if scope_error(conn, user_id, *key) or not orders.has_access(conn, user_id, source, key[0]):
        raise ListingNotFound
    for day in series:
        _date(day)
    digest = _digest([target, source, source_revision, series])
    prior = _current_order_sales(conn, target)
    if prior is not None and prior["digest"] == digest:
        return _order_snapshot(prior)
    version = 1 if prior is None else prior["version"] + 1
    snapshot_id = uuid4().hex
    conn.execute("INSERT INTO listing_order_sales_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (*key, version, snapshot_id, source, digest, source_revision, _json(series)))
    return {"snapshot_id": snapshot_id, "version": version, "source": source,
            "digest": digest, "source_revision": source_revision}


def _order_snapshot(row):
    return {field: row[field] for field in ("snapshot_id", "version", "source", "digest", "source_revision")}


def _order_series(target, row):
    try:
        series = json.loads(row["series"])
    except ValueError:
        return None
    if isinstance(series, dict) and _digest([target, row["source"], row["source_revision"], series]) == row["digest"]:
        return series
    return None


def read_order_sales_snapshot(conn, user_id, target, snapshot_id):
    """Read any immutable order-backed version under the same grants as read_sales."""
    key = _key(target)
    if scope_error(conn, user_id, *key):
        return None
    row = conn.execute("""SELECT * FROM listing_order_sales_snapshots
        WHERE shop_id=? AND marketplace_id=? AND sku=? AND snapshot_id=?""", (*key, snapshot_id)).fetchone()
    if row is None or not orders.has_access(conn, user_id, row["source"], key[0]):
        return None
    series = _order_series(target, row)
    return None if series is None else {**_order_snapshot(row), "series": series}


def read_sales(conn, user_id, shop_id, marketplace_id, sku, baseline_start=None, baseline_end=None,
               current_start=None, current_end=None):
    target = _scope(shop_id, marketplace_id, sku)
    if scope_error(conn, user_id, *_key(target)):
        return None
    if conn.execute("SELECT 1 FROM order_sales_pending WHERE shop_id=? AND marketplace_id=? AND sku=?",
                    _key(target)).fetchone():
        return None
    snapshot = _current_order_sales(conn, target)
    if snapshot is not None and not orders.has_access(conn, user_id, snapshot["source"], shop_id):
        return None
    row = snapshot if snapshot is not None else _source(conn, user_id, target, kind="sales")
    sales = (_order_series(target, snapshot) if snapshot is not None
             else json.loads(row["series"]) if row is not None else {})
    if sales is None:
        return None
    result = {"source": SOURCE, "target": target, "version": row["version"] if row is not None else None}
    if snapshot is not None:
        result["order_snapshot"] = _order_snapshot(snapshot)
    if baseline_start is None and baseline_end is None:
        return {**result, "current": _window(sales, _date(current_start), _date(current_end))}
    b_start, b_end, c_start, c_end = map(_date, (baseline_start, baseline_end, current_start, current_end))
    if b_end >= c_start or (b_end - b_start) != (c_end - c_start):
        raise ListingInvalid("invalid_listing_sales_window")
    baseline = _window(sales, b_start, b_end)
    current = _window(sales, c_start, c_end)
    first, second = baseline["units"], current["units"]
    delta = second - first if first is not None and second is not None else None
    return {**result, "baseline": baseline, "current": current,
            "units_delta": delta, "units_change_pct": round(delta * 100 / first, 6) if delta is not None and first else None,
            "conversion_rate": None}


def rank_sales(metrics):
    if not metrics or any(row[window]["units"] is None for row in metrics for window in ("baseline", "current")):
        return "incomplete", None
    if any(row["baseline"]["units"] == 0 for row in metrics):
        return "baseline_zero", None
    changes = [(Fraction(row["baseline"]["units"] - row["current"]["units"], row["baseline"]["units"]), row)
               for row in metrics]
    largest = max(drop for drop, _ in changes)
    if largest <= 0:
        return "no_decline", None
    winners = [row["target"] for drop, row in changes if drop == largest]
    return ("selected", winners[0]) if len(winners) == 1 else ("tied", None)


def _facts(variant):
    capacity, material = ("750 mL", "aluminum") if variant == "changed" else ("500 mL", "stainless steel")
    material_phrase = "an aluminum" if material == "aluminum" else "a stainless steel"
    facts = [
        ("capacity", f"The bottle holds {capacity}.", (f"{capacity} capacity for carrying water.", f"Holds {capacity} of water.")),
        ("material", f"The bottle body is {material}.", (f"Made with {material_phrase} body.", f"{material.capitalize()} bottle body.")),
        ("cap", "The cap screws onto the bottle.", ("Screw-on cap for closing the bottle.", "Closes with a screw-on cap.")),
        ("carry_loop", "The cap has a carry loop.", ("Carry loop integrated into the cap.", "The cap includes a loop for carrying.")),
        ("finish", "The bottle has a matte black finish.", ("Matte black finish on the bottle exterior.", "Finished in matte black.")),
        ("care", "The care sheet specifies hand washing.", ("Hand wash according to the care instructions.", "Hand wash for routine cleaning.")),
        ("opening", "The mouth measures 40 mm across.", ("40 mm bottle opening.", "The bottle has a 40 mm opening.")),
    ]
    if variant == "missing":
        facts = facts[:4]
    return [{"fact_id": identifier, "statement": statement, "options": list(options)}
            for identifier, statement, options in facts]


def seed_test_product(conn, user_id, *, shop_id=TEST_SHOP, marketplace_id=TEST_MARKETPLACE, sku=TEST_SKU,
                      variant="original", current_end=TEST_CURRENT[1]):
    """Install reproducible local fixtures for an existing test account."""
    if variant not in {"original", "changed", "missing"}:
        raise ListingInvalid("invalid_listing_fixture_variant")
    key = _key(_scope(shop_id, marketplace_id, sku))
    try:
        start = _date(current_end) - timedelta(days=13)
    except OverflowError:
        raise ListingInvalid("invalid_listing_sales_window") from None
    if conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
        raise ListingNotFound
    conn.execute("INSERT OR IGNORE INTO listing_test_shops VALUES (?, ?, ?)", (*key[:2], user_id))
    if conn.execute("SELECT 1 FROM listing_test_shops WHERE shop_id=? AND marketplace_id=? AND user_id=?",
                    (*key[:2], user_id)).fetchone() is None:
        raise ListingNotFound
    conn.execute("INSERT OR IGNORE INTO listing_test_products VALUES (?, ?, ?, 1, ?, ?, 2499, 24)",
                 (*key, "Task 42 Test Water Bottle", _json(_ORIGINAL_BULLETS)))
    conn.execute("""INSERT INTO listing_test_facts VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(shop_id, marketplace_id, sku) DO UPDATE SET version=version+1, facts=excluded.facts
        WHERE facts<>excluded.facts""", (*key, _json(_facts(variant))))
    counts = [8, 11, 10, 9, 12, 7, 13, 6, 7, 7, 5, 9, 5, 10]
    sales = {(start + timedelta(days=index)).isoformat(): count for index, count in enumerate(counts)}
    conn.execute("""INSERT INTO listing_test_sales VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(shop_id, marketplace_id, sku) DO UPDATE SET version=version+1, series=excluded.series
        WHERE series<>excluded.series""", (*key, _json(sales)))
    return _scope(*key)


def build_proposal(listing, facts, bullets, *, artifact_refs=None, sales=None, comparison=None, inspection=None,
                   report_basis=None):
    """Build an immutable draft from five different trusted fact phrasings."""
    if not listing or not facts or listing["target"] != facts["target"]:
        raise ListingInvalid("listing_evidence_mismatch")
    target = listing["target"]
    _key(target)
    if not isinstance(bullets, list) or len(bullets) != 5:
        raise ListingInvalid("listing_requires_five_bullets")
    if listing["source"] != SOURCE or facts["source"] != SOURCE:
        raise ListingInvalid("listing_evidence_mismatch")
    if missing_fact_fields(facts):
        raise ListingInvalid("listing_required_facts_missing")
    approved = {row["fact_id"]: row for row in facts["facts"]}
    selected = {}
    for bullet in bullets:
        if not isinstance(bullet, dict) or set(bullet) != {"fact_id", "text"}:
            raise ListingInvalid("listing_invalid_bullet")
        fact_id, description = bullet["fact_id"], bullet["text"]
        if not isinstance(fact_id, str) or not isinstance(description, str):
            raise ListingInvalid("listing_invalid_bullet")
        fact = approved.get(fact_id)
        if fact_id in selected or not fact or description not in fact["options"]:
            raise ListingInvalid("listing_unverified_bullet")
        selected[fact_id] = {"fact_id": fact_id, "text": description, "statement": fact["statement"]}
    after = [bullet["text"] for bullet in selected.values()]
    if after == listing["bullets"]:
        raise ListingInvalid("listing_bullets_unchanged")
    refs = artifact_refs if artifact_refs is not None else {}
    if not isinstance(refs, dict) or any(not isinstance(value, str) or not value for value in refs.values()):
        raise ListingInvalid("listing_invalid_artifact_refs")
    result = {"source": SOURCE, "target": target, "original_version": listing["version"],
              "facts_version": facts["version"], "facts_sha256": _digest({key: value for key, value in facts.items()
                                                                         if key != "original_sources"}),
              "before_bullets": listing["bullets"], "after_bullets": after,
              "bullets": list(selected.values()), "artifact_refs": refs}
    if "original_sources" in facts:
        sources = {fact_id: facts["original_sources"].get(fact_id)
                   for fact_id in selected if "original" in approved[fact_id]}
        if None in sources.values():
            raise ListingInvalid("listing_unverified_fact_source")
        if sources:
            result["product_sources"] = sources
    if sales is not None:
        if sales["target"] != target or sales["source"] != SOURCE:
            raise ListingInvalid("listing_evidence_mismatch")
        baseline, current = sales["baseline"], sales["current"]
        result["sales_basis"] = {"version": sales["version"],
            "evidence_sha256": _digest(sales), **dict(zip(
            _WINDOW_KEYS, (baseline["start_date"], baseline["end_date"], current["start_date"], current["end_date"]),
        ))}
        if "order_snapshot" in sales:
            result["sales_basis"]["order_snapshot"] = sales["order_snapshot"]
    if comparison is not None:
        metrics = [row["result"] for row in comparison]
        keys = {_key(row["target"]) for row in metrics}
        if (sales not in metrics or len(keys) != len(metrics) or len(keys) < 2
                or rank_sales(metrics) != ("selected", target)):
            raise ListingInvalid("listing_invalid_comparison")
        result["comparison_basis"] = [{"target": row["result"]["target"], "artifact_id": row["artifact_id"],
            "evidence_sha256": _digest(row["result"]), **({"order_snapshot": row["result"]["order_snapshot"]}
            if "order_snapshot" in row["result"] else {})} for row in comparison]
    if inspection is not None:
        threshold = inspection["threshold_pct"]
        omissions = inspection["omitted_fact_ids"]
        if (comparison is None or type(threshold) not in (int, float) or not 0 < threshold <= 100
                or not isinstance(omissions, list) or len(omissions) != len(set(omissions))
                or not set(omissions) <= approved.keys() or not set(omissions) & selected.keys()
                or 100 * (baseline["units"] - current["units"]) < threshold * baseline["units"]):
            raise ListingInvalid("listing_invalid_inspection_basis")
        result.update(inspection=inspection, review_baseline=baseline)
    if report_basis is not None:
        if (sales is None or "order_snapshot" not in sales or comparison is not None or inspection is not None
                or baseline["units"] is None or current["units"] is None
                or set(result.get("product_sources", {})) != set(selected)
                or report_basis["window"] != [current["start_date"], current["end_date"]]):
            raise ListingInvalid("listing_invalid_publication_basis")
        result.update(report_basis=report_basis, review_baseline=current)
    return result


def _order_access(conn, user_id, target, snapshot):
    return orders.has_access(conn, user_id, snapshot["source"], target["shop_id"])


def _sales_access(conn, user_id, result):
    target = result["target"]
    snapshot = result.get("order_snapshot")
    if snapshot is None:
        # Legacy evidence can predate the registration of an order projection.
        snapshot = _current_order_sales(conn, target)
    return snapshot is None or _order_access(conn, user_id, target, snapshot)


def _sales_visible(conn, user_id, result):
    if not _sales_access(conn, user_id, result):
        return False
    snapshot = result.get("order_snapshot")
    if snapshot is None:
        return True
    target = result["target"]
    row = conn.execute("""SELECT * FROM listing_order_sales_snapshots
        WHERE (shop_id,marketplace_id,sku)=(?,?,?) AND snapshot_id=?""",
        (*_key(target), snapshot["snapshot_id"])).fetchone()
    return row is not None and _order_snapshot(row) == snapshot and _order_series(target, row) is not None


def _proposal_sales(proposal):
    if "sales_basis" in proposal:
        yield {"target": proposal["target"], **proposal["sales_basis"]}
        yield from proposal.get("comparison_basis", ())


def _proposal_order_access(conn, user_id, proposal, *, current=False):
    return all(_sales_access(conn, user_id, row)
               and (not current or _sales_access(conn, user_id, {"target": row["target"]}))
               for row in _proposal_sales(proposal))


def _unbound_originals(source, selected=None):
    facts = source.get("facts") or {}
    return "original_sources" not in facts and any(
        "original" in fact and (selected is None or fact["fact_id"] in selected) for fact in facts.get("facts", ())
    )


def _fact_sources_visible(conn, user_id, target, facts, references, selected=None):
    for fact in facts:
        if "original" not in fact or (selected is not None and fact["fact_id"] not in selected):
            continue
        actual = product_documents.fact_source(conn, user_id, _key(target), fact["original"])
        if actual is None or (references is not None and references.get(fact["fact_id"]) != actual):
            return False
    return True


def evidence_visible(conn, user_id, evidence):
    """Recheck read access for evidence displayed outside the action receipt."""
    if evidence["kind"] == KIND and _unbound_originals(evidence):
        return False
    reads = [evidence[key] for key in ("metrics", "facts") if evidence.get(key)]
    reads.extend(evidence.get("comparisons", ()))
    for product in evidence.get("products", ()):
        reads.extend(receipt["result"] for receipt in product["receipts"].values() if receipt.get("result"))
    for result in reads:
        if "current" in result and "target" in result and not _sales_visible(conn, user_id, result):
            return False
        if "facts" in result and not _fact_sources_visible(conn, user_id, result["target"], result["facts"],
                                                             result.get("original_sources")):
            return False
        if "source_chunks" in result:
            scope = _key(result["target"])
            chunks = {chunk["chunk_id"] for group in result["source_chunks"].values() for chunk in group}
            if product_documents.current_chunk_ids(conn, user_id, scope, result["document_version"], chunks) != chunks:
                return False
    return True


def _require_snapshot(conn, user_id, snapshot, *, trace_id=None, run_id=None, checkpoint=None,
                      reviewed_listing=None, conversation_id=None, previous_sales=None):
    from backend.database import listing_evidence

    def verify(source, reader, target, artifact_id, dates=None, *, known=None):
        arguments = {**target, **(dates or {})}
        result = known if known is not None else reader(conn, user_id, *_key(target), **(dates or {}))
        if trace_id:
            add_trace_step(trace_id, "listing_evidence", "reused" if known is not None else "reread",
                           detail=checkpoint, error_code=None if result is not None else "listing_not_found",
                           evidence={"run_id": run_id, "checkpoint": checkpoint, "artifact_id": artifact_id,
                                     "source": source, "arguments": arguments})
        return result

    try:
        refs = snapshot["artifact_refs"]
        target = snapshot["target"]
        listing = verify("get_listing_current", read_listing, target, refs.get("listing"), known=reviewed_listing)
        if listing is None:
            raise ListingNotFound
        if not _proposal_order_access(conn, user_id, snapshot, current=True):
            raise ListingNotFound
        facts = verify("get_listing_facts", read_facts, target, refs.get("facts"))
        sales = None
        if basis := snapshot.get("sales_basis"):
            dates = {field: basis[field] for field in _WINDOW_KEYS}
            sales = verify("get_listing_sales", read_sales, target, refs.get("sales"), dates)
            if not listing_evidence.complete_sales(sales):
                raise ListingConflict("listing_evidence_changed")
        comparison = None
        if "comparison_basis" in snapshot:
            comparison = []
            for row in snapshot["comparison_basis"]:
                metric = verify("get_listing_sales", read_sales, row["target"], row["artifact_id"], dates,
                                known=sales if row["target"] == target and conn.in_transaction else None)
                if metric is None:
                    raise ListingConflict("listing_evidence_changed")
                comparison.append({"artifact_id": row["artifact_id"], "result": metric})
        report_basis = None
        if expected := snapshot.get("report_basis"):
            report = listing_evidence.read_report(conn, user_id, conversation_id, target, tuple(dates.values()),
                                                  target_scoped=True)
            report_basis = listing_evidence.publication_report_basis(report, expected["artifact_id"])
            if trace_id:
                add_trace_step(trace_id, "listing_evidence", "reread", detail=checkpoint,
                               evidence={"run_id": run_id, "checkpoint": checkpoint, "source": "publication_report",
                                         "artifact_id": expected["artifact_id"], "expected": expected["report"],
                                         "current": report.get("report")})
            if report_basis != expected:
                raise ListingConflict("listing_report_changed")
        choices = [{"fact_id": bullet["fact_id"], "text": bullet["text"]} for bullet in snapshot["bullets"]]
        actual = build_proposal(listing, facts, choices, sales=sales, artifact_refs=refs, comparison=comparison,
                                inspection=snapshot.get("inspection"), report_basis=report_basis)
        if actual != snapshot:
            changed = {key for key in actual.keys() | snapshot.keys() if actual.get(key) != snapshot.get(key)}
            if sales is not None and changed <= {"sales_basis", "review_baseline"}:
                units = listing_evidence.sales_correction(conn, user_id, previous_sales, sales)
                if units is not None:
                    raise ListingSalesChanged(units)
            raise ListingConflict("listing_evidence_changed")
    except (TypeError, KeyError, ListingInvalid):
        raise ListingConflict("listing_evidence_changed") from None


def _require_evidence(evidence, snapshot):
    """Verify the exact persisted reads and artifact references behind this draft."""
    try:
        if evidence["kind"] != KIND or evidence["target"] != snapshot["target"]:
            raise ListingInvalid("listing_evidence_mismatch")
        artifacts = evidence["artifacts"]
        refs = {key: artifacts[tool]["artifact_id"] for key, tool in (
            ("listing", "get_listing_current"), ("facts", "get_listing_facts"), ("sales", "get_listing_sales"),
        )}
        choices = [{"fact_id": bullet["fact_id"], "text": bullet["text"]} for bullet in snapshot["bullets"]]
        comparison = evidence.get("comparison_receipts")
        if comparison is not None and ([row["result"] for row in comparison] != evidence["comparisons"]
                                       or [row["result"]["target"] for row in comparison] != evidence["candidates"]):
            raise ListingInvalid("listing_invalid_comparison")
        inspection = None
        if assessment := evidence.get("operations_inspection"):
            product = assessment["products"][snapshot["target"]["sku"]]
            inspection = {"threshold_pct": assessment["threshold_pct"], "omitted_fact_ids": product["omissions"]}
        report_basis = None
        if publication := evidence.get("publication"):
            from backend.database import listing_evidence

            report_basis = listing_evidence.publication_report_basis(evidence["publication_report"],
                                                                    artifacts[listing_evidence.REPORT]["artifact_id"])
            if (report_basis is None or publication["target"] != snapshot["target"]
                    or publication["snapshot"] != evidence["metrics"].get("order_snapshot")):
                raise ListingInvalid("listing_invalid_publication_basis")
        if build_proposal(evidence["listing"], evidence["facts"], choices,
                          artifact_refs=refs, sales=evidence["metrics"], comparison=comparison,
                          inspection=inspection, report_basis=report_basis) != snapshot:
            raise ListingInvalid("listing_evidence_mismatch")
    except (TypeError, KeyError, ListingInvalid):
        raise ListingConflict("listing_provenance_changed") from None


def create_group_proposal(conn, run, entry_id, evidence):
    """Create a pending action in the caller's group reply transaction."""
    if evidence.get("kind") != KIND:
        return None
    snapshot = evidence.get("proposal")
    if snapshot is None:
        return None
    entry = conn.execute("""SELECT e.details, r.trace_id FROM chat_group_entries e
        JOIN chat_group_runs r ON r.run_id=e.run_id AND r.status='running'
        JOIN chat_groups g ON g.conversation_id=r.conversation_id AND g.archived=0
        WHERE e.id=? AND e.kind='assistant' AND g.user_id=? AND g.conversation_id=? AND r.run_id=?""",
        (entry_id, run.user_id, run.conversation_id, run.run_id),
    ).fetchone()
    if entry is None or json.loads(entry["details"] or "{}") != evidence:
        raise ListingConflict("listing_provenance_changed")
    _require_evidence(evidence, snapshot)
    trace_id = run.trace_id if "publication" in evidence else entry["trace_id"]
    _require_snapshot(conn, run.user_id, snapshot, trace_id=trace_id, run_id=run.run_id, checkpoint="proposal",
                      conversation_id=run.conversation_id)
    action_id = uuid4().hex
    conn.execute("INSERT INTO listing_change_proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (action_id, run.conversation_id, run.run_id, entry_id, *_key(snapshot["target"]), _json(snapshot)))
    return action_id


_ACTIONS = """SELECT p.*, g.archived, e.details, g.user_id, t.trace_id AS source_trace_id,
                     r.receipt_id, r.approved_by_user_id, r.approved_by_name, r.committed_version,
                     r.committed_bullets, r.execution_trace_id, r.approved_at
              FROM listing_change_proposals p
              JOIN chat_groups g ON g.conversation_id=p.conversation_id
              JOIN chat_group_runs t ON t.run_id=p.run_id AND t.conversation_id=g.conversation_id AND t.status='completed'
              JOIN chat_group_entries e ON e.id=p.entry_id AND e.run_id=t.run_id AND e.kind='assistant'
              JOIN listing_test_shops s ON s.shop_id=p.shop_id AND s.marketplace_id=p.marketplace_id AND s.user_id=g.user_id
              LEFT JOIN listing_change_receipts r ON r.action_id=p.action_id
              WHERE g.user_id=? AND p.conversation_id=?"""


def _action(conn, user_id, conversation_id, run_id, action_id):
    row = conn.execute(_ACTIONS + " AND p.run_id=? AND p.action_id=?", (user_id, conversation_id, run_id, action_id)).fetchone()
    if row is None:
        raise ListingNotFound
    source = json.loads(row["details"] or "{}")
    proposal = json.loads(row["snapshot"])
    if row["receipt_id"] is None:
        publication = source.get("publication")
        if (not _proposal_order_access(conn, user_id, proposal, current=True)
                or (publication and publication["read_by_user_id"] != user_id)):
            raise ListingNotFound
    if source.get("kind") != KIND or source.get("proposal") != proposal:
        raise ListingConflict("listing_provenance_changed")
    _require_evidence(source, proposal)
    return row, proposal, source


def _receipt(row):
    if row["receipt_id"] is None:
        return None
    return {"receipt_id": row["receipt_id"], "action_id": row["action_id"], "approved_by_user_id": row["approved_by_user_id"],
            "approved_by_name": row["approved_by_name"], "committed_version": row["committed_version"],
            "committed_bullets": json.loads(row["committed_bullets"]), "execution_trace_id": row["execution_trace_id"],
            "source_trace_id": row["source_trace_id"], "approved_at": row["approved_at"]}


def _summary(row, proposal):
    receipt = _receipt(row)
    state = "approved" if receipt else "pending"
    return {"action_id": row["action_id"], "conversation_id": row["conversation_id"], "run_id": row["run_id"],
            "entry_id": f"entry:{row['entry_id']}", "target": proposal["target"],
            "expected_version": proposal["original_version"], "status": state, "state": state,
            "receipt_id": row["receipt_id"], "receipt": receipt}


def group_proposals(conn, user_id, conversation_id):
    result = {}
    for row in conn.execute(_ACTIONS + " ORDER BY p.entry_id", (user_id, conversation_id)).fetchall():
        result.setdefault(row["entry_id"], []).append(_summary(row, json.loads(row["snapshot"])))
    return result


def read_action(conn, user_id, conversation_id, run_id, action_id, *, review=False):
    """Read the receipt or recheck a pending action, including legacy review callers."""
    row, proposal, source = _action(conn, user_id, conversation_id, run_id, action_id)
    listing = read_listing(conn, user_id, *_key(proposal["target"]))
    if listing is None:
        raise ListingNotFound
    order_access = _proposal_order_access(conn, user_id, proposal)
    result = {**_summary(row, proposal), "source_trace_id": row["source_trace_id"], "listing": listing}
    selected = {bullet["fact_id"] for bullet in proposal["bullets"]}
    originals_visible = _fact_sources_visible(conn, user_id, proposal["target"], source["facts"]["facts"],
                                              proposal.get("product_sources"), selected)
    if (order_access and originals_visible and not _unbound_originals(source, selected)
            and all(_sales_visible(conn, user_id, sale) for sale in _proposal_sales(proposal))):
        result["proposal"] = proposal
        if publication := source.get("publication"):
            result["publication"] = publication
    if source.get("publication") and row["receipt_id"] is not None:
        result["review_available"] = all(_sales_visible(conn, user_id, sale) for sale in _proposal_sales(proposal))
    if result["state"] == "pending":
        if row["archived"]:
            result["approval_blocked"] = "工作群已归档"
        else:
            try:
                _require_snapshot(conn, user_id, proposal, reviewed_listing=listing if conn.in_transaction else None,
                                  conversation_id=conversation_id, previous_sales=source.get("metrics"))
            except ListingSalesChanged as error:
                result["approval_block_reason"] = {"code": error.code, "previous_units": error.previous_units,
                                                   "current_units": error.current_units}
            except ListingConflict:
                result["approval_blocked"] = "商品或依据已变化，请重新生成提案"
    return result


def approve(conn, user_id, conversation_id, run_id, action_id, execution_trace_id):
    """Apply only five points and the version inside the caller's IMMEDIATE transaction."""
    row, proposal, source = _action(conn, user_id, conversation_id, run_id, action_id)
    if row["receipt_id"] is not None:
        return read_action(conn, user_id, conversation_id, run_id, action_id)
    if row["archived"]:
        raise ListingConflict("listing_group_archived")
    _require_snapshot(conn, user_id, proposal, trace_id=execution_trace_id, run_id=run_id, checkpoint="approval",
                      conversation_id=conversation_id, previous_sales=source.get("metrics"))
    key = _key(proposal["target"])
    version = proposal["original_version"]
    bullets = _json(proposal["after_bullets"])
    updated = conn.execute("""UPDATE listing_test_products SET bullets=?, version=version+1
        WHERE shop_id=? AND marketplace_id=? AND sku=? AND version=?""", (bullets, *key, version))
    if updated.rowcount != 1:
        raise ListingConflict("listing_version_changed")
    username = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()["username"]
    conn.execute("INSERT INTO listing_change_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (uuid4().hex, action_id, user_id, username, version + 1, bullets, execution_trace_id,
                  _now().astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")))
    return read_action(conn, user_id, conversation_id, run_id, action_id)


def _now():
    return datetime.now(UTC)


def _review_day():
    return _now().astimezone(ZoneInfo(SALES_TIMEZONE)).date()


def read_cycle_review(conn, user_id, conversation_id, run_id, action_id):
    row, proposal, source = _action(conn, user_id, conversation_id, run_id, action_id)
    baseline = proposal.get("review_baseline")
    if (baseline is None or row["receipt_id"] is None
            or _unbound_originals(source, {bullet["fact_id"] for bullet in proposal["bullets"]})
            or not all(_sales_visible(conn, user_id, sale) for sale in _proposal_sales(proposal))):
        raise ListingNotFound
    target = proposal["target"]
    approved_day = datetime.fromisoformat(row["approved_at"]).astimezone(ZoneInfo(SALES_TIMEZONE)).date()
    start, end = approved_day + timedelta(days=1), approved_day + timedelta(days=7)
    sales = read_sales(conn, user_id, *_key(target), current_start=start.isoformat(), current_end=end.isoformat())
    if sales is None:
        raise ListingNotFound
    original_snapshot = proposal["sales_basis"].get("order_snapshot")
    current_snapshot = sales.get("order_snapshot")
    if (original_snapshot or {}).get("source") != (current_snapshot or {}).get("source"):
        raise ListingConflict("listing_evidence_changed")
    today = _review_day().isoformat()
    current = _window({point["date"]: point["units"] for point in sales["current"]["days"]
                       if point["date"] < today}, start, end)
    missing = [point["date"] for point in current["days"] if point["units"] is None]
    change = None if missing else current["units"] - baseline["units"]
    result = {"target": target, "receipt_id": row["receipt_id"], "action_id": action_id,
            "sales_timezone": SALES_TIMEZONE, "approved_day": approved_day.isoformat(),
            "status": "incomplete" if missing else "complete", "baseline": baseline, "current": current,
            "missing_dates": missing, "units_delta": change,
            "units_change_pct": round(change * 100 / baseline["units"], 6) if change is not None and baseline["units"] else None}
    if publication := source.get("publication"):
        follow_up = conn.execute("""SELECT b.run_id, json_extract(b.spec, '$.publication.publication_id') AS publication_id
            FROM background_inspections b JOIN chat_group_runs r USING (run_id)
            WHERE r.conversation_id=? AND r.status='completed' AND b.user_id=?
              AND json_extract(b.spec, '$.publication.snapshot.snapshot_id')=?
            ORDER BY b.rowid DESC LIMIT 1""",
            (conversation_id, user_id, current_snapshot["snapshot_id"])).fetchone()
        result.update(run_id=run_id, original_version=proposal["original_version"], receipt=_receipt(row),
                      publication=publication, baseline_snapshot=original_snapshot, observation_snapshot=current_snapshot,
                      observation_inspection=dict(follow_up) if follow_up else None,
                      known_units=sum(point["units"] for point in current["days"] if point["units"] is not None),
                      effect_status="unknown", recommendation=("补齐缺报日期后继续比较。" if missing
                                                               else "继续观察同期销量和其他影响因素。"))
    return result


def main():
    from backend.database import database

    parser = argparse.ArgumentParser(description="Seed an explicit local test listing in an isolated SQLite database.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--shop-id", default=TEST_SHOP)
    parser.add_argument("--marketplace-id", default=TEST_MARKETPLACE)
    parser.add_argument("--sku", default=TEST_SKU)
    parser.add_argument("--variant", choices=("original", "changed", "missing"), default="original")
    parser.add_argument("--current-end", default=TEST_CURRENT[1])
    args = parser.parse_args()
    if database.settings.DIAGNOSIS_MYSQL_ENABLED or database.sandbox_db_path() is None:
        parser.error("请设置独立的 DIAGNOSIS_SANDBOX_DB_PATH 并使用 SQLite")
    database.init_db()
    with closing(database.get_db()) as conn, conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id FROM users WHERE username=?", (args.username,)).fetchone()
        if row is None:
            parser.error("请先初始化一个已有的测试账号")
        target = seed_test_product(conn, row["id"], shop_id=args.shop_id, marketplace_id=args.marketplace_id,
                                   sku=args.sku, variant=args.variant, current_end=args.current_end)
    end = _date(args.current_end)
    print(_json({"source": SOURCE, "target": target,
                 "baseline": [(end - timedelta(days=offset)).isoformat() for offset in (13, 7)],
                 "current": [(end - timedelta(days=offset)).isoformat() for offset in (6, 0)]}))


if __name__ == "__main__":
    main()
