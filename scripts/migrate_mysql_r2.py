"""Import a quiesced, sanitized SQLite copy into the isolated mysql_r2 schema."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3

import pymysql

from backend.database.database import DEFAULT_DB_PATH
from backend.database.mysql_diagnosis import MySQLDiagnosisConnection, MySQLStorageError, validate_schema_connection
from backend.database.mysql_r2_schema import R2_AUTO_IDS, R2_COLUMNS, R2_PRIMARY_KEYS, R2_SCHEMA


BASE_TABLES = frozenset(tuple(R2_COLUMNS)[:11])
LEGACY_COLUMNS = {
    "conversations": {"priority": 1, "chat_view_id": None},
    "diagnosis_tasks": {"mode": "structured"},
    "diagnosis_turns": {"snapshot": None},
}
LOCK = "mysql_r2:sqlite_import"
DDL = Path(__file__).with_name("mysql_r2_schema.sql")
I64_MAX = 2**63 - 1
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
                       r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?")


class MigrationError(RuntimeError):
    """A migration failure whose message contains only diagnostic metadata."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def source_digest(path):
    if any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise MigrationError("Source must be a standalone SQLite backup without journal sidecars")
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


@contextmanager
def open_source(path):
    if path == DEFAULT_DB_PATH or (DEFAULT_DB_PATH.exists() and path.samefile(DEFAULT_DB_PATH)):
        raise MigrationError("Use a sanitized copy in place of the default business SQLite file")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MigrationError("SQLite quick_check failed")
        yield conn


def inventory(conn):
    return {
        table: {
            "columns": [dict(row) for row in conn.execute("SELECT * FROM pragma_table_xinfo(?)", (table,))],
            "foreign_keys": [dict(row) for row in conn.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,))],
            "indexes": [
                {**dict(index), "columns": [dict(row) for row in conn.execute(
                    "SELECT * FROM pragma_index_info(?)", (index["name"],),
                )]}
                for index in conn.execute("SELECT * FROM pragma_index_list(?)", (table,))
            ],
            "ddl": [dict(row) for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_schema WHERE tbl_name=? ORDER BY type, name", (table,),
            )],
        }
        for (table,) in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")
        if not table.startswith("sqlite_")
    }


def check_source_shape(schema, evidence):
    unknown = set(schema) - R2_COLUMNS.keys()
    missing = BASE_TABLES - schema.keys()
    if unknown or missing:
        raise MigrationError(f"Unsupported SQLite table set. Unknown={sorted(unknown)}, missing base={sorted(missing)}")
    for table, columns in R2_COLUMNS.items():
        if table not in schema:
            evidence[table]["source_status"] = "absent_in_legacy_schema"
            continue
        existing = {row["name"]: row["pk"] for row in schema[table]["columns"]}
        primary = tuple(sorted((name for name in existing if existing[name]), key=existing.get))
        added = set(columns) - existing.keys()
        if (primary != R2_PRIMARY_KEYS[table] or added - LEGACY_COLUMNS.get(table, {}).keys()
                or existing.keys() - set(columns) or any(row["hidden"] for row in schema[table]["columns"])):
            raise MigrationError(f"Unsupported SQLite column or primary key shape in {table}")
        evidence[table]["source_status"] = "present"
        evidence[table]["legacy_defaults"] = {name: LEGACY_COLUMNS[table][name] for name in columns if name in added}


def target_metadata(cursor):
    cursor.execute(
        "SELECT TABLE_NAME AS tab, COLUMN_NAME AS col, DATA_TYPE AS kind, IS_NULLABLE AS nullable, "
        "CHARACTER_MAXIMUM_LENGTH AS width FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()"
    )
    columns = {(row["tab"], row["col"]): row for row in cursor.fetchall()}
    cursor.execute(
        "SELECT TABLE_NAME AS tab, CONSTRAINT_NAME AS name, COLUMN_NAME AS col, "
        "REFERENCED_TABLE_NAME AS parent, REFERENCED_COLUMN_NAME AS parent_col "
        "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() "
        "AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY ORDINAL_POSITION"
    )
    constraints = {}
    for row in cursor.fetchall():
        constraints.setdefault((row["tab"], row["parent"], row["name"]), []).append((row["col"], row["parent_col"]))
    return columns, constraints


def timestamp(value):
    if isinstance(value, str):
        if not TIMESTAMP.fullmatch(value) or (value[-6:-5] in ("+", "-") and int(value[-2:]) >= 60):
            raise ValueError("expected ISO/SQLite timestamp")
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("expected datetime")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    if value.year < 1000:
        raise ValueError("outside MySQL DATETIME range")
    return value.isoformat(timespec="microseconds") + "Z"


def invalid_json_constant(_value):
    raise ValueError("non-finite JSON constant")


def normalized(value, meta, *, source):
    if value is None:
        if meta["nullable"] != "YES":
            raise ValueError("NULL in required column")
        return None
    kind, column = meta["kind"], meta["col"]
    if kind == "datetime":
        return timestamp(value)
    if kind == "json":
        value = value if source else json.loads(value)
        if not (type(value) is int or isinstance(value, str)):
            raise ValueError("expected SQLite INTEGER or TEXT plan ID")
        return value
    if kind == "bigint":
        if type(value) is not int or not -2**63 <= value <= I64_MAX:
            raise ValueError("expected signed 64-bit integer")
        return value
    if kind == "double":
        if type(value) not in (int, float) or not math.isfinite(value) or (type(value) is int and float(value) != value):
            raise ValueError("expected finite DOUBLE")
        return float(value) if value else 0.0
    if kind not in ("varchar", "text", "longtext") or not isinstance(value, str):
        raise ValueError("expected compatible text")
    if kind == "varchar" and len(value) > meta["width"]:
        raise ValueError("text exceeds MySQL column width")
    if column == "snapshot":
        try:
            snapshot = json.loads(value, parse_constant=invalid_json_constant)
        except json.JSONDecodeError:
            raise ValueError("invalid JSON snapshot") from None
        if not isinstance(snapshot, dict):
            raise ValueError("expected JSON object snapshot")
    return value


def primary_key(table, row):
    return tuple(row[col] for col in R2_PRIMARY_KEYS[table])


def report_anomaly(evidence, table, key, field, reason):
    key = tuple(str(value) if isinstance(value, bytes) or (type(value) is float and not math.isfinite(value))
                else value for value in key)
    evidence[table]["anomalies"].append({"primary_key": key, "field": field, "reason": reason})


def read_source(conn, metadata, schema, evidence):
    data = {table: [] for table in R2_COLUMNS}
    original = {table: [] for table in R2_COLUMNS}
    for table, columns in R2_COLUMNS.items():
        evidence[table]["source_row_count"] = 0
        if table not in schema:
            continue
        defaults = evidence[table]["legacy_defaults"]
        projection = ", ".join("NULL AS token" if col == "token" else col for col in columns if col not in defaults)
        for row in conn.execute(f"SELECT {projection} FROM {table}"):
            evidence[table]["source_row_count"] += 1
            values = {**dict(row), **defaults}
            original[table].append(values)
            cleaned = {}
            for col in columns:
                try:
                    cleaned[col] = normalized(values[col], metadata[table, col], source=True)
                except (ValueError, OverflowError, RecursionError) as error:
                    reason = error.args[0] if type(error) is ValueError else "invalid or unsupported value"
                    report_anomaly(evidence, table, primary_key(table, values), col, reason)
            if len(cleaned) == len(columns):
                data[table].append(cleaned)
        if table == "sessions":
            evidence[table]["tokens_redacted"] = conn.execute("SELECT COUNT(token) FROM sessions").fetchone()[0]
    return data, original


def verify_relations(data, original, constraints, evidence):
    for (table, parent, name), columns in constraints.items():
        parent_keys = {tuple(row[parent_col] for _, parent_col in columns) for row in original[parent]}
        for row in original[table]:
            values = tuple(row[col] for col, _ in columns)
            if None not in values and values not in parent_keys:
                report_anomaly(evidence, table, primary_key(table, row), name, f"orphan reference to {parent}")

    plans = {row["id"]: row for row in data["ad_plans"]}
    turns = {row["id"]: row for row in data["diagnosis_turns"]}
    proposals = {row["action_id"]: row for row in data["diagnosis_review_proposals"]}
    for row in data["diagnosis_sandbox_targets"]:
        plan = plans.get(row["plan_id"])
        if plan is not None and plan["user_id"] != row["user_id"]:
            report_anomaly(evidence, "diagnosis_sandbox_targets", (row["plan_id"],), "user_id", "plan owner mismatch")
    for row in proposals.values():
        turn = turns.get(row["turn_id"])
        if turn is not None and turn["reply"] is None:
            report_anomaly(evidence, "diagnosis_review_proposals", (row["action_id"],), "turn_id", "turn has no committed reply")
    for row in data["diagnosis_reviews"]:
        proposal = proposals.get(row["action_id"])
        turn = turns.get(row["turn_id"])
        if ((turn is not None and turn["user_id"] != row["user_id"])
                or (proposal is not None and (proposal["turn_id"] != row["turn_id"]
                                              or json.loads(proposal["snapshot"]) != json.loads(row["snapshot"])))):
            report_anomaly(evidence, "diagnosis_reviews", (row["review_id"],), "action_id", "review turn, owner or snapshot mismatch")


def source_watermarks(conn, data):
    sequences = {}
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
        for table, seq in conn.execute("SELECT name, seq FROM sqlite_sequence"):
            if table in R2_AUTO_IDS:
                if table in sequences or type(seq) is not int:
                    raise MigrationError(f"Invalid SQLite allocation history in {table}")
                sequences[table] = seq
    result = {}
    for table in R2_AUTO_IDS:
        max_id = max((row["id"] for row in data[table]), default=0)
        floor = max(0, max_id, sequences.get(table, 0))
        if floor >= I64_MAX:
            raise MigrationError(f"SQLite historical allocation exhausted in {table}")
        result[table] = {"max_id": max_id, "sqlite_sequence": sequences.get(table), "next_id_min": floor + 1}
    return result


def fingerprint(table, rows):
    ordered = sorted(rows, key=lambda row: primary_key(table, row))
    return {
        "row_count": len(ordered),
        "primary_keys": [primary_key(table, row) for row in ordered],
        "sha256": digest(ordered),
    }


def target_fingerprints(cursor, metadata):
    fingerprints = {}
    for table, columns in R2_COLUMNS.items():
        cursor.execute(f"SELECT {', '.join(columns)} FROM {table}")
        rows = [{col: normalized(row[col], metadata[table, col], source=False) for col in columns}
                for row in cursor.fetchall()]
        fingerprints[table] = fingerprint(table, rows)
    return fingerprints


def read_allocators(cursor):
    cursor.execute(
        "SELECT TABLE_NAME AS tab, AUTO_INCREMENT AS next_id FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND AUTO_INCREMENT IS NOT NULL"
    )
    return {row["tab"]: row["next_id"] for row in cursor.fetchall()}


def insert_values(table, row):
    for col in R2_COLUMNS[table]:
        value = row[col]
        if value is not None:
            if table == "diagnosis_tasks" and col == "plan_id":
                value = canonical(value)
            elif col in ("created_at", "verified_at"):
                value = value[:-1].replace("T", " ")
        yield value


def compare(target, tables):
    for table, actual in target.items():
        source = tables[table]["source"]
        tables[table].update(target=actual, matches=actual == source)
    return all(item["matches"] for item in tables.values())


def import_copy(source, connect, *, after_table=None):
    """Return reconciliation and failure details. Use an offline target for the duration."""
    source = source.resolve()
    tables = {table: {"source": None, "target": None, "matches": False, "anomalies": []} for table in R2_COLUMNS}
    report = {"status": "failed", "phase": "source_open", "tables": tables}
    before = None
    try:
        before = source_digest(source)
        with open_source(source) as sqlite, closing(connect()) as mysql, mysql.cursor() as cursor:
            report["phase"] = "target_guard"
            validate_schema_connection(MySQLDiagnosisConnection(mysql, pymysql.MySQLError), R2_SCHEMA)
            cursor.execute("SELECT GET_LOCK(%s, 15) AS acquired", (LOCK,))
            if cursor.fetchone()["acquired"] != 1:
                raise MigrationError("Cannot acquire the isolated import lock")
            mysql.rollback()
            cursor.execute("SET SESSION time_zone='+00:00', information_schema_stats_expiry=0, "
                           "sql_mode='STRICT_ALL_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,NO_AUTO_VALUE_ON_ZERO'")
            metadata, constraints = target_metadata(cursor)
            report["phase"] = "source_preflight"
            schema = report["sqlite_schema"] = inventory(sqlite)
            check_source_shape(schema, tables)
            data, original = read_source(sqlite, metadata, schema, tables)
            verify_relations(data, original, constraints, tables)
            target = target_fingerprints(cursor, metadata)
            for table, rows in data.items():
                tables[table]["target"] = target[table]
                if len(rows) == tables[table]["source_row_count"]:
                    tables[table]["source"] = fingerprint(table, rows)
            if any(item["anomalies"] for item in tables.values()):
                raise MigrationError("SQLite rows failed preflight")
            matched = compare(target, tables)
            report["phase"] = "target_preflight"
            if not matched and any(item["row_count"] for item in target.values()):
                raise MigrationError("Target contains divergent business rows. Use a new isolated schema")
            watermarks = report["watermarks"] = source_watermarks(sqlite, data)
            current_ids = read_allocators(cursor)
            # MySQL ALTER commits implicitly. Reserve IDs before the DML transaction.
            for table, item in watermarks.items():
                if current_ids[table] < item["next_id_min"]:
                    cursor.execute(f"ALTER TABLE {table} AUTO_INCREMENT = {item['next_id_min']}")
            mysql.rollback()
            mysql.begin()
            try:
                if not matched:
                    report["phase"] = "insert"
                    for table, rows in data.items():
                        columns = R2_COLUMNS[table]
                        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
                        for row in rows:
                            try:
                                cursor.execute(sql, tuple(insert_values(table, row)))
                            except pymysql.MySQLError as error:
                                code = error.args[0] if error.args and isinstance(error.args[0], int) else None
                                report_anomaly(tables, table, primary_key(table, row), None, f"MySQL insert errno={code}")
                                raise
                        if after_table:
                            after_table(table, cursor)
                report["phase"] = "reconcile"
                if not compare(target_fingerprints(cursor, metadata), tables):
                    raise MigrationError("In-transaction reconciliation failed")
                for table, next_id in read_allocators(cursor).items():
                    watermarks[table]["mysql_next_id"] = next_id
                    if next_id < watermarks[table]["next_id_min"]:
                        raise MigrationError(f"MySQL ID allocator is below the source watermark in {table}")
                if source_digest(source) != before:
                    raise MigrationError("SQLite copy changed during migration")
            except Exception:
                mysql.rollback()
                report["rolled_back"] = True
                # Replace any reconciliation against the writer's uncommitted rows.
                compare(target_fingerprints(cursor, metadata), tables)
                raise
            report["phase"] = "commit"
            mysql.commit()
            report["phase"] = "post_commit_read"
            with closing(connect()) as reader, reader.cursor() as independent:
                independent.execute("SET time_zone='+00:00'")
                independent.execute("SELECT CONNECTION_ID() AS connection_id")
                report["reconciliation_connection_id"] = independent.fetchone()["connection_id"]
                if not compare(target_fingerprints(independent, metadata), tables):
                    raise MigrationError("Independent post-commit reconciliation failed")
            report.update(status="already_matched" if matched else "imported", phase="complete")
    except (MigrationError, MySQLStorageError, pymysql.MySQLError, sqlite3.Error, OSError, ValueError) as error:
        report["error"] = {"type": type(error).__name__}
        if isinstance(error, MigrationError):
            report["error"]["reason"] = str(error)
        elif error.args and isinstance(error.args[0], int):
            report["error"]["errno"] = error.args[0]
    finally:
        try:
            after = source_digest(source)
        except (OSError, MigrationError):
            after = None
        report["source_file_sha256"] = {"before": before, "after": after}
        if before != after:
            report["status"] = "failed"
            report["source_changed"] = True
        for item in tables.values():
            item["anomaly_rows"] = len({canonical(issue["primary_key"]) for issue in item["anomalies"]})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    password = os.environ.get("MYSQL_R2_IMPORT_PASSWORD")
    if not password or not 1 <= args.port <= 65535:
        parser.error("Set MYSQL_R2_IMPORT_PASSWORD and provide the isolated loopback port")

    def connect():
        return pymysql.connect(host="127.0.0.1", port=args.port, user=args.user, password=password,
                               database=R2_SCHEMA, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                               autocommit=False, connect_timeout=3, read_timeout=30, write_timeout=30)

    with args.report.open("x", encoding="utf-8") as stream:
        report = import_copy(args.source, connect)
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(report["status"])
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
