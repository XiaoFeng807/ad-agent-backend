"""四层记忆系统 v4: 滑动窗口 + 短期/长期/业务记忆"""
import os, json
from datetime import datetime, timedelta
from backend.memory.importance import build_important_window

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "user_memory")

# ── 分层配置 ──
LAYER_CONFIG = {
    "short_term": {"max_items": 20, "ttl_minutes": 30},
    "long_term_profile": {"max_items": 30, "ttl_days": 30},
    "business_memory": {"max_items": 50, "ttl_days": 90},
}


def _get_path(user_id):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return os.path.join(MEMORY_DIR, f"user_{user_id}.json")


def _now():
    return datetime.now()


def _now_str():
    return _now().strftime("%m-%d %H:%M")


# ==================== 持久化 ====================



def _apply_decay(memory):
    """时间衰减：删除超过保留期限的旧记录"""
    now = _now()
    cutoff_short = now - timedelta(minutes=LAYER_CONFIG["short_term"]["ttl_minutes"])
    cutoff_profile = now - timedelta(days=LAYER_CONFIG["long_term_profile"]["ttl_days"])
    cutoff_biz = now - timedelta(days=LAYER_CONFIG["business_memory"]["ttl_days"])

    st = memory.get("short_term", [])
    kept = []
    for item in st:
        try:
            ts_str = item[1:17]
            item_dt = datetime(now.year, int(ts_str[:2]), int(ts_str[3:5]), int(ts_str[6:8]), int(ts_str[9:11]))
            if item_dt >= cutoff_short: kept.append(item)
        except: kept.append(item)
    memory['short_term'] = kept

    profile = memory.get('long_term_profile', {})
    prefs = profile.get('preferences', [])
    kept = []
    for p in prefs:
        try:
            dt = datetime.strptime(p["first_seen"][:5] + " 00:00", "%m-%d %H:%M").replace(year=now.year)
            if dt >= cutoff_profile: kept.append(p)
        except: kept.append(p)
    profile['preferences'] = kept

    biz = memory.get('business_memory', {})
    for key in ['recent_decisions', 'alert_history', 'key_metrics']:
        items = biz.get(key, [])
        kept = []
        for item in items:
            try:
                dt = datetime.strptime(item['timestamp'][:5] + ' ' + item['timestamp'][6:11], '%m-%d %H:%M').replace(year=now.year)
                if dt >= cutoff_biz: kept.append(item)
            except: kept.append(item)
        biz[key] = kept
    return memory


def _load(user_id):
    path = _get_path(user_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            memory = json.load(f)
            _apply_decay(memory)
            return memory
    return {
        "user_id": user_id,
        "short_term": [],
        "long_term_profile": {
            "frequent_actions": [],
            "preferences": [],
            "first_seen": _now_str(),
            "total_interactions": 0,
        },
        "business_memory": {
            "accounts": {},
            "recent_decisions": [],
            "alert_history": [],
            "key_metrics": [],
        },
    }


def _save(user_id, memory):
    path = _get_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)


# ==================== L1: 滑动窗口 — 在 orchestrator 内存中维护 ====================

def build_sliding_window(messages, max_turns=6):
    """从对话历史中截取最近 N 轮完整消息"""
    window = []
    for m in reversed(messages):
        role = m.get("role", "")
        if role in ("user", "assistant"):
            window.insert(0, m)
            # 统计 user + assistant 的对数
            user_count = sum(1 for w in window if w["role"] == "user")
            if user_count >= max_turns:
                break
    return window


# ==================== L2: 短期记忆 ====================

def add_short_term(user_id, user_msg, ai_reply):
    """存储一次对话摘要到短期记忆"""
    memory = _load(user_id)
    ts = _now_str()
    summary = f"[{ts}] {user_msg[:30]} -> {ai_reply[:60]}"
    memory["short_term"].append(summary)
    # 裁剪
    max_items = LAYER_CONFIG["short_term"]["max_items"]
    if len(memory["short_term"]) > max_items:
        memory["short_term"] = memory["short_term"][-max_items:]
    # TTL 清理：删除超过 30 分钟的
    cutoff = _now() - timedelta(minutes=LAYER_CONFIG["short_term"]["ttl_minutes"])
    kept = []
    for item in memory["short_term"]:
        try:
            ts_str = item[1:17]  # [MM-DD HH:MM]
            item_dt = datetime(_now().year, int(ts_str[:2]), int(ts_str[3:5]),
                               int(ts_str[6:8]), int(ts_str[9:11]))
            if item_dt >= cutoff:
                kept.append(item)
        except:
            kept.append(item)
    memory["short_term"] = kept
    _save(user_id, memory)
    return summary


def get_short_term(user_id):
    """获取短期记忆文本"""
    memory = _load(user_id)
    items = memory.get("short_term", [])
    if not items:
        return ""
    return "【近期对话】\n" + "\n".join(items[-8:])


# ==================== L3: 长期画像 ====================

def update_profile(user_id, action_name, action_args=None):
    """更新用户长期画像"""
    memory = _load(user_id)
    profile = memory["long_term_profile"]
    profile["total_interactions"] = profile.get("total_interactions", 0) + 1

    # 记录高频操作
    actions = profile.setdefault("frequent_actions", [])
    found = False
    for a in actions:
        if a["action"] == action_name:
            a["count"] += 1
            a["last_seen"] = _now_str()
            found = True
            break
    if not found:
        actions.append({"action": action_name, "count": 1, "last_seen": _now_str()})
    # 保留 top 10
    actions.sort(key=lambda x: -x["count"])
    profile["frequent_actions"] = actions[:10]

    # 从参数学习偏好
    if action_args:
        if "keyword" in action_args:
            prefs = profile.setdefault("preferences", [])
            kw = action_args["keyword"]
            # 不重复记录相同关键词
            if not any(p.get("keyword") == kw for p in prefs):
                prefs.append({"keyword": kw, "count": 1, "first_seen": _now_str()})
            else:
                for p in prefs:
                    if p.get("keyword") == kw:
                        p["count"] = p.get("count", 0) + 1
        if action_name == "get_dashboard_data":
            prefs = profile.setdefault("preferences", [])
            if not any(p.get("type") == "dashboard" for p in prefs):
                prefs.append({"type": "dashboard", "label": "关注整体投放效果", "first_seen": _now_str()})
        elif action_name == "get_alerts":
            prefs = profile.setdefault("preferences", [])
            if not any(p.get("type") == "alerts" for p in prefs):
                prefs.append({"type": "alerts", "label": "关注告警与异常", "first_seen": _now_str()})

    _save(user_id, memory)
    return memory


def get_profile_text(user_id):
    """获取长期画像文本"""
    memory = _load(user_id)
    profile = memory.get("long_term_profile", {})
    parts = []
    actions = profile.get("frequent_actions", [])
    if actions:
        top = [f"{a['action']}({a['count']}次)" for a in actions[:3]]
        parts.append("【行为习惯】" + " ".join(top))
    prefs = profile.get("preferences", [])
    if prefs:
        labels = [p.get("label") or p.get("keyword", "") for p in prefs[:3]]
        parts.append("【关注偏好】" + "; ".join(labels))
    total = profile.get("total_interactions", 0)
    if total:
        parts.append(f"【使用频率】共交互{total}次")
    return "\n".join(parts)


# ==================== L4: 业务记忆 ====================

def update_business_memory(user_id, category, data):
    """更新业务记忆"""
    memory = _load(user_id)
    biz = memory["business_memory"]
    ts = _now_str()

    if category == "account" and isinstance(data, dict):
        name = data.get("account_name", "")
        if name:
            biz["accounts"][name] = {**data, "updated_at": ts}

    elif category == "decision" and isinstance(data, dict):
        biz.setdefault("recent_decisions", []).append({
            **data, "timestamp": ts
        })
        if len(biz["recent_decisions"]) > 10:
            biz["recent_decisions"] = biz["recent_decisions"][-10:]

    elif category == "alert" and isinstance(data, dict):
        biz.setdefault("alert_history", []).append({
            **data, "timestamp": ts
        })
        if len(biz["alert_history"]) > 10:
            biz["alert_history"] = biz["alert_history"][-10:]

    elif category == "metrics" and isinstance(data, dict):
        biz.setdefault("key_metrics", []).append({
            **data, "timestamp": ts
        })
        if len(biz["key_metrics"]) > 20:
            biz["key_metrics"] = biz["key_metrics"][-20:]

    _save(user_id, memory)


def get_business_text(user_id):
    """获取业务记忆文本"""
    memory = _load(user_id)
    biz = memory.get("business_memory", {})
    parts = []

    accounts = biz.get("accounts", {})
    if accounts:
        acct_str = []
        for name, info in accounts.items():
            bal = info.get("balance", "?")
            status = info.get("status", "?")
            acct_str.append(f"{name}(余额{bal}/{status})")
        if acct_str:
            parts.append("【账户概况】" + "; ".join(acct_str[:5]))

    decisions = biz.get("recent_decisions", [])
    if decisions:
        recent = decisions[-3:]
        d_str = [f"{d.get('category','?')}:{d.get('suggestion','')[:20]}" for d in recent]
        parts.append("【近期决策】" + " | ".join(d_str))

    alerts = biz.get("alert_history", [])
    if alerts:
        unread = [a for a in alerts if not a.get("is_read")]
        if unread:
            parts.append(f"【未读告警】{len(unread)}条")

    metrics = biz.get("key_metrics", [])
    if metrics:
        last = metrics[-1]
        parts.append(f"【最新指标】ROAS={last.get('roas','?')} 花费={last.get('cost','?')}")

    return "\n".join(parts)


# ==================== 整合：给 orchestrator 调用 ====================

def compose_context(user_id, messages=None):
    """组合所有层级的记忆上下文"""
    layers = []

    # L1: 滑动窗口
    if messages:
        window = build_important_window(messages, max_turns=6)
        parts = []
        for m in window:
            role = m["role"]
            content = m.get("content", "")
            if role == "user":
                parts.append("用户: " + content[:200])
            else:
                parts.append("AI: " + content[:200])
        if parts:
            layers.append("【最近对话】\n" + "\n".join(parts))

    # L2: 短期
    st = get_short_term(user_id)
    if st:
        layers.append(st)

    # L3: 长期画像
    prof = get_profile_text(user_id)
    if prof:
        layers.append(prof)

    # L4: 业务记忆
    biz = get_business_text(user_id)
    if biz:
        layers.append(biz)

    return "\n\n".join(layers)


# ==================== 兼容旧接口 ====================

def get_compact_memory(user_id):
    """保持旧接口兼容"""
    return compose_context(user_id)


def store_conversation_summary(user_id, user_msg, ai_reply):
    """保持旧接口兼容"""
    return add_short_term(user_id, user_msg, ai_reply)


def get_conversation_summaries(user_id):
    """保持旧接口兼容"""
    return get_short_term(user_id)
# ===== 兼容旧接口 =====
add_fact = update_profile

