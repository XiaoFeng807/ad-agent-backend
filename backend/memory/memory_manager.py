# coding: utf-8
"""树状记忆系统 v2：按维度分层存储 + 自动学习偏好"""
import os, json
from datetime import datetime
from collections import Counter

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "user_memory")

def _get_path(user_id):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return os.path.join(MEMORY_DIR, f"user_{user_id}.json")

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
            "告警与问题": []
        },
        "stats": {}  # 新增：统计每种操作的次数
    }

def save_memory(user_id, memory):
    path = _get_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

def _learn_preferences(memory, tool_name, fact):
    """自动学习用户偏好：统计操作频率，推导关注点"""
    stats = memory.setdefault("stats", {})
    stats[tool_name] = stats.get(tool_name, 0) + 1

    tree = memory["tree"]
    prefs = tree.setdefault("关注偏好", [])

    # 当某个操作累计达到 3 次，推导一条偏好
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
        # 去重：同样的偏好不重复添加
        if pref not in prefs:
            prefs.append(pref)
        if len(prefs) > 5:
            tree["关注偏好"] = prefs[-5:]

def add_fact(user_id, tool_name, tool_args, tool_result):
    """把一条操作记录智能分流到树的不同分支"""
    memory = load_memory(user_id)
    tree = memory["tree"]
    now = datetime.now().strftime("%m-%d")

    # === 分流到不同分支 ===
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

    # 追加到最近操作
    tree.setdefault("最近操作", []).append(fact)
    if len(tree["最近操作"]) > 10:
        tree["最近操作"] = tree["最近操作"][-10:]

    # 容量控制
    if len(tree.get("告警与问题", [])) > 5:
        tree["告警与问题"] = tree["告警与问题"][-5:]
    if len(tree.get("关注偏好", [])) > 5:
        tree["关注偏好"] = tree["关注偏好"][-5:]

    # 自动学习偏好
    _learn_preferences(memory, tool_name, fact)

    save_memory(user_id, memory)
    return memory

def get_compact_memory(user_id):
    """格式化树状记忆，注入提示词"""
    memory = load_memory(user_id)
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

    return "\n".join(parts)
