# coding: utf-8
"""验证码模块测试"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.database.database import init_db, seed_data
init_db()
seed_data()

from backend.captcha.captcha import generate_captcha


def test_captcha_generates_text():
    text = generate_captcha()
    assert text, "验证码文本不应为空"
    assert len(text) >= 2, f"验证码长度至少2，实际: {len(text)}"
    print(f"  [PASS] 验证码生成文本: {text}")


def test_captcha_generates_tuple():
    result = generate_captcha()
    assert isinstance(result, tuple), f"应返回元组，实际: {type(result)}"
    assert len(result) == 2, f"元组长度应为2，实际: {len(result)}"
    img, text = result
    assert text, "文本不应为空"
    print(f"  [PASS] 验证码返回格式正确: (Image, {text})")


def test_captcha_is_random():
    _, t1 = generate_captcha()
    _, t2 = generate_captcha()
    assert t1 != t2, "两次验证码应不同"
    print(f"  [PASS] 验证码随机性: {t1} != {t2}")


if __name__ == "__main__":
    print("=" * 40)
    print("测试验证码模块")
    print("=" * 40)
    test_captcha_generates_text()
    test_captcha_generates_tuple()
    test_captcha_is_random()
    print("=" * 40)
    print("所有验证码测试通过！")
