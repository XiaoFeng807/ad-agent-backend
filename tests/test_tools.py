# coding: utf-8
"""工具函数测试（get_real_trends、告警等）"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.database.database import init_db, seed_data
init_db()
seed_data()

from backend.tools.tools import get_real_trends, get_alerts, get_plans_summary


def test_real_trends_returns_data():
    """测试搜索趋势返回了正确的数据结构"""
    result = get_real_trends("AI 广告")
    assert "keyword" in result, "应返回关键词"
    assert "trend" in result, "应返回趋势数据"
    assert "source" in result, "应返回数据来源"
    assert len(result["trend"]) == 7, "应包含7天趋势数据"
    print(f"  [PASS] 搜索趋势返回 {len(result['trend'])} 天数据，来源: {result['source']}")


def test_real_trends_values_in_range():
    """测试趋势值在0-100范围内"""
    result = get_real_trends("AI 广告")
    for item in result["trend"]:
        assert 0 <= item["value"] <= 100, f"趋势值应在0-100之间，实际: {item['value']}"
    print("  [PASS] 所有趋势值在合理范围")


def test_alerts_returns_list():
    """测试告警列表返回正常"""
    alerts = get_alerts(user_id=1)
    assert isinstance(alerts, list), "告警应为列表"
    if alerts:
        assert "id" in alerts[0], "告警应有id字段"
        assert "message" in alerts[0], "告警应有message字段"
    print(f"  [PASS] 告警列表返回 {len(alerts)} 条")


def test_plans_summary():
    """测试广告计划列表"""
    plans = get_plans_summary(user_id=1)
    assert isinstance(plans, list), "计划应为列表"
    assert len(plans) > 0, "应有至少一个计划"
    assert "plan_name" in plans[0], "计划应有名称"
    print(f"  [PASS] 广告计划返回 {len(plans)} 条")


if __name__ == "__main__":
    print("=" * 40)
    print("测试工具函数模块")
    print("=" * 40)
    test_real_trends_returns_data()
    test_real_trends_values_in_range()
    test_alerts_returns_list()
    test_plans_summary()
    print("=" * 40)
    print("所有工具函数测试通过！")
