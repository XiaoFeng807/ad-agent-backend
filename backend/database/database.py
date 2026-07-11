"""数据库模块 — 建表 + 初始化数据"""
import sqlite3, os, hashlib

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ad_agent.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def init_db():
    """创建所有表（幂等：IF NOT EXISTS）"""
    conn = get_db()
    c = conn.cursor()

    tables = [
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        """CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            message TEXT,
            level TEXT DEFAULT "warning",
            is_read INTEGER DEFAULT 0,
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    for sql in tables:
        c.execute(sql)
    conn.commit()
    conn.close()


def seed_data():
    """初始化示例数据（幂等：已有数据则跳过）"""
    conn = get_db()
    c = conn.cursor()

    # 用户
    exist = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if exist == 0:
        users = [
            ("boss", hash_password("admin123"), "boss"),
            ("admin", hash_password("admin123"), "admin"),
            ("zhangsan", hash_password("user123"), "user"),
            ("lisi", hash_password("user123"), "user"),
        ]
        c.executemany("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", users)

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
            "INSERT INTO ad_accounts (account_name, platform, balance, daily_budget, status, user_id) VALUES (?,?,?,?,?,?)",
            accounts
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

    # 每日报告（最近30天）
    exist = c.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0]
    if exist == 0:
        import random
        from datetime import datetime, timedelta
        base_cost = 1500
        base_imp = 30000
        base_clicks = 600
        base_sales = 4000
        for day_offset in range(30):
            date = (datetime.now() - timedelta(days=29 - day_offset)).strftime("%Y-%m-%d")
            for uid in range(1, 5):
                cost = base_cost + random.randint(-200, 200)
                imp = base_imp + random.randint(-2000, 2000)
                clicks = base_clicks + random.randint(-50, 50)
                sales = base_sales + random.randint(-500, 500)
                c.execute(
                    "INSERT OR IGNORE INTO daily_reports (report_date, cost, impressions, clicks, sales, user_id) VALUES (?,?,?,?,?,?)",
                    (date, max(0, cost), max(0, imp), max(0, clicks), max(0, sales), uid)
                )

    conn.commit()
    conn.close()