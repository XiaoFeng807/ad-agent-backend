# coding: utf-8
"""仪表盘函数测试"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.database.database import init_db, seed_data
init_db()
seed_data()

from backend.tools.tools import get_dashboard_data, get_daily_trend


def test_dashboard_returns_all_fields():
    """测试仪表盘返回了所有必需的字段"""
    data = get_dashboard_data(1)
    required = ["total_cost", "total_sales", "roas", "total_impressions",
                 "total_clicks", "ctr", "cpc", "total_orders", "cpa"]
    for field in required:
        assert field in data, f"缺少字段: {field}"
    print("  [PASS] 所有必需字段都存在")


def test_dashboard_values_are_positive():
    """测试仪表盘数值都是正数"""
    data = get_dashboard_data(1)
    for key in ["total_cost", "total_sales", "total_impressions", "total_clicks"]:
        assert data[key] > 0, f"{key} 应该大于0，实际: {data[key]}"
    print("  [PASS] 核心指标都是正数")


def test_dashboard_roas_reasonable():
    """测试ROAS在合理范围内（1~10之间）"""
    data = get_dashboard_data(1)
    assert 1 < data["roas"] < 10, f"ROAS 应在1-10之间，实际: {data['roas']}"
    print("  [PASS] ROAS 在合理范围")


def test_daily_trend_returns_correct_count():
    """测试每日趋势返回了指定天数的数据"""
    trend = get_daily_trend(7, user_id=1)
    assert len(trend) == 7, f"应返回7天数据，实际: {len(trend)}"
    print(f"  [PASS] 返回了 {len(trend)} 天趋势数据")


def test_daily_trend_has_required_fields():
    """测试趋势数据包含日期和指标字段"""
    trend = get_daily_trend(7, user_id=1)
    if trend:
        required = ["report_date", "cost", "impressions", "clicks", "sales"]
        for field in required:
            assert field in trend[0], f"趋势缺少字段: {field}"
    print("  [PASS] 趋势数据字段完整")


if __name__ == "__main__":
    print("=" * 40)
    print("测试仪表盘模块")
    print("=" * 40)
    test_dashboard_returns_all_fields()
    test_dashboard_values_are_positive()
    test_dashboard_roas_reasonable()
    test_daily_trend_returns_correct_count()
    test_daily_trend_has_required_fields()
    print("=" * 40)
    print("所有仪表盘测试通过！")
