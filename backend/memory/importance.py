"""重要性评分 — 不调 LLM，纯规则，每条对话回合打分"""
import re

# 高价值关键词
_HIGH_VALUE_KW = [
    "ROAS", "CPC", "CPM", "CTR", "CVR", "CPA", "ROI",
    "花费", "营收", "销售额", "利润", "预算", "余额",
    "下降", "上升", "增长", "减少", "波动", "异常",
    "暂停", "开启", "调整", "增加", "减少",
    "建议", "优化", "方案", "策略",
    "原因", "为什么", "导致", "因为", "所以",
]

# 低价值关键词
_LOW_VALUE_KW = [
    "你好", "在吗", "谢谢", "好的", "嗯", "行",
    "ok", "知道", "明白",
]


def score_turn(user_msg, ai_reply):
    """对一轮对话打分，1-10，越高越重要"""
    text = (user_msg + " " + ai_reply).lower()
    score = 5  # 基础分

    # 高价值关键词命中
    hits = sum(1 for kw in _HIGH_VALUE_KW if kw.lower() in text)
    score += hits * 1.5
    if hits >= 3:
        score += 2  # 多个高价值词 → 决策类对话

    # 包含数字
    numbers = re.findall(r"\d+\.?\d*%?", text)
    if numbers:
        score += min(3, len(numbers))
        # 含百分比
        if any("%" in n for n in numbers):
            score += 1

    # 含金额符号
    if re.search(r"[¥￥$€]", text):
        score += 1

    # 用户消息太短 → 低价值
    if len(user_msg.strip()) <= 2:
        score -= 3
    elif len(user_msg.strip()) <= 4:
        score -= 1

    # AI 回复太短 → 低价值
    if len(ai_reply.strip()) < 15:
        score -= 2

    # 低价值关键词
    if user_msg.strip().lower() in _LOW_VALUE_KW:
        score -= 4

    # AI 问 "需要我帮你..." 说明在走流程，中等价值
    if "需要我帮" in ai_reply or "请问还有什么" in ai_reply:
        score = max(score, 4)

    return max(1, min(10, round(score)))


def importance_label(score):
    """将分数转为可读标签"""
    if score >= 9:
        return "CRITICAL"
    elif score >= 7:
        return "HIGH"
    elif score >= 5:
        return "MEDIUM"
    elif score >= 3:
        return "LOW"
    return "TRIVIAL"


def build_important_window(messages, max_turns=6):
    """按重要性保留最高分的 N 轮对话，而非最近 N 轮"""
    # 收集所有 user-assistant 对
    pairs = []
    i = 0
    while i < len(messages):
        if messages[i].get("role") == "user":
            user_content = messages[i].get("content", "")
            ai_content = ""
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                ai_content = messages[i + 1].get("content", "")
            s = score_turn(user_content, ai_content)
            pairs.append((s, messages[i], messages[i + 1] if ai_content else None))
            i += 2 if ai_content else 1
        else:
            i += 1

    if len(pairs) <= max_turns:
        window = []
        for _, u, a in pairs:
            window.append(u)
            if a:
                window.append(a)
        return window

    # 按重要性排序，取最高分的 max_turns 轮
    pairs.sort(key=lambda x: -x[0])
    selected = pairs[:max_turns]
    selected.sort(key=lambda x: x[1].get("created_at", 0) if "created_at" in x[1] else 0)

    window = []
    for _, u, a in selected:
        window.append(u)
        if a:
            window.append(a)
    return window