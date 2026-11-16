"""Connections for the opt-in, loopback-only diagnosis rehearsal."""

from contextlib import closing
from decimal import Decimal, InvalidOperation
import re
from pathlib import Path

from backend.config.config import settings
from backend.database.mysql_r2_schema import (
    R2_AUTO_IDS, R2_COLUMNS, R2_FOREIGN_KEYS, R2_PRIMARY_KEYS, R2_SCHEMA, R2_UNIQUE_KEYS, R2_VERSION_TABLE,
)


SCHEMA = "mysql_r1"
USER = "mysql_r1_app"
TABLES = frozenset({
    "users", "sessions", "ad_accounts", "ad_plans", "daily_reports", "plan_metric_bindings",
    "diagnosis_tasks", "diagnosis_turns", "diagnosis_review_proposals", "diagnosis_reviews",
})


class MySQLStorageError(RuntimeError):
    """A diagnosis database operation failed."""


class MySQLDiagnosisConnection:
    def __init__(self, connection, driver_error):
        self._connection = connection
        self._driver_error = driver_error
        self._cursor = connection.cursor()

    def _call(self, operation, *args):
        try:
            return operation(*args)
        except self._driver_error as error:
            code = error.args[0] if error.args and type(error.args[0]) is int else None
            raise MySQLStorageError("MySQL diagnosis operation failed", code) from None

    def execute(self, sql, params=()):
        # Diagnosis statements use positional binds and have no literal question marks.
        self._call(self._cursor.execute, sql.replace("?", "%s"), params)
        return self._cursor

    def begin(self):
        self._call(self._connection.begin)

    def commit(self):
        self._call(self._connection.commit)

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is not None:
            self._call(self._connection.rollback)
            return
        try:
            self.commit()
        except MySQLStorageError:
            self._call(self._connection.rollback)
            raise


def is_mysql(conn):
    return isinstance(conn, MySQLDiagnosisConnection)


def schema_name():
    return R2_SCHEMA if settings.DIAGNOSIS_MYSQL_SCHEMA_VERSION == "2" else SCHEMA


def chat_enabled():
    return (settings.DIAGNOSIS_MYSQL_ENABLED and schema_name() == R2_SCHEMA
            and bool(settings.MYSQL_CHAT_MEMORY_ROOT))


def pause_enabled():
    return (settings.DIAGNOSIS_MYSQL_ENABLED and schema_name() == R2_SCHEMA
            and settings.DIAGNOSIS_MYSQL_PAUSE_ENABLED)


def chat_memory_root(conn):
    root = Path(settings.MYSQL_CHAT_MEMORY_ROOT)
    if not chat_enabled() or not root.is_absolute():
        raise MySQLStorageError("MySQL chat requires an absolute MYSQL_CHAT_MEMORY_ROOT")
    server_id = conn.execute("SELECT @@server_uuid AS id").fetchone()["id"]
    if not re.fullmatch(r"[0-9a-f-]{36}", server_id):
        raise MySQLStorageError("Invalid MySQL server UUID")
    return root.resolve() / R2_SCHEMA / server_id


def connect(*, read_only=False):
    port = settings.DIAGNOSIS_MYSQL_PORT
    password = settings.DIAGNOSIS_MYSQL_PASSWORD.get_secret_value()
    if not settings.DIAGNOSIS_MYSQL_ENABLED or not 1 <= port <= 65535 or not password:
        raise MySQLStorageError("Enable the diagnosis MySQL rehearsal and configure its port and password")
    if settings.DIAGNOSIS_SANDBOX_DB_PATH:
        raise MySQLStorageError("Disable the SQLite pause sandbox for the diagnosis MySQL rehearsal")
    try:
        import pymysql
    except ImportError:
        raise MySQLStorageError("The diagnosis MySQL rehearsal requires PyMySQL") from None
    try:
        connection = pymysql.connect(
            host="127.0.0.1", port=port, database=schema_name(), user=USER, password=password,
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False,
            connect_timeout=3, read_timeout=20, write_timeout=20,
            init_command="SET time_zone = '+00:00', transaction_isolation = 'REPEATABLE-READ', "
                         "innodb_lock_wait_timeout = 10",
        )
    except pymysql.MySQLError as error:
        code = error.args[0] if error.args and type(error.args[0]) is int else None
        raise MySQLStorageError("Cannot connect to the diagnosis MySQL rehearsal", code) from None
    conn = MySQLDiagnosisConnection(connection, pymysql.MySQLError)
    if read_only:
        try:
            conn.execute("SET SESSION TRANSACTION READ ONLY")
        except MySQLStorageError:
            conn.close()
            raise
    return conn


def validate_schema():
    with closing(connect(read_only=True)) as conn:
        validate_schema_connection(conn, schema_name())


def validate_schema_connection(conn, schema):
    from backend.database.chat_groups import TABLE_COLUMNS as group_columns

    group_columns = dict(group_columns)

    server = conn.execute(
        "SELECT DATABASE() AS db, VERSION() AS version, @@foreign_key_checks AS foreign_keys"
    ).fetchone()
    tables = conn.execute(
        "SELECT TABLE_NAME AS name, ENGINE AS engine FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"
    ).fetchall()
    expected = TABLES if schema == SCHEMA else {*R2_COLUMNS, R2_VERSION_TABLE}
    engines = {row["name"]: row["engine"] for row in tables}
    if schema == R2_SCHEMA and "conversation_turns" in engines:
        expected = expected | {"conversation_turns"}
        # Optional additive upgrade; old R2 schemas still validate for non-chat callers.
        turn_columns = {"conversation_turns": ("user_message_id", "assistant_message_id", "user_id")}
    else:
        turn_columns = {}
    groups = schema == R2_SCHEMA and group_columns.keys() <= engines.keys()
    if groups:
        expected = expected | group_columns.keys()
    if (schema not in (SCHEMA, R2_SCHEMA) or server["db"] != schema
            or not re.fullmatch(r"8\.4\.\d+(?:-commercial)?", server["version"]) or server["foreign_keys"] != 1
            or engines != dict.fromkeys(expected, "InnoDB")):
        raise MySQLStorageError("Expected the selected MySQL 8.4 diagnosis schema with InnoDB and foreign keys")
    if schema == R2_SCHEMA:
        _validate_r2(conn, {**(group_columns if groups else {}), **turn_columns})


def _validate_r2(conn, group_columns):
    version = conn.execute(f"SELECT version FROM {R2_VERSION_TABLE}").fetchall()
    columns = conn.execute(
        "SELECT TABLE_NAME AS tab, COLUMN_NAME AS name, DATA_TYPE AS kind, EXTRA AS extra "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() ORDER BY ORDINAL_POSITION"
    ).fetchall()
    indexes = conn.execute(
        """SELECT TABLE_NAME AS tab, INDEX_NAME AS name, NON_UNIQUE AS non_unique,
                  GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS cols
           FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE()
           GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE"""
    ).fetchall()
    relations = conn.execute(
        """SELECT k.TABLE_NAME AS tab, GROUP_CONCAT(k.COLUMN_NAME ORDER BY k.ORDINAL_POSITION) AS cols,
                  k.REFERENCED_TABLE_NAME AS parent,
                  GROUP_CONCAT(k.REFERENCED_COLUMN_NAME ORDER BY k.ORDINAL_POSITION) AS parent_cols,
                  r.DELETE_RULE AS delete_rule
           FROM information_schema.KEY_COLUMN_USAGE k
           JOIN information_schema.REFERENTIAL_CONSTRAINTS r
             ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.TABLE_NAME=k.TABLE_NAME
                AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME
           WHERE k.TABLE_SCHEMA=DATABASE()
           GROUP BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.REFERENCED_TABLE_NAME, r.DELETE_RULE"""
    ).fetchall()
    shape = {}
    for row in columns:
        shape.setdefault(row["tab"], []).append(row["name"])
    if "conversation_turns" in shape:
        turn_indexes = [r for r in indexes if r["tab"] == "conversation_turns"]
        turn_foreign = {(r["cols"], r["parent"], r["parent_cols"]) for r in relations
                        if r["tab"] == "conversation_turns"}
        if (not any(r["name"] == "PRIMARY" and r["cols"] == "user_message_id" for r in turn_indexes)
                or not any(not r["non_unique"] and r["cols"] == "assistant_message_id" for r in turn_indexes)
                or turn_foreign != {("user_message_id", "conversations", "id"),
                                    ("assistant_message_id", "conversations", "id")}):
            raise MySQLStorageError("Expected the TASK-025 committed turn keys")
    if any(tuple(shape.pop(table, ())) != fields for table, fields in group_columns.items()):
        raise MySQLStorageError("Expected the TASK-027 chat group columns")
    columns = [row for row in columns if row["tab"] not in group_columns]
    indexes = [row for row in indexes if row["tab"] not in group_columns]
    relations = [row for row in relations if row["tab"] not in group_columns]
    primary = {row["tab"]: tuple(row["cols"].split(",")) for row in indexes if row["name"] == "PRIMARY"}
    unique = {(row["tab"], row["cols"]) for row in indexes if row["name"] != "PRIMARY" and not row["non_unique"]}
    named = {(row["tab"], row["name"], row["cols"]) for row in indexes}
    foreign = {(row["tab"], row["cols"], row["parent"], row["parent_cols"], row["delete_rule"]) for row in relations}
    auto = {(row["tab"], row["name"]) for row in columns if row["extra"] == "auto_increment"}
    if (version != [{"version": 2}]
            or {k: tuple(v) for k, v in shape.items()} != {**R2_COLUMNS, R2_VERSION_TABLE: ("version",)}
            or primary != {**R2_PRIMARY_KEYS, R2_VERSION_TABLE: ("version",)}
            or unique != R2_UNIQUE_KEYS or foreign != R2_FOREIGN_KEYS
            or auto != {(table, "id") for table in R2_AUTO_IDS}
            or ("ad_plans", "owner_status_plan", "user_id,status,id") not in named
            or ("diagnosis_turns", "diagnosis_turns_task", "user_id,task_id,id") not in named
            or not any(row["tab"] == "diagnosis_tasks" and row["name"] == "plan_id" and row["kind"] == "json"
                       for row in columns)):
        raise MySQLStorageError("Expected the MYSQL-R2 version, columns, keys and diagnosis indexes")


def plan_key(value):
    # Match numeric plan IDs without MySQL's permissive string-to-number coercion.
    if isinstance(value, str) and re.fullmatch(
        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value.strip()
    ):
        try:
            value = Decimal(value.strip())
        except InvalidOperation:
            return None
    if type(value) not in (int, Decimal) or not -(2**63) <= value < 2**63:
        return None
    return int(value) if value == int(value) else None
