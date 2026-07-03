# coding: utf-8
"""登录模块测试"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from flask import Flask
from backend.database.database import init_db, seed_data, hash_password
from backend.di import DatabaseProvider
from backend.auth.auth import Auth
from backend.captcha.captcha import generate_captcha

init_db()
seed_data()
db_provider = DatabaseProvider()
auth = Auth(db_provider)


def test_password_hashing():
    """测试密码加密：同样的密码，哈希值应该一致"""
    h1 = hash_password("admin123")
    h2 = hash_password("admin123")
    assert h1 == h2, "相同密码应生成相同哈希"
    print("  [PASS] 密码哈希一致")


def test_login_boss():
    """测试老板账号登录"""
    result = auth.login("boss", "admin123")
    assert result is not None, "boss 应该能登录成功"
    assert "token" in result, "登录应返回token"
    assert result["user"]["role"] == "boss", "角色应为boss"
    print("  [PASS] boss登录成功")


def test_login_wrong_password():
    """测试错误密码登录失败"""
    result = auth.login("boss", "wrongpass")
    assert result is None, "错误密码应返回None"
    print("  [PASS] 错误密码登录被拒绝")


def test_login_nonexistent_user():
    """测试不存在的用户登录失败"""
    result = auth.login("nobody", "pass123")
    assert result is None, "不存在用户应返回None"
    print("  [PASS] 不存在用户登录被拒绝")


def test_captcha_generates():
    """测试验证码生成是否正常"""
    img, code = generate_captcha()
    assert img is not None, "验证码图片不能为空"
    assert len(code) == 4, "验证码应为4位"
    assert code.isalnum(), "验证码应为字母数字组合"
    print(f"  [PASS] 验证码生成成功: {code}")


if __name__ == "__main__":
    print("=" * 40)
    print("测试登录模块")
    print("=" * 40)
    test_password_hashing()
    test_login_boss()
    test_login_wrong_password()
    test_login_nonexistent_user()
    test_captcha_generates()
    print("=" * 40)
    print("所有登录测试通过！")
