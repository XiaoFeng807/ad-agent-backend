# coding: utf-8
"""任务追踪模块测试"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.agent.task_tracker import TaskTracker, track_message, get_task_context
from backend.agent.intent_classifier import classify_intent


def test_new_task_started():
    """第一条消息创建新任务"""
    tracker = TaskTracker()
    status, task, detail = tracker.update("query_dashboard", "今天数据怎么样")
    assert status == "started", f"新任务应为started，实际: {status}"
    assert task == "viewing_dashboard", f"应为viewing_dashboard，实际: {task}"
    print(f"  [PASS] 新任务开始: {status} -> {task}")


def test_task_continuation():
    """同一任务延续"""
    tracker = TaskTracker()
    tracker.update("query_dashboard", "今天数据怎么样")
    status, task, detail = tracker.update("query_report", "再对比一下上周")
    assert status == "continued", f"同任务延续应为continued，实际: {status}"
    assert detail == 2, f"轮数应为2，实际: {detail}"
    print(f"  [PASS] 任务延续: 第{detail}轮({status})")


def test_task_switch():
    """任务切换"""
    tracker = TaskTracker()
    tracker.update("query_dashboard", "今天数据怎么样")
    status, task, detail = tracker.update("query_account", "看看账户余额")
    assert status == "switched", f"任务切换应为switched，实际: {status}"
    assert task == "viewing_account", f"应切换为viewing_account，实际: {task}"
    print(f"  [PASS] 任务切换: {status} -> {task}")


def test_task_completion():
    """任务完成"""
    tracker = TaskTracker()
    tracker.update("query_dashboard", "今天数据怎么样")
    status, task, detail = tracker.update("unknown", "好了谢谢")
    assert status == "completed", f"任务完成应为completed，实际: {status}"
    print(f"  [PASS] 任务完成: {status}")


def test_task_context_output():
    """任务上下文格式化正确"""
    tracker = TaskTracker()
    tracker.update("query_dashboard", "今天数据怎么样")
    tracker.update("query_report", "对比上周")
    ctx = tracker.get_context()
    assert "查看仪表盘" in ctx, f"上下文应包含任务描述"
    assert "2轮" in ctx, f"上下文应包含轮数"
    print(f"  [PASS] 任务上下文: {ctx}")


def test_reset():
    """重置追踪器"""
    tracker = TaskTracker()
    tracker.update("query_dashboard", "今天数据怎么样")
    tracker.reset()
    assert tracker.current_task is None, "重置后无当前任务"
    assert tracker.turn_count == 0, "重置后轮数为0"
    print(f"  [PASS] 重置追踪器正确")


def test_full_conversation_flow():
    """模拟完整对话流程"""
    tracker = TaskTracker()
    dialog = [
        ("query_dashboard", "今天数据怎么样", "started"),
        ("query_report", "对比上周", "continued"),
        ("query_account", "看看余额", "switched"),
        ("unknown", "好了", "completed"),
    ]
    for intent, msg, expected_status in dialog:
        status, task, detail = tracker.update(intent, msg)
        assert status == expected_status, f"\"{msg}\": 预期{expected_status}，实际{status}"
    print(f"  [PASS] 完整流程模拟正确: {len(dialog)}步")


if __name__ == "__main__":
    print("=" * 40)
    print("测试任务追踪模块")
    print("=" * 40)
    test_new_task_started()
    test_task_continuation()
    test_task_switch()
    test_task_completion()
    test_task_context_output()
    test_reset()
    test_full_conversation_flow()
    print("=" * 40)
    print("所有任务追踪测试通过！")
