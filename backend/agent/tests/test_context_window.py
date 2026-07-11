# coding: utf-8
"""上下文窗口管理测试"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.agent.context_window import estimate_tokens, trim_history, get_optimized_context


def test_token_estimation():
    """Token 估算准确"""
    assert estimate_tokens("你好") == 2, f"中文估算错误: {estimate_tokens('你好')}"
    assert estimate_tokens("hello world") == 3, f"英文估算错误: {estimate_tokens('hello world')}"
    assert estimate_tokens("") == 0, "空字符串应为0"
    assert estimate_tokens("ROAS = 销售额 / 花费") > 0
    print(f"  [PASS] Token 估算正确")


def test_empty_messages():
    """空消息列表不报错"""
    trimmed, tokens = trim_history([], max_tokens=100)
    assert trimmed == [], "空列表应返回空"
    assert tokens == 0, "空列表token应为0"
    print(f"  [PASS] 空消息处理正确")


def test_no_truncation_needed():
    """短消息列表不需要截断"""
    msgs = [{"role": "user", "content": "你好", "priority": 1},
            {"role": "assistant", "content": "你好！", "priority": 1}]
    trimmed, tokens = trim_history(msgs, max_tokens=1000)
    assert len(trimmed) == 2, "短消息不应被截断"
    print(f"  [PASS] 短消息无需截断: {len(trimmed)}条/{tokens} tokens")


def test_priority_retention():
    """高优先级消息优先保留"""
    msgs = [{"role": "user", "content": "低优先级", "priority": 0},
            {"role": "assistant", "content": "重要：预算已修改为500", "priority": 2},
            {"role": "user", "content": "普通消息", "priority": 1}]
    trimmed, tokens = trim_history(msgs, max_tokens=40)
    contents = [m["content"] for m in trimmed]
    assert "重要：预算已修改为500" in contents, "高优先级应保留"
    print(f"  [PASS] 优先级保留正确: {contents}")


def test_truncation_overflow():
    """超长消息应被截断"""
    long_msg = {"role": "user", "content": "测试消息 " * 200, "priority": 1}
    trimmed, tokens = trim_history([long_msg], max_tokens=50)
    assert len(trimmed) == 0, "单条超长消息应丢弃"
    print(f"  [PASS] 超长消息处理正确: 0条/{tokens} tokens")


def test_get_optimized_context():
    """外部接口正常工作"""
    msgs = [{"role": "user", "content": "今天数据怎么样"}]
    result = get_optimized_context(msgs)
    assert len(result) == 1
    assert result[0]["content"] == "今天数据怎么样"
    print(f"  [PASS] get_optimized_context 接口正确")


if __name__ == "__main__":
    print("=" * 40)
    print("测试上下文窗口管理模块")
    print("=" * 40)
    test_token_estimation()
    test_empty_messages()
    test_no_truncation_needed()
    test_priority_retention()
    test_truncation_overflow()
    test_get_optimized_context()
    print("=" * 40)
    print("所有上下文窗口测试通过！")
