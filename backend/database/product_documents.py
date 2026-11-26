"""Persistent, immutable source releases for the SQLite listing catalog."""

import json

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS product_document_releases (
        id INTEGER PRIMARY KEY, shop_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, sku TEXT NOT NULL,
        version TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
        fingerprint TEXT NOT NULL, embedding_model TEXT NOT NULL, evidence_rules TEXT NOT NULL,
        UNIQUE (shop_id, marketplace_id, sku, version),
        FOREIGN KEY (shop_id, marketplace_id, sku) REFERENCES listing_test_products (shop_id, marketplace_id, sku)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS product_document_active_release
        ON product_document_releases (shop_id, marketplace_id, sku) WHERE active=1""",
    """CREATE TABLE IF NOT EXISTS product_documents (
        id INTEGER PRIMARY KEY, release_id INTEGER NOT NULL REFERENCES product_document_releases (id),
        document_id TEXT NOT NULL, kind TEXT NOT NULL, filename TEXT NOT NULL, original TEXT NOT NULL,
        regions TEXT NOT NULL, UNIQUE (release_id, document_id)
    )""",
    """CREATE TABLE IF NOT EXISTS product_document_chunks (
        chunk_id TEXT PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES product_documents (id),
        strategy TEXT NOT NULL CHECK (strategy IN ('fixed', 'structured')),
        start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, content TEXT NOT NULL, embedding BLOB NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS product_document_chunks_source ON product_document_chunks (source_id, strategy)""",
)

SCOPE_KEYS = ("shop_id", "marketplace_id", "sku")

_CHUNK_RELEASES = """
    FROM product_document_chunks c
    JOIN product_documents d ON d.id=c.source_id
    JOIN product_document_releases r ON r.id=d.release_id
"""

# Both read paths join the current product and its owner in the same snapshot.
AUTHORIZED = _CHUNK_RELEASES + """
    JOIN listing_test_products p USING (shop_id, marketplace_id, sku)
    JOIN listing_test_shops s USING (shop_id, marketplace_id)
    WHERE s.user_id=? AND r.active=1
"""

CHUNK_COLUMNS = """
    c.chunk_id, c.strategy, c.start_offset, c.end_offset, c.content,
    d.document_id, d.kind, d.filename, d.regions,
    r.shop_id, r.marketplace_id, r.sku, r.version, r.embedding_model
"""


def scoped_chunks(conn, user_id, scope, strategy):
    return conn.execute(
        f"""SELECT {CHUNK_COLUMNS}, c.embedding, r.evidence_rules {AUTHORIZED}
            AND r.shop_id=? AND r.marketplace_id=? AND r.sku=? AND c.strategy=? ORDER BY c.chunk_id""",
        (user_id, *scope, strategy),
    ).fetchall()


def scoped_chunk(conn, user_id, chunk_id):
    return conn.execute(
        f"SELECT {CHUNK_COLUMNS}, d.original {AUTHORIZED} AND c.chunk_id=?",
        (user_id, chunk_id),
    ).fetchone()


def fact_source(conn, user_id, scope, original):
    """Resolve a stored fact's file and row to one readable structured source."""
    if (not isinstance(original, dict) or type(original.get("line")) is not int
            or original["line"] < 1 or not isinstance(original.get("file"), str)
            or not isinstance(original.get("release"), str)):
        return None
    location = f"row:{original['line']}"
    matches = []
    for row in conn.execute(
        f"""SELECT {CHUNK_COLUMNS}, d.original, r.fingerprint {AUTHORIZED}
            AND r.shop_id=? AND r.marketplace_id=? AND r.sku=? AND r.version=?
            AND d.filename=? AND c.strategy='structured'""",
        (user_id, *scope, original["release"], original["file"]),
    ):
        for region in json.loads(row["regions"]):
            start, end = region["start_offset"], region["end_offset"]
            if (region["location"] == location and row["start_offset"] <= start < end <= row["end_offset"]
                    and row["content"] == row["original"][row["start_offset"]:row["end_offset"]]):
                matches.append({"release": row["version"], "fingerprint": row["fingerprint"],
                                "document_id": row["document_id"], "chunk_id": row["chunk_id"],
                                "file": row["filename"], "line": original["line"],
                                "start_offset": start, "end_offset": end})
    return matches[0] if len(matches) == 1 else None


def visible_receipt_headers(conn, user_id, chunk_ids):
    """Return receipt headings for the chunks the current user can look up."""
    scope = AUTHORIZED + " AND EXISTS (SELECT 1 FROM users u WHERE u.id=s.user_id) AND"
    return _receipt_headers(conn, chunk_ids, scope, (user_id,))


def stored_receipt_headers(conn, chunk_ids):
    """Locate complete stored headings for redaction, independent of current access."""
    return _receipt_headers(conn, chunk_ids, _CHUNK_RELEASES + " WHERE", ())


def _receipt_headers(conn, chunk_ids, scope, params):
    ids = sorted(chunk_ids)
    headers = {}
    # Legacy group history can span more than one SQLite bind-variable batch.
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT c.chunk_id, r.sku, r.version {scope} c.chunk_id IN ({placeholders})",
            (*params, *batch),
        )
        headers.update((row["chunk_id"], f"{row['sku']} · {row['version']}") for row in rows)
    return headers


def active_version(conn, scope):
    release = conn.execute(
        """SELECT version FROM product_document_releases
           WHERE shop_id=? AND marketplace_id=? AND sku=? AND active=1""",
        scope,
    ).fetchone()
    return release["version"] if release else None


def current_chunk_ids(conn, user_id, scope, version, chunk_ids):
    if not chunk_ids:
        return set()
    placeholders = ",".join("?" for _ in chunk_ids)
    return {row["chunk_id"] for row in conn.execute(
        f"""SELECT c.chunk_id {AUTHORIZED}
            AND r.shop_id=? AND r.marketplace_id=? AND r.sku=? AND r.version=?
            AND c.strategy='structured' AND c.chunk_id IN ({placeholders})""",
        (user_id, *scope, version, *chunk_ids),
    )}
