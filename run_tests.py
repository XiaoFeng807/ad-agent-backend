#!/usr/bin/env python
# coding: utf-8
"""统一测试运行器"""
import os, sys, time, subprocess

TEST_FILES = [
    "backend/auth/tests/test_auth.py",
    "backend/tools/tests/test_tools.py",
    "backend/routes/tests/test_dashboard.py",
    "backend/agent/tests/test_intent_classifier.py",
    "backend/agent/tests/test_context_window.py",
    "backend/agent/tests/test_task_tracker.py",
    "backend/agent/tests/test_multi_agent.py",
    "backend/captcha/tests/test_captcha.py",
]

def run_all():
    passed = 0
    failed = 0
    total_start = time.time()

    print("=" * 50)
    print("  智能广告投放助手 - 测试套件")
    print("=" * 50)
    print()

    for test_file in TEST_FILES:
        filepath = os.path.join(os.path.dirname(__file__), test_file)
        if not os.path.exists(filepath):
            print(f"  [!] 文件不存在: {test_file}")
            continue

        print(f"  >>> 运行: {test_file}")
        start = time.time()

        result = subprocess.run(
            [sys.executable, filepath],
            capture_output=True, text=True, timeout=30
        )
        elapsed = time.time() - start

        if result.returncode == 0:
            passed += 1
            print(result.stdout.splitlines()[-1] if result.stdout else "")
            print(f"  <<< 通过 ({elapsed:.1f}s)")
        else:
            failed += 1
            error_line = (result.stderr or "").strip().split("\n")[-1][:80] if result.stderr else "unknown"
            print(f"  <<< 失败: {error_line}")
        print()

    total_time = time.time() - total_start
    print("=" * 50)
    total = passed + failed
    print(f"  总计: {total} 个测试文件")
    print(f"  通过: {passed}  |  失败: {failed}  |  用时: {total_time:.1f}s")
    print("=" * 50)

    return failed == 0

if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
