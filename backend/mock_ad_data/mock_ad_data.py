"""模拟数据生成模块：生成示例广告数据用于演示"""
import random, math
from datetime import datetime, timedelta


def generate_mock_plans(c):
    """生成模拟广告计划数据"""
    import random

    # 为每个用户创建不同的广告计划
    plans_data = {
        1: [  # boss的广告计划
            ("Google搜索-品牌词", "Google Ads", 300, "active", 15000, 420, 3200, 8500),
            ("Google搜索-通用词", "Google Ads", 500, "active", 28000, 680, 5600, 12000),
            ("Meta-再营销", "Meta Ads", 400, "active", 35000, 890, 4800, 15000),
            ("Meta-新客拓展", "Meta Ads", 350, "paused", 12000, 310, 2100, 3000),
            ("TikTok-爆款视频", "TikTok Ads", 300, "active", 68000, 1500, 3900, 9600),
            ("TikTok-达人合作", "TikTok Ads", 250, "active", 42000, 1100, 2800, 7200),
        ],
        2: [  # admin的广告计划
            ("部门A-搜索广告", "Google Ads", 300, "active", 12000, 380, 2800, 6500),
            ("部门B-社媒广告", "Meta Ads", 250, "active", 22000, 560, 3200, 7800),
            ("部门A-展示广告", "Google Ads", 200, "paused", 8000, 200, 1500, 2000),
        ],
        3: [  # zhangsan的广告计划
            ("测试计划A", "Google Ads", 100, "active", 5000, 150, 800, 2000),
            ("测试计划B", "Meta Ads", 100, "active", 8000, 200, 900, 1800),
        ],
        4: [  # lisi的广告计划
            ("个人推广-搜索", "Google Ads", 80, "active", 3000, 100, 500, 1200),
        ],
    }

    for user_id, plans in plans_data.items():
        for name, plat, budget, status, imp, clicks, cost, sales in plans:
            c.execute(
                "INSERT OR IGNORE INTO ad_plans (plan_name, platform, daily_budget, status, "
                "impressions, clicks, cost, sales, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, plat, budget, status, imp, clicks, cost, sales, user_id)
            )


def generate_mock_alerts(c):
    """生成模拟告警数据"""
    import random
    alerts_templates = [
        ("roas_drop", "ROAS下降：Google搜索-通用词 ROAS从2.5降至1.8", "danger"),
        ("budget_exceed", "预算超支：TikTok-爆款视频 日消耗已达预算的85%", "warning"),
        ("impression_drop", "曝光下降：Meta-再营销 曝光量较昨日下降30%", "warning"),
        ("account_balance", "账户余额不足：Google Ads-备用 余额仅剩500元", "danger"),
        ("plan_paused", "计划暂停：Meta-新客拓展 已被系统自动暂停", "info"),
    ]

    for user_id in range(1, 5):
        for t, msg, level in alerts_templates:
            c.execute(
                "INSERT OR IGNORE INTO alerts (type, message, level, user_id, is_read) VALUES (?,?,?,?,?)",
                (t, msg, level, user_id, random.choice([0, 0, 0, 1]))  # 部分已读
            )


def generate_mock_daily_reports(c):
    """生成最近30天的每日数据报告"""
    import random
    from datetime import datetime, timedelta

    for user_id in range(1, 5):
        base_cost = random.randint(500, 2000)
        base_imp = random.randint(10000, 50000)
        base_clicks = random.randint(200, 1000)
        base_sales = random.randint(1000, 5000)

        for day_offset in range(30):
            date = (datetime.now() - timedelta(days=29 - day_offset)).strftime("%Y-%m-%d")
            # 加入随机波动，模拟真实数据
            cost = base_cost + random.randint(-200, 200)
            imp = base_imp + random.randint(-2000, 2000)
            clicks = base_clicks + random.randint(-50, 50)
            sales = base_sales + random.randint(-500, 500)

            c.execute(
                "INSERT OR IGNORE INTO daily_reports (report_date, cost, impressions, clicks, sales, user_id) "
                "VALUES (?,?,?,?,?,?)",
                (date, max(0, cost), max(0, imp), max(0, clicks), max(0, sales), user_id)
            )
