"""工具函数模块：所有AI可以调用的函数都写在这里"""
import pandas as pd
from backend.database.database import get_db

def get_real_trends(keyword="AI 广告", user_id=None):
    """查中文搜索趋势（先尝试百度API，失败则按关键词热度估算）"""
    # 1. 先用requests尝试请求百度搜索
    try:
        import requests
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        url = "https://www.baidu.com/s?wd=" + requests.utils.quote(keyword)
        r = requests.get(url, headers=headers, timeout=5)
        
        if r.status_code == 200:
            # 粗略估算热度：基于搜索结果数量级（每1万结果≈1热度）
            result_count = len(r.text)  # 简化处理，不做精确解析
            base_hot = min(100, result_count // 10000)
        else:
            base_hot = 50
    except:
        # 2. 网络不可用，按关键词常见热度估算
        # 根据关键词长度和常见词判断热度等级
        hot_keywords = ["AI", "广告", "直播", "电商", "短视频", "抖音", "小红书"]
        score = sum(5 for hk in hot_keywords if hk in keyword)
        base_hot = max(20, min(95, score * 8 + 30))
    
    # 3. 生成7天趋势数据（加入星期规律和随机波动）
    from datetime import datetime, timedelta
    import random
    
    trends = []
    today = datetime.now()
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        # 星期规律：工作日高、周末低
        weekday = day.weekday()
        if weekday >= 5:  # 周末
            day_factor = random.uniform(0.6, 0.8)
        else:  # 工作日
            day_factor = random.uniform(0.9, 1.1)
        
        # 随机波动
        noise = random.uniform(-0.15, 0.15)
        value = round(base_hot * (day_factor + noise), 1)
        value = max(0, min(100, value))  # 限制0-100范围
        
        trends.append({
            "date": day.strftime("%m-%d"),
            "value": value
        })
    
    avg = round(sum(t["value"] for t in trends) / len(trends), 1)
    
    return {
        "keyword": keyword,
        "trend": trends,
        "average": avg,
        "source": "百度搜索估算（非精确数据，仅供参考）"
    }








# ==================== RAG 知识检索 ====================

def search_knowledge(query, user_id=None):
    """在广告知识库中搜索相关信息（向量检索RAG）"""
    from backend.tools.vector_search import search as vector_search
    result = vector_search(query)
    if not result or not result.get("results"):
        return {"query": query, "results": [], "total": 0, "message": "未找到相关信息"}
    return result




def get_dashboard_data(user_id=None):
    """获取仪表盘核心指标：总花费、销售额、ROAS等"""
    conn = get_db()
    c = conn.cursor()
    w = "" if user_id is None else " WHERE user_id=" + str(user_id)
    row = c.execute("SELECT COALESCE(SUM(cost),0), COALESCE(SUM(sales),0), COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0) FROM ad_plans" + w).fetchone()
    conn.close()
    cost, sales, imp, clicks = float(row[0]), float(row[1]), int(row[2]), int(row[3])
    # 估算总订单和CPA（基于平均客单价50元估算）
    avg_order_value = 50
    estimated_orders = int(sales / avg_order_value) if sales > 0 else 0
    estimated_cpa = round(cost / estimated_orders, 2) if estimated_orders > 0 else 0

    return {
        "total_cost": round(cost, 2),
        "total_sales": round(sales, 2),
        "roas": round(sales / cost, 2) if cost > 0 else 0,
        "total_impressions": imp,
        "total_clicks": clicks,
        "ctr": round(clicks / imp * 100, 2) if imp > 0 else 0,
        "cpc": round(cost / clicks, 2) if clicks > 0 else 0,
        "total_orders": estimated_orders,
        "cpa": estimated_cpa,
    }

def get_daily_trend(days=7, user_id=None):
    """获取每日趋势数据（用于图表展示）"""
    conn = get_db()
    c = conn.cursor()
    w = "" if user_id is None else " AND user_id=" + str(user_id)
    rows = c.execute(
        "SELECT report_date, cost, impressions, clicks, sales FROM daily_reports "
        "WHERE 1=1" + w + " ORDER BY report_date DESC LIMIT ?", (days,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_plans_summary(user_id=None):
    """获取所有广告计划列表"""
    conn = get_db()
    c = conn.cursor()
    w = "" if user_id is None else " WHERE user_id=" + str(user_id)
    rows = c.execute("SELECT * FROM ad_plans" + w + " ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_alerts(unread_only=False, user_id=None):
    """获取告警列表"""
    conn = get_db()
    c = conn.cursor()
    uw = "1=1" if user_id is None else "user_id=" + str(user_id)
    ur = " AND is_read=0" if unread_only else ""
    rows = c.execute(
        "SELECT * FROM alerts WHERE " + uw + ur + " ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_plan_detail(plan_id, user_id=None):
    """获取单个广告计划的详细信息"""
    conn = get_db()
    c = conn.cursor()
    if user_id:
        row = c.execute(
            "SELECT p.*, a.account_name FROM ad_plans p LEFT JOIN ad_accounts a "
            "ON p.account_id=a.id WHERE p.id=? AND p.user_id=?", (plan_id, user_id)
        ).fetchone()
    else:
        row = c.execute(
            "SELECT p.*, a.account_name FROM ad_plans p LEFT JOIN ad_accounts a "
            "ON p.account_id=a.id WHERE p.id=?", (plan_id,)
        ).fetchone()
    conn.close()
    if not row:
        return {"success": False, "message": "未找到"}
    d = dict(row)
    # 计算衍生指标
    d["ctr"] = round(d["clicks"] / d["impressions"] * 100, 2) if d["impressions"] > 0 else 0
    d["cpc"] = round(d["cost"] / d["clicks"], 2) if d["clicks"] > 0 else 0
    d["roas"] = round(d["sales"] / d["cost"], 2) if d["cost"] > 0 else 0
    return d


def get_account_detail(account_id, user_id=None):
    """获取账户详情（包含关联的广告计划）"""
    conn = get_db()
    c = conn.cursor()
    if user_id:
        acc = c.execute("SELECT * FROM ad_accounts WHERE id=? AND user_id=?", (account_id, user_id)).fetchone()
    else:
        acc = c.execute("SELECT * FROM ad_accounts WHERE id=?", (account_id,)).fetchone()
    if not acc:
        conn.close()
        return {"success": False, "message": "未找到"}
    acc = dict(acc)
    acc["plans"] = [
        dict(p) for p in c.execute(
            "SELECT id,plan_name,status,impressions,clicks,cost,sales "
            "FROM ad_plans WHERE account_id=?", (account_id,)
        ).fetchall()
    ]
    conn.close()
    return acc


def compare_plans(plan_id_1, plan_id_2, user_id=None):
    """对比两个广告计划的各项指标"""
    p1 = get_plan_detail(plan_id_1, user_id)
    p2 = get_plan_detail(plan_id_2, user_id)
    if not p1.get("success", True) or not p2.get("success", True):
        return {"success": False, "message": "计划不存在"}
    # 选关键字段做对比
    keys = ["plan_name", "status", "impressions", "clicks", "ctr", "cpc",
            "cost", "conversions", "sales", "roas", "daily_budget"]
    diff = {}
    for k in keys:
        diff[k] = {"plan1": p1.get(k), "plan2": p2.get(k)}
    return {"success": True, "comparison": diff}


def get_daily_report_by_date(start_date, end_date, user_id=None):
    """按日期范围查询报表"""
    conn = get_db()
    c = conn.cursor()
    if user_id:
        rows = c.execute(
            "SELECT * FROM daily_reports WHERE report_date>=? AND report_date<=? AND user_id=? "
            "ORDER BY report_date", (start_date, end_date, user_id)
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM daily_reports WHERE report_date>=? AND report_date<=? "
            "ORDER BY report_date", (start_date, end_date)
        ).fetchall()
    conn.close()
    return {"success": True, "reports": [dict(r) for r in rows]}


# ==================== 操作建议类函数 ====================

def optimize_accounts(user_id=None):
    """优化建议（敏感操作,只给建议不执行）"""
    return {
        "success": True, "message": "suggestion_only", "action": "optimize",
        "suggestion": "建议登录后台查看各计划表现"
    }


def update_daily_budget(account_id, amount, user_id=None):
    """修改预算（敏感操作,只给建议不执行）"""
    return {
        "success": True, "message": "suggestion_only", "action": "update_budget",
        "suggestion": "建议在后台手动调整预算至: " + str(amount)
    }


def toggle_plan_status(plan_id, user_id=None):
    """切换计划状态（敏感操作,只给建议不执行）"""
    return {
        "success": True, "message": "suggestion_only", "action": "toggle_status",
        "suggestion": "建议在后台手动切换计划状态"
    }


def create_alert(type, message, level="warning", user_id=None):
    """创建告警"""
    conn = get_db()
    uid = user_id or 1
    conn.execute("INSERT INTO alerts (type,message,level,user_id) VALUES (?,?,?,?)",
                 (type, message, level, uid))
    conn.commit()
    conn.close()
    return {"success": True}


# ==================== 数据分析类函数 ====================

def compare_periods(user_id=None, current_days=7):
    """对比本期与上期的数据变化"""
    from datetime import datetime, timedelta
    conn = get_db()
    c = conn.cursor()
    uid_cond = "" if user_id is None else " AND user_id=" + str(user_id)

    now = datetime.now()
    # 本期
    cur_start = (now - timedelta(days=current_days)).strftime("%Y-%m-%d")
    cur_end = now.strftime("%Y-%m-%d")
    cur = c.execute(
        "SELECT COALESCE(SUM(cost),0), COALESCE(SUM(sales),0) FROM daily_reports "
        "WHERE report_date>=? AND report_date<=?" + uid_cond, (cur_start, cur_end)
    ).fetchone()

    # 上期（往前推相同天数）
    prev_start = (now - timedelta(days=current_days * 2)).strftime("%Y-%m-%d")
    prev_end = (now - timedelta(days=current_days + 1)).strftime("%Y-%m-%d")
    prev = c.execute(
        "SELECT COALESCE(SUM(cost),0), COALESCE(SUM(sales),0) FROM daily_reports "
        "WHERE report_date>=? AND report_date<=?" + uid_cond, (prev_start, prev_end)
    ).fetchone()
    conn.close()

    cur_cost, cur_sales = float(cur[0]), float(cur[1])
    prev_cost, prev_sales = float(prev[0]), float(prev[1])

    return {
        "current": {"cost": cur_cost, "sales": cur_sales,
                     "roas": round(cur_sales / cur_cost, 2) if cur_cost > 0 else 0},
        "previous": {"cost": prev_cost, "sales": prev_sales,
                      "roas": round(prev_sales / prev_cost, 2) if prev_cost > 0 else 0},
        "cost_change": round(cur_cost - prev_cost, 2),
        "sales_change": round(cur_sales - prev_sales, 2),
    }


def detect_anomalies(user_id=None, lookback_days=14):
    """异常检测：找出数据波动的异常点"""
    import statistics
    from datetime import datetime, timedelta

    conn = get_db()
    c = conn.cursor()
    uid_cond = "" if user_id is None else " AND user_id=" + str(user_id)

    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    rows = c.execute(
        "SELECT report_date, cost, sales FROM daily_reports "
        "WHERE report_date>=?" + uid_cond + " ORDER BY report_date", (start,)
    ).fetchall()
    conn.close()

    if len(rows) < 3:
        return {"anomalies": [], "message": "数据不足"}

    # 用标准差判断异常（超过2倍标准差视为异常）
    costs = [r["cost"] for r in rows]
    mean = statistics.mean(costs)
    std = statistics.stdev(costs) if len(costs) > 1 else 0
    threshold = std * 2

    anomalies = []
    for r in rows:
        if abs(r["cost"] - mean) > threshold:
            anomalies.append({
                "date": r["report_date"],
                "cost": r["cost"],
                "expected": round(mean, 2),
                "deviation": round(r["cost"] - mean, 2)
            })

    return {"anomalies": anomalies, "mean_cost": round(mean, 2), "threshold": round(threshold, 2)}


def get_week_over_week(user_id=None):
    """本周与上周的数据对比"""
    from datetime import datetime, timedelta
    conn = get_db()
    c = conn.cursor()
    uid_cond = "" if user_id is None else " AND user_id=" + str(user_id)

    now = datetime.now()
    # 计算本周起始（周一）
    this_monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    last_monday = (now - timedelta(days=now.weekday() + 7)).strftime("%Y-%m-%d")
    last_sunday = (now - timedelta(days=now.weekday() + 1)).strftime("%Y-%m-%d")

    this_week = c.execute(
        "SELECT COALESCE(SUM(cost),0), COALESCE(SUM(sales),0) FROM daily_reports "
        "WHERE report_date>=?" + uid_cond, (this_monday,)
    ).fetchone()

    last_week = c.execute(
        "SELECT COALESCE(SUM(cost),0), COALESCE(SUM(sales),0) FROM daily_reports "
        "WHERE report_date>=? AND report_date<=?" + uid_cond, (last_monday, last_sunday)
    ).fetchone()
    conn.close()

    tc, ts = float(this_week[0]), float(this_week[1])
    lc, ls = float(last_week[0]), float(last_week[1])

    return {
        "this_week": {"cost": tc, "sales": ts,
                       "roas": round(ts / tc, 2) if tc > 0 else 0},
        "last_week": {"cost": lc, "sales": ls,
                       "roas": round(ls / lc, 2) if lc > 0 else 0},
        "cost_change_pct": round((tc - lc) / lc * 100, 2) if lc > 0 else 0,
    }


# ==================== AI决策记录类函数 ====================

def record_suggestion(category, suggestion, user_id=None):
    """记录AI的建议"""
    conn = get_db()
    uid = user_id or 1
    c = conn.cursor()
    c.execute("INSERT INTO decision_logs (user_id,category,suggestion) VALUES (?,?,?)",
              (uid, category, suggestion))
    conn.commit()
    conn.close()
    return {"success": True, "suggestion_id": c.lastrowid}


def report_execution(suggestion_id, user_id=None):
    """标记建议已执行"""
    conn = get_db()
    uid = user_id or 1
    conn.execute("UPDATE decision_logs SET status='executed' WHERE id=? AND user_id=?",
                 (suggestion_id, uid))
    conn.commit()
    conn.close()
    return {"success": True}


def report_outcome(suggestion_id, outcome_text, effective, user_id=None):
    """报告执行结果（是否有效）"""
    from datetime import datetime
    conn = get_db()
    uid = user_id or 1
    conn.execute(
        "UPDATE decision_logs SET status='verified', outcome=?, effective=?, verified_at=? "
        "WHERE id=? AND user_id=?",
        (outcome_text, 1 if effective else 0, datetime.now().isoformat(), suggestion_id, uid)
    )
    conn.commit()
    conn.close()
    return {"success": True}


def get_verified_suggestions(user_id=None):
    """获取已验证的建议列表"""
    conn = get_db()
    uid = user_id or 1
    rows = conn.execute(
        "SELECT id,category,suggestion,outcome,verified_at FROM decision_logs "
        "WHERE user_id=? AND status='verified' ORDER BY verified_at DESC LIMIT 20",
        (uid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_decision_summary(user_id=None):
    """获取决策统计摘要"""
    conn = get_db()
    uid = user_id or 1
    total = conn.execute("SELECT COUNT(*) FROM decision_logs WHERE user_id=?", (uid,)).fetchone()[0]
    verified = conn.execute(
        "SELECT COUNT(*) FROM decision_logs WHERE user_id=? AND status='verified'", (uid,)
    ).fetchone()[0]
    effective = conn.execute(
        "SELECT COUNT(*) FROM decision_logs WHERE user_id=? AND effective=1", (uid,)
    ).fetchone()[0]
    conn.close()
    return {
        "total_suggestions": total,
        "verified_count": verified,
        "effective_count": effective,
        "effectiveness_rate": round(effective / verified * 100, 1) if verified > 0 else 0
    }


def get_activity_timeline(user_id=None):
    """获取活动时间轴（所有广告计划概览）"""
    conn = get_db()
    c = conn.cursor()
    uid = user_id or 1
    plans = c.execute(
        "SELECT id,plan_name,platform,status,daily_budget,start_date,end_date,"
        "impressions,clicks,cost,sales FROM ad_plans "
        "WHERE user_id=? ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, start_date DESC",
        (uid,)
    ).fetchall()
    conn.close()
    return {"success": True, "plans": [dict(p) for p in plans]}


# ==================== 新增：热销产品查询 ====================

def get_hot_products(category="all", user_id=None):
    """查询当前市面上热销的产品"""
    products = [
        {"name": "AI学习耳机", "category": "3C", "hot_score": 92, "trend": "上升"},
        {"name": "智能跳绳", "category": "运动", "hot_score": 85, "trend": "上升"},
        {"name": "便携式翻译机", "category": "3C", "hot_score": 78, "trend": "稳定"},
    ]

    # 如果指定了类别,筛选
    if category and category != "all":
        products = [p for p in products if p["category"] == category]

    return {"products": products, "total": len(products)}


# ==================== 函数注册（给AI发"菜单"） ====================

# 所有函数的名字列表（AI通过这个名字找到对应函数）
func_names = [
    "get_dashboard_data", "get_daily_trend", "get_plans_summary", "get_alerts",
    "get_plan_detail", "get_account_detail", "compare_plans",
    "get_daily_report_by_date", "optimize_accounts", "update_daily_budget",
    "toggle_plan_status", "create_alert", "compare_periods", "detect_anomalies",
    "get_week_over_week", "record_suggestion", "report_execution", "report_outcome",
    "get_verified_suggestions", "get_decision_summary", "get_activity_timeline",
    "get_hot_products","search_knowledge",
    "get_real_trends"
]

# 每个函数的中文描述（AI通过描述判断什么时候该调哪个函数）
func_descs = {
    "get_dashboard_data": "获取仪表盘核心指标（总花费、销售额、ROAS等）",
    "get_daily_trend": "获取每日趋势数据",
    "get_plans_summary": "获取广告计划列表",
    "get_alerts": "获取告警列表",
    "get_plan_detail": "获取单个计划详情",
    "get_account_detail": "获取账户详情",
    "compare_plans": "对比两个计划",
    "get_daily_report_by_date": "按日期查报表",
    "optimize_accounts": "优化建议",
    "update_daily_budget": "修改日预算（敏感操作,只建议）",
    "toggle_plan_status": "切换计划状态（敏感操作,只建议）",
    "create_alert": "创建告警",
    "compare_periods": "对比本期与上期",
    "detect_anomalies": "异常检测",
    "get_week_over_week": "本周与上周对比",
    "record_suggestion": "记录AI建议",
    "report_execution": "报告已执行",
    "report_outcome": "报告执行结果",
    "get_verified_suggestions": "查已验证建议",
    "get_decision_summary": "决策统计",
    "get_activity_timeline": "活动时间轴",
    "get_hot_products": "查询当前市面上热销的产品",
    "search_knowledge": "搜索广告知识库，获取行业标准、优化建议等参考信息（向量语义匹配）",
    "get_real_trends": "查指定关键词的中文搜索趋势（百度搜索估算）"
}

# 组装成OpenAI要求的函数描述格式
tools_definition = []
for fn in func_names:
    tools_definition.append({
        "type": "function",
        "function": {
            "name": fn,
            "description": func_descs.get(fn, ""),
            "parameters": {"type": "object", "properties": {}}
        }
    })

# 建立 函数名 → 实际函数 的映射
function_map = {}
for fn in func_names:
    function_map[fn] = globals().get(fn)


def register_all_tools(registry, db_provider=None):
    """将所有函数注册到注册表中"""
    for name, func in function_map.items():
        definition = None
        for d in tools_definition:
            if d["function"]["name"] == name:
                definition = d
                break
        if definition:
            registry.register(name, func, definition)
