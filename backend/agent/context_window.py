"""上下文窗口管理 v2 — 企业级
- 精准 token 计数（优先 tiktoken，回退 heuristic）
- 滑动窗口 + 摘要压缩
- 优先级保留
- 时间衰减（越近权重越高）
"""

import json
from datetime import datetime

# ==================== 精准 Token 计数 ====================
_tiktoken_enc = None

def _init_tokenizer():
    """尝试加载 tiktoken，失败则 None"""
    global _tiktoken_enc
    if _tiktoken_enc is not None:
        return True
    try:
        import tiktoken
        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        return True
    except (ImportError, Exception):
        return False

def estimate_tokens(text):
    """估算 token 数：优先 tiktoken，回退 heuristic"""
    if not text:
        return 0
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    elif not isinstance(text, str):
        text = str(text)

    if _init_tokenizer():
        return len(_tiktoken_enc.encode(text, disallowed_special=()))

    # Heuristic 回退：中文 ~1.5 字/token，英文 ~4 字/token
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4) + 1


# ==================== 窗口配置 ====================
# DeepSeek 上下文：64K，我们保守用 32K
MAX_TOKENS = 32000
SYSTEM_TOKENS = 3000       # 系统提示词 + 摘要 + 记忆
RESERVED_TOKENS = 3000     # 回复预留 + 工具调用返回
MAX_HISTORY_TOKENS = MAX_TOKENS - SYSTEM_TOKENS - RESERVED_TOKENS  # 26000

# 滑动窗口：最近 N 轮完整保留
SLIDING_WINDOW_TURNS = 5   # 最近 5 轮对话完整保留
SUMMARY_MAX_TOKENS = 800   # 历史摘要最多占 800 token


def estimate_messages_tokens(messages):
    """估算多条消息的总 token"""
    total = 0
    for msg in messages:
        content = msg.get("content", "") or ""
        total += estimate_tokens(content)
        # 工具调用也计入
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                total += estimate_tokens(tc.function.name) + estimate_tokens(tc.function.arguments)
        # RAG 搜索结果
        if isinstance(content, dict) and content.get("source") == "rag":
            total += estimate_tokens(json.dumps(content, ensure_ascii=False))
    return total


def get_conversation_summary(user_id):
    """从 memory_manager 获取对话摘要"""
    try:
        from backend.memory.memory_manager import get_conversation_summaries
        summary_text = get_conversation_summaries(user_id)
        if summary_text:
            return summary_text
    except Exception:
        pass
    return ""


def optimize_context(messages, user_id=None, max_tokens=MAX_HISTORY_TOKENS):
    """对外核心接口：输入消息列表，返回优化后的列表 + 摘要文本

    策略：
    1. 滑动窗口：最近 5 轮完整保留
    2. 优先级保留：重要消息(priority>=2)优先
    3. 摘要压缩：被截断的历史浓缩为摘要
    4. 时间衰减：同优先级下越新越优先
    """
    if not messages:
        return [], ""

    # 1. 估算每条消息
    scored = []
    now = datetime.now()
    for i, msg in enumerate(messages):
        content = msg.get("content", "") or ""
        tokens = estimate_tokens(content)
        priority = msg.get("priority", 1)
        # 时间衰减：越新的消息权重越高（优先级微调 0~0.5）
        age_boost = min(0.5, (len(messages) - i) / len(messages) * 0.5)
        effective_priority = priority + age_boost
        scored.append({
            "msg": msg,
            "tokens": tokens,
            "priority": priority,
            "effective": effective_priority,
            "idx": i
        })

    total_tokens = sum(s["tokens"] for s in scored)

    # 2. 没超限直接返回
    if total_tokens <= max_tokens:
        return messages, ""

    # 3. 超限 → 分三段处理
    # 3a. 滑动窗口：保留最后 SLIDING_WINDOW_TURNS 轮（每轮 = 1 user + 1 assistant + 可能的 tool）
    #     从末尾向前数，保留最近的完整对话轮次
    keep_indices = set()
    turns_found = 0
    for i in range(len(scored) - 1, -1, -1):
        keep_indices.add(i)
        if scored[i]["msg"].get("role") == "user":
            turns_found += 1
            if turns_found >= SLIDING_WINDOW_TURNS:
                break

    # 3b. 按优先级排序（滑动窗口之外的消息）
    sliding_window_msgs = [s for i, s in enumerate(scored) if i in keep_indices]
    others = [s for i, s in enumerate(scored) if i not in keep_indices]

    # 3c. 高优先级消息也保留（不论新旧）
    high_priority_others = [s for s in others if s["priority"] >= 2]
    low_priority_others = [s for s in others if s["priority"] < 2]

    # 4. 从高到低填充
    result = sliding_window_msgs[:]
    result_tokens = sum(s["tokens"] for s in result)

    # 先加高优先级（按新到旧）
    for s in reversed(high_priority_others):
        if result_tokens + s["tokens"] <= max_tokens:
            result.append(s)
            result_tokens += s["tokens"]

    # 再低优先级（按新到旧）
    for s in reversed(low_priority_others):
        if result_tokens + s["tokens"] <= max_tokens:
            result.append(s)
            result_tokens += s["tokens"]

    # 5. 生成摘要：被截掉的历史
    truncated = [s for i, s in enumerate(scored) if i not in {r["idx"] for r in result}]
    summary_text = ""
    if truncated:
        try:
            # 提取截断内容的要点
            summary_parts = []
            for s in truncated:
                role = s["msg"].get("role", "")
                content = (s["msg"].get("content", "") or "")
                if role == "user" and content:
                    summary_parts.append(f"用户问:{content[:60]}")
                elif role == "assistant" and content:
                    summary_parts.append(f"答:{content[:60]}")
                elif role == "tool" and content:
                    summary_parts.append("[数据查询]")
            if summary_parts:
                # 限制摘要 token
                summary = " | ".join(summary_parts[-15:])
                summary_tokens = estimate_tokens(summary)
                if summary_tokens > SUMMARY_MAX_TOKENS:
                    # 截断到 SUMMARY_MAX_TOKENS
                    ratio = SUMMARY_MAX_TOKENS / summary_tokens
                    cut_len = int(len(summary) * ratio)
                    summary = summary[:cut_len] + "..."
                summary_text = f"[历史对话摘要] {summary}"
        except Exception:
            pass

    # 6. 按原始顺序排序
    result.sort(key=lambda s: s["idx"])
    result_msgs = [s["msg"] for s in result]

    return result_msgs, summary_text


def window_info(tokens_used):
    """返回上下文窗口使用情况"""
    pct = round(tokens_used / MAX_HISTORY_TOKENS * 100, 1)
    return f"上下文使用: {tokens_used}/{MAX_HISTORY_TOKENS} token ({pct}%)"
