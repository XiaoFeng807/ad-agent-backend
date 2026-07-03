"""上下文窗口管理：智能截断+优先级保留，替代原来的 messages[-15:]"""

# ==================== Token 估算 ====================
# 不使用外部库，用经验公式估算
def estimate_tokens(text):
    """估算文本的 token 数（中文约 1.5 字/token，英文约 4 字/token）"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4) + 1


# ==================== 上下文窗口 ====================
# DeepSeek 上下文窗口大小
MAX_TOKENS = 32000       # 总窗口限制
SYSTEM_TOKENS = 2000     # 系统提示词占用
RESERVED_TOKENS = 2000   # 回复预留
MAX_HISTORY_TOKENS = MAX_TOKENS - SYSTEM_TOKENS - RESERVED_TOKENS  # 28000


def trim_history(messages, max_tokens=MAX_HISTORY_TOKENS):
    """智能截断：按优先级保留消息，超出时截断最早的低优先级消息"""
    if not messages:
        return [], 0

    # 1. 先估算每条消息的 token
    scored = []
    for msg in messages:
        tokens = estimate_tokens(msg.get("content", "") or "")
        priority = msg.get("priority", 1)
        scored.append((msg, tokens, priority))

    total_tokens = sum(s[1] for s in scored)

    # 2. 如果没超限，直接返回
    if total_tokens <= max_tokens:
        return messages, total_tokens

    # 3. 超限了，按优先级分组
    high_priority = [s for s in scored if s[2] >= 2]   # 重要消息
    normal = [s for s in scored if s[2] == 1]           # 普通消息
    low_priority = [s for s in scored if s[2] <= 0]     # 低优先级

    # 4. 高优先级保留，普通和低优先级从最早的开始删
    result = high_priority[:]
    result_tokens = sum(s[1] for s in high_priority)

    # 5. 从最新的普通消息开始加
    for s in reversed(normal):
        if result_tokens + s[1] <= max_tokens:
            result.append(s)
            result_tokens += s[1]

    # 6. 如果还有空间，加低优先级（从最新的开始）
    for s in reversed(low_priority):
        if result_tokens + s[1] <= max_tokens:
            result.append(s)
            result_tokens += s[1]

    # 7. 按原始顺序排序
    result.sort(key=lambda x: scored.index(x) if x in scored else 0)
    result_msgs = [s[0] for s in result]

    return result_msgs, result_tokens


def window_info(tokens_used):
    """返回上下文窗口使用情况"""
    pct = round(tokens_used / MAX_HISTORY_TOKENS * 100, 1)
    return f"上下文使用: {tokens_used}/{MAX_HISTORY_TOKENS} token ({pct}%)"


def get_optimized_context(messages):
    """对外接口：输入消息列表，返回优化后的列表"""
    trimmed, tokens = trim_history(messages)
    return trimmed


