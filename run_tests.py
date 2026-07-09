# coding: utf-8
"""测试运行器：一键运行所有单元测试"""
import os, sys, subprocess

os.chdir(os.path.dirname(os.path.abspath(__file__)))

TEST_FILES = [
    "backend/agent/tests/test_multi_agent.py",
    "backend/tools/tests/test_tools.py",
]

pass_count = 0
fail_count = 0

for test_file in TEST_FILES:
    path = os.path.join(os.path.dirname(__file__), test_file)
    if not os.path.exists(path):
        print(f"\n  Skip: {test_file} (not found)")
        continue

    print(f"\n{'=' * 50}")
    print(f"  Running: {test_file}")
    print(f"{'=' * 50}")
    
    # 用 subprocess 避免路径空格问题
    result = subprocess.run([sys.executable, path], capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr[:200])
    
    if result.returncode == 0:
        pass_count += 1
    else:
        fail_count += 1

print(f"\n{'=' * 50}")
print(f"  Results: {pass_count} passed, {fail_count} failed")
print(f"{'=' * 50}")
