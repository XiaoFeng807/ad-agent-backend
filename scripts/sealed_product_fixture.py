"""Use holder-owned original formats and read grants in an isolated evaluation database."""

import json
import re
import sqlite3
from collections import Counter
from contextlib import closing, contextmanager
from hashlib import sha256
from secrets import token_urlsafe
from unittest.mock import patch

from backend.database import database, listing_operations as listing, product_documents as store
from backend.rag import product_evidence as rag
from scripts.product_evidence import PASSWORD

KINDS = {"SPEC": "specs", "MANUAL": "manual", "FAQ": "faq"}
_DEFAULT_REGIONS = rag.source_regions
_NUMBERED = {
    kind: re.compile(
        rf"^\ufeff?[ \t]{{0,3}}(?:#{{1,6}}[ \t]+)?(?:\*\*)?[\[【]?({prefix}[0-9]+)[\]】]?(?:\*\*)?"
        r"(?=[ \t|｜:：.、)）\-–—\r\n]|$)[ \t|｜:：.、)）\-–—]*([^\r\n]*)",
        re.M,
    )
    for kind, prefix in (("manual", "M"), ("faq", "F"))
}
_READABLE = """
    JOIN sealed_product_readers s ON s.source_id=d.id
    WHERE s.user_id=? AND r.active=1
"""
_PRODUCTS = """
    FROM listing_test_products p
    JOIN product_document_releases r USING (shop_id, marketplace_id, sku)
    JOIN product_documents d ON d.release_id=r.id
""" + _READABLE


def source_regions(kind, original):
    if kind in _NUMBERED:
        headings = list(_NUMBERED[kind].finditer(original))
        if headings:
            regions = []
            prefix = "page" if kind == "manual" else "faq"
            for idx, heading in enumerate(headings):
                label, title = heading.groups()
                end = headings[idx + 1].start() if idx + 1 < len(headings) else len(original)
                content = original[heading.start():end].rstrip()
                regions.append(rag._region(f"{prefix}:{label}", title.strip(" *\t") or label,
                                           heading.start(), heading.start() + len(content)))
            if len({row["location"] for row in regions}) != len(regions):
                raise ValueError("document needs unique source locations")
            return regions
    if kind == "specs":
        lines = original.splitlines(keepends=True)
        if lines and len(lines[0].rstrip("\r\n").split("\t")) == 3:
            regions, offset = [], len(lines[0])
            for number, line in enumerate(lines[1:], 2):
                content = line.rstrip("\r\n")
                if content:
                    columns = content.split("\t")
                    if len(columns) != 3 or any(not cell.strip() for cell in columns[:2]):
                        raise ValueError(f"incomplete specification row {number}")
                    regions.append(rag._region(f"row:{number}", columns[0], offset, offset + len(content)))
                offset += len(line)
            if regions:
                return regions
    return _DEFAULT_REGIONS(kind, original)


def _scope_error(conn, user_id, shop_id, marketplace_id, sku):
    row = conn.execute(f"SELECT 1 {_PRODUCTS} AND r.shop_id=? AND r.marketplace_id=? AND r.sku=? LIMIT 1",
                       (user_id, shop_id, marketplace_id, sku)).fetchone()
    return None if row is not None else "listing_not_found"


def _product_choices(conn, user_id, shop_id, marketplace_id, *, include_title=True):
    columns = "p.sku, p.title" if include_title else "p.sku"
    return [dict(row) for row in conn.execute(
        f"SELECT DISTINCT {columns} {_PRODUCTS} AND r.shop_id=? AND r.marketplace_id=? ORDER BY p.sku",
        (user_id, shop_id, marketplace_id),
    )]


def _readable_product_scopes(conn, user_id, skus):
    if not skus:
        return []
    placeholders = ",".join("?" for _ in skus)
    return [tuple(row) for row in conn.execute(
        f"""SELECT DISTINCT p.shop_id, p.marketplace_id, p.sku {_PRODUCTS}
            AND p.sku COLLATE NOCASE IN ({placeholders})""", (user_id, *skus),
    )]


def _readable_product_catalog(conn, user_id):
    return listing._product_catalog(conn, user_id, _PRODUCTS)


@contextmanager
def evaluation_access():
    with (
        patch.object(rag, "source_regions", source_regions),
        patch.object(store, "AUTHORIZED", store._CHUNK_RELEASES + _READABLE),
        patch.object(listing, "scope_error", _scope_error),
        patch.object(listing, "product_choices", _product_choices),
        patch.object(listing, "readable_product_scopes", _readable_product_scopes),
        patch.object(listing, "readable_product_catalog", _readable_product_catalog),
    ):
        yield


def verify_inventory(manifest, inventory):
    paths = {doc["file"]: doc for doc in inventory["documents"]}
    accounts = manifest["accounts"]
    if (len(paths) != len(inventory["documents"]) or len(accounts) != len(set(accounts))
            or len(accounts) != len(inventory["accounts"]) or not accounts
            or any(not isinstance(account, str) or not account.strip() for account in accounts)):
        raise ValueError("Use the original inventory's unique files and accounts")
    original_accounts = inventory["accounts"]
    if isinstance(original_accounts, dict) or all(isinstance(account, str) for account in original_accounts):
        if set(accounts) != set(original_accounts):
            raise ValueError("Account aliases differ from the original inventory")
    seen = set()
    for release in manifest["releases"]:
        for doc in release["documents"]:
            path = doc["file"]
            original = paths.get(path)
            readers = doc["read_accounts"]
            if (original is None or path in seen
                    or rag._scope(release["target"]) != (original["shop"], original["site"], original["sku"])
                    or doc["kind"] != KINDS[original["kind"]] or len(readers) != len(set(readers))
                    or set(readers) != set(original["read_accounts"]) or not set(readers) <= set(accounts)):
                raise ValueError("Document scope, type or read grants differ from the original inventory")
            seen.add(path)
    if seen != paths.keys():
        raise ValueError("The local manifest must include every original inventory file exactly once")


def import_fixture(conn, manifest, source_dir, *, embedding=None):
    with conn:
        conn.execute("""CREATE TABLE sealed_product_readers (
            source_id INTEGER NOT NULL REFERENCES product_documents(id),
            user_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY (source_id, user_id)
        )""")
        catalog_id = conn.execute("INSERT INTO users (username, password) VALUES (?, ?)",
                                  ("sealed-catalog", token_urlsafe(48))).lastrowid
        password = database.hash_password(PASSWORD)
        users = {name: conn.execute("INSERT INTO users (username, password) VALUES (?, ?)",
                                   (f"product-evidence-{name}", password)).lastrowid
                 for name in manifest["accounts"]}
    for item in manifest["catalog"]:
        target = item["target"]
        listing.seed_test_product(conn, catalog_id, **target)
        scope = rag._scope(target)
        with conn:
            conn.execute("UPDATE listing_test_products SET title=? WHERE shop_id=? AND marketplace_id=? AND sku=?",
                         (item["title"], *scope))
            aliases = [(*scope, alias) for alias in item.get("product_aliases", ())]
            if display_name := item.get("shop_display_name"):
                aliases.append((*scope[:2], "", display_name))
            conn.executemany("INSERT OR IGNORE INTO listing_test_catalog_aliases VALUES (?, ?, ?, ?)", aliases)
    for release in manifest["releases"]:
        if not rag.import_release(conn, release, source_dir, embedding=embedding):
            raise ValueError("Duplicate release in sealed fixture")
        with conn:
            for doc in release["documents"]:
                row = conn.execute(
                    """SELECT d.id FROM product_documents d JOIN product_document_releases r ON r.id=d.release_id
                       WHERE r.shop_id=? AND r.marketplace_id=? AND r.sku=? AND r.version=? AND d.document_id=?""",
                    (*rag._scope(release["target"]), release["version"], doc["id"]),
                ).fetchone()
                conn.executemany("INSERT INTO sealed_product_readers (source_id, user_id) VALUES (?, ?)",
                                 [(row["id"], users[reader]) for reader in doc["read_accounts"]])
    return users


def active_source_chunks(conn, target):
    return conn.execute(
        """SELECT d.document_id, MIN(c.chunk_id) AS chunk_id
           FROM product_document_releases r JOIN product_documents d ON d.release_id=r.id
           JOIN product_document_chunks c ON c.source_id=d.id
           WHERE r.active=1 AND r.shop_id=? AND r.marketplace_id=? AND r.sku=? AND c.strategy='structured'
           GROUP BY d.id ORDER BY d.document_id""", rag._scope(target),
    ).fetchall()


def switch_snapshot(conn, active):
    with conn:
        conn.execute("UPDATE product_document_releases SET active=0")
        conn.executemany(
            """UPDATE product_document_releases SET active=1
               WHERE shop_id=? AND marketplace_id=? AND sku=? AND version=?""",
            [(*scope, version) for scope, version in active.items()],
        )


def preflight(manifest, source_dir, snapshots):
    docs = {doc["file"]: doc for release in manifest["releases"] for doc in release["documents"]}
    with closing(sqlite3.connect(":memory:")) as conn, evaluation_access():
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT)")
        for statement in (*listing.SCHEMA, *store.SCHEMA):
            conn.execute(statement)
        users = import_fixture(conn, manifest, source_dir, embedding=lambda texts: [[1.0, 0.0]] * len(texts))
        sources = conn.execute(
            """SELECT r.shop_id, r.marketplace_id, r.sku, r.version, d.filename, d.original, d.regions,
                      c.chunk_id, c.start_offset, c.end_offset, c.content
               FROM product_documents d JOIN product_document_releases r ON r.id=d.release_id
               JOIN product_document_chunks c ON d.id=c.source_id WHERE c.strategy='structured'""",
        ).fetchall()
        hashes, coverage = {}, Counter()
        for row in sources:
            path = row["filename"]
            raw = (source_dir / path).read_bytes()
            offsets = row["start_offset"], row["end_offset"]
            regions = json.loads(row["regions"])
            if (raw != row["original"].encode("utf-8") or row["content"] != row["original"][slice(*offsets)]
                    or offsets not in {(r["start_offset"], r["end_offset"]) for r in regions}):
                raise ValueError("Source bytes or structured chunk offsets changed during import")
            hashes[path] = sha256(raw).hexdigest()
            coverage[path] += 1
        if coverage.keys() != docs.keys():
            raise ValueError("An original file did not produce structured chunks")
        permissions = Counter()
        for active in snapshots.values():
            switch_snapshot(conn, active)
            allowed_skus = set()
            for row in sources:
                scope = tuple(row[key] for key in store.SCOPE_KEYS)
                readers = docs[row["filename"]]["read_accounts"]
                for name, uid in users.items():
                    granted = row["version"] == active.get(scope) and name in readers
                    if (store.scoped_chunk(conn, uid, row["chunk_id"]) is not None) != granted:
                        raise ValueError("Read check differs from the frozen grant or active release")
                    permissions["allowed" if granted else "denied"] += 1
                    if granted:
                        allowed_skus.add((uid, scope))
            for uid in users.values():
                for scope in active:
                    allowed = (uid, scope) in allowed_skus
                    choices = listing.product_choices(conn, uid, *scope[:2], include_title=False)
                    if ((listing.scope_error(conn, uid, *scope) is None) != allowed
                            or any(choice["sku"] == scope[2] for choice in choices) != allowed):
                        raise ValueError("SKU scope or choices differ from the frozen read grants")
    originals_sha256 = sha256(json.dumps(hashes, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {"documents": dict(Counter(doc["kind"] for doc in docs.values())), "accounts": len(users),
            "snapshots": len(snapshots), "structured_chunks": len(sources), "read_checks": dict(permissions),
            "originals_sha256": originals_sha256, "model_requests": 0}
