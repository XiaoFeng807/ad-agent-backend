# coding: utf-8
"""树状记忆系统 v3：添加时间衰减遗忘机制"""
import os, json
from datetime import datetime, timedelta
from collections import Counter

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "user_memory")
# 默认保留天数
# 每积累 N 条新操作，自动生成一次阶段总结
SUMMARY_TRIGGER = 5

MAX_AGE_DAYS = {
    "最近操作": 3,
    "告警与问题": 7,
    "关注偏好": 14,
    "平台账户": 30,
    "阶段总结": 30
}


def _get_path(user_id):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return os.path.join(MEMORY_DIR, f"user_{user_id}.json")


def _parse_date(text):
    """从文本开头的 [MM-DD] 提取日期，返回 datetime 对象"""
    if text.startswith("[") and "]" in text:
        try:
            date_str = text[1:text.index("]")]
            month, day = date_str.split("-")
            now = datetime.now()
            return datetime(now.year, int(month), int(day))
        except:
            pass
    return None


def _apply_decay(memory):
    """时间衰减：删除超过保留天数的旧记录"""
    tree = memory["tree"]
    now = datetime.now()

    for branch, max_age in MAX_AGE_DAYS.items():
        items = tree.get(branch, {}) if isinstance(tree.get(branch), dict) else tree.get(branch, [])

        if isinstance(items, dict):
            # 平台账户是字典，保留
            continue

        kept = []
        for item in items:
            dt = _parse_date(item)
            if dt is None:
                kept.append(item)  # 没有日期的不删
            else:
                age = (now - dt).days
                if age <= max_age:
                    kept.append(item)

        tree[branch] = kept

    return memory


def _generate_summary(tree):
    """从树状记忆的最近操作中自动生成一段总结"""
    ops = tree.get("最近操作", [])
    if not ops:
        return ""

    # 提取关键操作类型
    dashboard_count = sum(1 for o in ops if "仪表盘" in o)
    alert_count = sum(1 for o in ops if "告警" in o)
    plan_count = sum(1 for o in ops if "计划" in o)
    trend_count = sum(1 for o in ops if "趋势" in o or "搜索" in o)
    
    items = []
    if dashboard_count > 0:
        items.append(f"查看了{dashboard_count}次仪表盘")
    if alert_count > 0:
        items.append(f"处理了{alert_count}条告警")
    if plan_count > 0:
        items.append(f"查看了{plan_count}次广告计划")
    if trend_count > 0:
        items.append(f"搜索了{trend_count}次趋势")
    
    if not items:
        return ""

    from datetime import datetime
    now = datetime.now().strftime("%m-%d")
    summary = f"[{now}] {', '.join(items)}"
    return summary


def _check_and_summarize(memory):
    """当新操作积累到阈值时，自动生成阶段总结"""
    tree = memory["tree"]
    ops = tree.get("最近操作", [])
    last_count = tree.get("_last_summary_count", 0)
    
    # 从上次总结到现在新增了多少条
    new_count = len(ops) - last_count
    if new_count >= 5 and len(ops) > 0:
        summary = _generate_summary(tree)
        if summary:
            tree.setdefault("阶段总结", []).append(summary)
            # 最多保留 5 条总结
            if len(tree["阶段总结"]) > 5:
                tree["阶段总结"] = tree["阶段总结"][-5:]
            # 更新计数
            tree["_last_summary_count"] = len(ops)


def load_memory(user_id):
    path = _get_path(user_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "user_id": user_id,
        "tree": {
            "平台账户": {},
            "最近操作": [],
            "关注偏好": [],
            "告警与问题": [],
            "阶段总结": [],
            "_last_summary_count": 0
        },
        "stats": {}
    }


def save_memory(user_id, memory):
    path = _get_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)


def _learn_preferences(memory, tool_name, fact):
    stats = memory.setdefault("stats", {})
    stats[tool_name] = stats.get(tool_name, 0) + 1

    tree = memory["tree"]
    prefs = tree.setdefault("关注偏好", [])

    count = stats[tool_name]
    pref = None

    if tool_name == "get_dashboard_data" and count == 3:
        pref = "用户经常查看仪表盘，关注整体投放效果（ROAS、花费）"
    elif tool_name == "get_daily_trend" and count == 3:
        pref = "用户关注每日趋势变化"
    elif tool_name == "get_account_detail" and count == 3:
        pref = "用户频繁查看账户详情，关注余额和预算"
    elif tool_name == "get_plans_summary" and count == 3:
        pref = "用户关心各广告计划的表现对比"
    elif tool_name == "get_real_trends" and count == 3:
        pref = "用户关注市场搜索趋势"
    elif tool_name == "get_alerts" and count == 3:
        pref = "用户重视告警信息，及时处理问题"
    elif tool_name == "get_hot_products" and count == 3:
        pref = "用户关注热销产品，有选品需求"

    if pref:
        if pref not in prefs:
            prefs.append(pref)
        if len(prefs) > 5:
            tree["关注偏好"] = prefs[-5:]


def add_fact(user_id, tool_name, tool_args, tool_result):
    memory = load_memory(user_id)
    tree = memory["tree"]
    now = datetime.now().strftime("%m-%d")

    if tool_name == "get_dashboard_data" and tool_result:
        cost = tool_result.get("total_cost", "?")
        roas = tool_result.get("roas", "?")
        sales = tool_result.get("total_sales", "?")
        fact = f"[{now}] 仪表盘：花费{cost}，ROAS {roas}，销售额{sales}"

    elif tool_name == "get_account_detail" and tool_result:
        name = tool_result.get("account_name", "未知")
        bal = tool_result.get("balance", "?")
        tree["平台账户"][name] = f"余额{bal}"
        fact = f"[{now}] 查看了{name}"

    elif tool_name == "get_plans_summary" and tool_result:
        count = len(tool_result)
        names = [p.get("plan_name","") for p in tool_result[:3]]
        fact = f"[{now}] 查看了{count}个计划：{', '.join(names)}"
        platforms = set(p.get("platform","") for p in tool_result if p.get("platform"))
        for p in platforms:
            tree["平台账户"][p] = tree["平台账户"].get(p, "")

    elif tool_name == "get_real_trends" and tool_result:
        kw = tool_args.get("keyword", "未知")
        avg = tool_result.get("average", "?")
        fact = f"[{now}] 搜索趋势：{kw}（热度{avg}）"

    elif tool_name == "get_alerts" and tool_result:
        count = len(tool_result)
        fact = f"[{now}] 告警：{count}条待处理"
        if tool_result:
            msgs = [a.get("message","") for a in tool_result[:2]]
            tree.setdefault("告警与问题", []).append(f"[{now}] {'; '.join(msgs)}")

    elif tool_name == "get_hot_products" and tool_result:
        count = len(tool_result)
        fact = f"[{now}] 查看了{count}个热销产品"

    elif "suggest" in tool_name or "optimize" in tool_name:
        fact = f"[{now}] 获取了优化建议"
    else:
        fact = f"[{now}] 执行了{tool_name}"

    tree.setdefault("最近操作", []).append(fact)
    if len(tree["最近操作"]) > 10:
        tree["最近操作"] = tree["最近操作"][-10:]

    if len(tree.get("告警与问题", [])) > 5:
        tree["告警与问题"] = tree["告警与问题"][-5:]
    if len(tree.get("关注偏好", [])) > 5:
        tree["关注偏好"] = tree["关注偏好"][-5:]

    _learn_preferences(memory, tool_name, fact)

    # 检查是否需要生成阶段总结
    _check_and_summarize(memory)

    # 每次保存前都运行时间衰减
    _apply_decay(memory)

    save_memory(user_id, memory)
    return memory


def get_compact_memory(user_id):
    memory = load_memory(user_id)
    # 读取时也运行衰减
    memory = _apply_decay(memory)
    tree = memory["tree"]
    parts = []

    if tree.get("平台账户"):
        accts = [f"{k}({v})" for k, v in tree["平台账户"].items()]
        parts.append("【平台账户】" + "; ".join(accts))

    if tree.get("最近操作"):
        recent = tree["最近操作"][-5:]
        parts.append("【最近】" + " | ".join(recent))

    if tree.get("告警与问题"):
        alerts = tree["告警与问题"][-3:]
        parts.append("【告警】" + " | ".join(alerts))

    if tree.get("关注偏好"):
        prefs = tree["关注偏好"]
        parts.append("【用户偏好】" + "; ".join(prefs))

    if tree.get("阶段总结"):
        summaries = tree["阶段总结"][-3:]
        parts.append("【阶段总结】" + " | ".join(summaries))

    return "\n".join(parts)
