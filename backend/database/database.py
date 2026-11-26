"""数据库模块 — 建表 + 初始化数据"""
import sqlite3, hashlib
from contextlib import closing
from pathlib import Path

from backend.config.config import settings
from backend.database import mysql_diagnosis
from backend.database.listing_operations import SCHEMA as LISTING_SCHEMA
from backend.database.order_sales import SCHEMA as ORDER_SALES_SCHEMA
from backend.database.orders import SCHEMA as ORDER_SCHEMA
from backend.database.product_documents import SCHEMA as PRODUCT_DOCUMENT_SCHEMA
from backend.database.search_term_reports import SCHEMA as SEARCH_TERM_SCHEMA

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "ad_agent.db"
DB_PATH = str(DEFAULT_DB_PATH)
DEMO_USERS = (
    (1, "boss", "admin123", "boss"),
    (2, "admin", "admin123", "admin"),
    (3, "zhangsan", "user123", "user"),
    (4, "lisi", "user123", "user"),
)


def sandbox_db_path():
    configured = settings.DIAGNOSIS_SANDBOX_DB_PATH
    if not configured:
        return None
    path = Path(configured).resolve()
    protected_paths = [DEFAULT_DB_PATH, Path(DB_PATH)]
    if settings.PLAN_METRICS_DB_PATH:
        protected_paths.append(Path(settings.PLAN_METRICS_DB_PATH))
    for protected in protected_paths:
        if path == protected.resolve() or (path.exists() and protected.exists() and path.samefile(protected)):
            raise ValueError("暂停沙箱必须使用独立的 SQLite 文件")
    return path


def sandbox_memory_root():
    """Keep each sandbox's persistent memory beside its resolved SQLite file."""
    path = sandbox_db_path()
    return path.parent / (path.name + ".memory") if path is not None else None

def get_db(*, read_only=False, must_exist=False, timeout=5.0):
    sandbox = sandbox_db_path()
    if sandbox is not None and sandbox.exists():
        # Recognize importer output before opening a writable connection or enabling WAL.
        with closing(sqlite3.connect(f"{sandbox.as_uri()}?mode=ro", uri=True, timeout=timeout)) as inspection:
            metric_tables = inspection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' "
                "AND name IN ('metadata', 'plan_daily_metrics')"
            ).fetchone()[0]
        if metric_tables == 2:
            raise ValueError("暂停沙箱必须使用独立的 SQLite 文件，不能使用计划历史指标库")
    if read_only or must_exist:
        path = Path(sandbox or DB_PATH).resolve()
        mode = "ro" if read_only else "rw"
        conn = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(sandbox or DB_PATH, timeout=timeout)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def get_diagnosis_db(*, read_only=False):
    connect = mysql_diagnosis.connect if settings.DIAGNOSIS_MYSQL_ENABLED else get_db
    return connect(read_only=read_only) if read_only else connect()


def owns_plan(conn, plan_id, user_id):
    return owns_plans(conn, (plan_id,), user_id)


def owns_plans(conn, plan_ids, user_id):
    if not plan_ids:
        return True
    ids = tuple(map(mysql_diagnosis.plan_key, plan_ids)) if mysql_diagnosis.is_mysql(conn) else tuple(plan_ids)
    marks = ",".join("?" for _ in ids)
    row = conn.execute(f"SELECT count(*) AS owned FROM ad_plans WHERE user_id=? AND id IN ({marks})",
                       (user_id, *ids)).fetchone()
    return row["owned"] == len(ids)


def init_db():
    """创建所有表（幂等：IF NOT EXISTS）"""
    tables = [
        *ORDER_SCHEMA,
        *ORDER_SALES_SCHEMA,
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT "user",
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            priority INTEGER DEFAULT 1,
            chat_view_id TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS diagnosis_tasks (
            user_id INTEGER NOT NULL,
            task_id TEXT NOT NULL,
            plan_id NOT NULL,
            rule_id TEXT NOT NULL,
            baseline_start TEXT NOT NULL,
            baseline_end TEXT NOT NULL,
            current_start TEXT NOT NULL,
            current_end TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'structured',
            PRIMARY KEY (user_id, task_id)
        )""",
        """CREATE TABLE IF NOT EXISTS plan_metric_bindings (
            plan_id INTEGER PRIMARY KEY NOT NULL REFERENCES ad_plans(id),
            source_id TEXT NOT NULL,
            data_version TEXT NOT NULL,
            source_plan_id TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS diagnosis_turns (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            task_id TEXT NOT NULL,
            message TEXT NOT NULL,
            reply TEXT,
            snapshot TEXT,
            FOREIGN KEY (user_id, task_id) REFERENCES diagnosis_tasks (user_id, task_id)
        )""",
        """CREATE INDEX IF NOT EXISTS diagnosis_turns_task
            ON diagnosis_turns (user_id, task_id, id)""",
        """CREATE TABLE IF NOT EXISTS diagnosis_review_proposals (
            action_id TEXT PRIMARY KEY NOT NULL,
            turn_id INTEGER NOT NULL UNIQUE REFERENCES diagnosis_turns (id),
            snapshot TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS diagnosis_reviews (
            review_id TEXT PRIMARY KEY NOT NULL,
            action_id TEXT NOT NULL UNIQUE REFERENCES diagnosis_review_proposals (action_id),
            user_id INTEGER NOT NULL,
            turn_id INTEGER NOT NULL,
            snapshot TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""",
        """CREATE TABLE IF NOT EXISTS diagnosis_sandbox_targets (
            plan_id INTEGER PRIMARY KEY REFERENCES ad_plans(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            registration_id TEXT NOT NULL UNIQUE
        )""",
        """CREATE TABLE IF NOT EXISTS diagnosis_pause_proposals (
            action_id TEXT PRIMARY KEY NOT NULL,
            review_action_id TEXT NOT NULL UNIQUE REFERENCES diagnosis_review_proposals(action_id),
            plan_id INTEGER NOT NULL,
            registration_id TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            expected_status TEXT NOT NULL CHECK (expected_status = 'active')
        )""",
        """CREATE TABLE IF NOT EXISTS diagnosis_pause_receipts (
            receipt_id TEXT PRIMARY KEY NOT NULL,
            action_id TEXT NOT NULL UNIQUE REFERENCES diagnosis_pause_proposals(action_id),
            execution_trace_id TEXT NOT NULL,
            committed_status TEXT NOT NULL CHECK (committed_status = 'paused'),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""",
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS ad_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            balance REAL DEFAULT 0,
            daily_budget REAL DEFAULT 0,
            status TEXT DEFAULT "active",
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS ad_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_name TEXT NOT NULL,
            platform TEXT,
            daily_budget REAL DEFAULT 0,
            status TEXT DEFAULT "active",
            impressions INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            cost REAL DEFAULT 0,
            sales REAL DEFAULT 0,
            start_date TEXT,
            end_date TEXT,
            account_id INTEGER,
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TRIGGER IF NOT EXISTS diagnosis_sandbox_target_deleted
            AFTER DELETE ON ad_plans
            BEGIN DELETE FROM diagnosis_sandbox_targets WHERE plan_id=OLD.id; END""",
        """CREATE TRIGGER IF NOT EXISTS diagnosis_sandbox_target_transferred
            AFTER UPDATE OF user_id ON ad_plans WHEN OLD.user_id IS NOT NEW.user_id
            BEGIN DELETE FROM diagnosis_sandbox_targets WHERE plan_id=OLD.id; END""",
        """CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            message TEXT,
            level TEXT DEFAULT "warning",
            is_read INTEGER DEFAULT 0,
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            plan_id INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT,
            cost REAL DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            sales REAL DEFAULT 0,
            user_id INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS decision_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category TEXT,
            suggestion TEXT,
            status TEXT DEFAULT "pending",
            outcome TEXT,
            effective INTEGER DEFAULT 0,
            verified_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            type TEXT DEFAULT "image",
            url TEXT,
            plan_id INTEGER DEFAULT 0,
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
    ]

    with closing(get_db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for sql in tables:
            conn.execute(sql)
        conversation_columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
        if "priority" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN priority INTEGER DEFAULT 1")
        if "chat_view_id" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN chat_view_id TEXT")
        from backend.database.chat_storage import upgrade_sqlite as upgrade_chat_turns
        upgrade_chat_turns(conn)
        from backend.database.chat_groups import upgrade_sqlite
        upgrade_sqlite(conn)
        for sql in (*LISTING_SCHEMA, *PRODUCT_DOCUMENT_SCHEMA, *SEARCH_TERM_SCHEMA):
            conn.execute(sql)
        turn_columns = {row[1] for row in conn.execute("PRAGMA table_info(diagnosis_turns)")}
        if "snapshot" not in turn_columns:
            conn.execute("ALTER TABLE diagnosis_turns ADD COLUMN snapshot TEXT")
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(diagnosis_tasks)")}
        if "mode" not in task_columns:
            conn.execute("ALTER TABLE diagnosis_tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'structured'")
        alert_columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
        for name, kind in (("plan_id", "INTEGER"), ("ctr_check_key", "TEXT"), ("ctr_check_evidence", "TEXT")):
            if name not in alert_columns:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {kind}")
        conn.execute("CREATE INDEX IF NOT EXISTS alerts_review_period ON alerts(user_id, type, level, created_at)")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS alerts_ctr_check_identity
                      ON alerts(user_id, plan_id, ctr_check_key)""")


def seed_data(*, require_sandbox=False):
    """Seed an explicitly selected demo database or a caller's test fixture."""
    if require_sandbox and sandbox_db_path() is None:
        raise ValueError("Demo startup requires an independent DIAGNOSIS_SANDBOX_DB_PATH")
    with closing(get_db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if require_sandbox:
            _require_demo_database(conn)
        _seed_demo_data(conn)


def _require_demo_database(conn):
    users = {tuple(row) for row in conn.execute("SELECT id, username, role FROM users")}
    demo_users = {(uid, name, role) for uid, name, _, role in DEMO_USERS}
    if users:
        if users != demo_users:
            raise ValueError("Demo startup requires the dedicated demo accounts")
        return

    # An empty users table alone does not identify a new demo database.
    for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
        if name.startswith("sqlite_"):
            continue
        table = '"' + name.replace('"', '""') + '"'
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise ValueError("Demo startup requires an empty database or an existing demo database")


def _seed_demo_data(conn):
    from backend.mock_ad_data.mock_ad_data import generate_mock_daily_reports

    c = conn.cursor()

    # 用户
    exist = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if exist == 0:
        c.executemany(
            "INSERT INTO users (id, username, password, role) VALUES (?, ?, ?, ?)",
            [(uid, name, hash_password(password), role) for uid, name, password, role in DEMO_USERS],
        )

    # 广告账户
    exist = c.execute("SELECT COUNT(*) FROM ad_accounts").fetchone()[0]
    if exist == 0:
        accounts = [
            ("主账户-Google Ads", "Google Ads", 50000, 1000, "active", 1),
            ("主账户-Meta Ads", "Meta Ads", 30000, 800, "active", 1),
            ("主账户-TikTok Ads", "TikTok Ads", 20000, 500, "active", 1),
            ("备用账户", "Google Ads", 5000, 200, "active", 1),
            ("部门账户", "Meta Ads", 15000, 600, "active", 2),
            ("测试账户", "Google Ads", 3000, 100, "active", 3),
        ]
        c.executemany(
            "INSERT INTO ad_accounts (id, account_name, platform, balance, daily_budget, status, user_id) "
            "VALUES (?,?,?,?,?,?,?)",
            [(account_id, *account) for account_id, account in enumerate(accounts, 1)],
        )

    # 广告计划
    exist = c.execute("SELECT COUNT(*) FROM ad_plans").fetchone()[0]
    if exist == 0:
        plans = [
            ("Google搜索-品牌词", "Google Ads", 300, "active", 15000, 420, 3200, 8500, 1, 1),
            ("Google搜索-通用词", "Google Ads", 500, "active", 28000, 680, 5600, 12000, 1, 1),
            ("Meta-再营销", "Meta Ads", 400, "active", 35000, 890, 4800, 15000, 2, 1),
            ("Meta-新客拓展", "Meta Ads", 350, "paused", 12000, 310, 2100, 3000, 2, 1),
            ("TikTok-爆款视频", "TikTok Ads", 300, "active", 68000, 1500, 3900, 9600, 3, 1),
            ("TikTok-达人合作", "TikTok Ads", 250, "active", 42000, 1100, 2800, 7200, 3, 1),
            ("部门A-搜索广告", "Google Ads", 300, "active", 12000, 380, 2800, 6500, 5, 2),
            ("部门B-社媒广告", "Meta Ads", 250, "active", 22000, 560, 3200, 7800, 5, 2),
            ("测试计划A", "Google Ads", 100, "active", 5000, 150, 800, 2000, 6, 3),
            ("测试计划B", "Meta Ads", 100, "active", 8000, 200, 900, 1800, 6, 3),
            ("个人推广-搜索", "Google Ads", 80, "active", 3000, 100, 500, 1200, 6, 4),
        ]
        c.executemany(
            "INSERT INTO ad_plans (plan_name, platform, daily_budget, status, impressions, clicks, cost, sales, account_id, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            plans
        )

    # 告警
    exist = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    if exist == 0:
        alerts = [
            ("roas_drop", "ROAS下降：Google搜索-通用词 ROAS从2.5降至1.8", "danger", 0, 1),
            ("budget_exceed", "预算超支：TikTok-爆款视频 日消耗已达预算的85%", "warning", 0, 1),
            ("impression_drop", "曝光下降：Meta-再营销 曝光量较昨日下降30%", "warning", 0, 1),
            ("account_balance", "账户余额不足：Google Ads-备用 余额仅剩500元", "danger", 0, 1),
            ("plan_paused", "计划暂停：Meta-新客拓展 已被系统自动暂停", "info", 1, 1),
        ]
        c.executemany(
            "INSERT INTO alerts (type, message, level, is_read, user_id) VALUES (?,?,?,?,?)",
            alerts
        )

    generate_mock_daily_reports(c)
