"""数据库模块：创建表、初始化数据、提供数据库连接"""
import sqlite3, hashlib, os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "ad_agent.db")


def get_db():
    """获取数据库连接（每次调用都新建连接）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 让查询结果可以用列名访问
    conn.execute("PRAGMA journal_mode=WAL")  # 提高并发性能
    return conn


def hash_password(password):
    """对密码进行SHA256加密存储"""
    return hashlib.sha256(password.encode()).hexdigest()


def init_db():
    """创建所有数据库表（如果不存在的话）"""
    conn = get_db()
    c = conn.cursor()

    # 用户表
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT "user",
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # 广告账户表
    c.execute("""CREATE TABLE IF NOT EXISTS ad_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_name TEXT NOT NULL,
        platform TEXT NOT NULL,
        balance REAL DEFAULT 0,
        daily_budget REAL DEFAULT 0,
        status TEXT DEFAULT "active",
        user_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # 广告计划表
    c.execute("""CREATE TABLE IF NOT EXISTS ad_plans (
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
    )""")

    # 告警表
    c.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT,
        message TEXT,
        level TEXT DEFAULT "warning",
        is_read INTEGER DEFAULT 0,
        user_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # 操作日志表
    c.execute("""CREATE TABLE IF NOT EXISTS operation_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        detail TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # 对话记录表
    c.execute("""CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # 素材表
    c.execute("""CREATE TABLE IF NOT EXISTS materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        type TEXT DEFAULT "image",
        url TEXT,
        plan_id INTEGER DEFAULT 0,
        user_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # AI决策记录表
    c.execute("""CREATE TABLE IF NOT EXISTS decision_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        category TEXT,
        suggestion TEXT,
        status TEXT DEFAULT "pending",
        outcome TEXT,
        effective INTEGER DEFAULT 0,
        verified_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # 每日数据表
    c.execute("""CREATE TABLE IF NOT EXISTS daily_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_date TEXT,
        cost REAL DEFAULT 0,
        impressions INTEGER DEFAULT 0,
        clicks INTEGER DEFAULT 0,
        sales REAL DEFAULT 0,
        user_id INTEGER
    )""")

    conn.commit()
    conn.close()


def seed_data():
    """插入默认用户和样例数据（只在表为空时执行）"""
    conn = get_db()
    c = conn.cursor()

    # 检查是否已有用户
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        conn.close()
        return

    # 创建4个预设用户
    users = [
        ("boss", hash_password("admin123"), "boss"),
        ("admin", hash_password("admin123"), "admin"),
        ("zhangsan", hash_password("user123"), "user"),
        ("lisi", hash_password("user123"), "user"),
    ]
    c.executemany("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", users)

    # 为每个用户创建广告账户
    # boss的账户
    accounts_boss = [
        ("Google Ads - 主账户", "Google Ads", 18000, 500),
        ("Meta Ads - 主账户", "Meta Ads", 12000, 400),
        ("TikTok Ads - 主账户", "TikTok Ads", 8000, 300),
        ("Google Ads - 备用", "Google Ads", 5000, 200),
    ]
    for name, plat, bal, bud in accounts_boss:
        c.execute("INSERT INTO ad_accounts (account_name, platform, balance, daily_budget, user_id) "
                  "VALUES (?, ?, ?, ?, 1)", (name, plat, bal, bud))

    # admin的账户
    accounts_admin = [
        ("Google Ads - 部门A", "Google Ads", 10000, 300),
        ("Meta Ads - 部门B", "Meta Ads", 8000, 250),
    ]
    for name, plat, bal, bud in accounts_admin:
        c.execute("INSERT INTO ad_accounts (account_name, platform, balance, daily_budget, user_id) "
                  "VALUES (?, ?, ?, ?, 2)", (name, plat, bal, bud))

    # 导入模拟数据生成函数
    from backend.mock_ad_data.mock_ad_data import generate_mock_plans, generate_mock_alerts, generate_mock_daily_reports
    generate_mock_plans(c)
    generate_mock_alerts(c)
    generate_mock_daily_reports(c)

    conn.commit()
    conn.close()
