# coding: utf-8
"""工具函数完整测试"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.database.database import init_db, seed_data
init_db()
seed_data()

from backend.tools.tools import (
    get_dashboard_data, get_daily_trend, get_plans_summary,
    get_alerts, get_plan_detail, get_account_detail,
    get_week_over_week, get_activity_timeline,
    get_hot_products, search_knowledge,
    compare_plans, get_decision_summary,
    get_verified_suggestions, get_real_trends
)


def test_dashboard_returns_data():
    """测试仪表盘返回核心指标"""
    data = get_dashboard_data(user_id=1)
    assert isinstance(data, dict), "仪表盘数据应为字典"
    assert "total_cost" in data, "应有总花费"
    assert "total_sales" in data, "应有总营收"
    assert "roas" in data, "应有ROAS"
    assert "cpc" in data, "应有CPC"
    assert "total_impressions" in data, "应有展示量"
    assert "total_clicks" in data, "应有点击量"
    print(f"  [PASS] 仪表盘: 花费={data['total_cost']}, ROAS={data['roas']}")


def test_daily_trend():
    """测试每日趋势"""
    trend = get_daily_trend(days=7, user_id=1)
    assert isinstance(trend, list), "趋势应为列表"
    assert len(trend) <= 7, f"最多7天，实际: {len(trend)}"
    if trend:
        assert "report_date" in trend[0], "应有日期"
        assert "cost" in trend[0], "应有花费"
        assert "sales" in trend[0], "应有营收"
    print(f"  [PASS] 每日趋势: {len(trend)}天")


def test_plans_summary():
    """测试广告计划列表"""
    plans = get_plans_summary(user_id=1)
    assert isinstance(plans, list), "计划应为列表"
    assert len(plans) > 0, "应有至少一个计划"
    assert "plan_name" in plans[0], "计划应有名称"
    names = [p["plan_name"] for p in plans]
    print(f"  [PASS] 广告计划: {len(plans)}个 ({', '.join(names[:3])}...)")


def test_week_over_week():
    """测试周同比"""
    wow = get_week_over_week(user_id=1)
    assert isinstance(wow, dict), "周同比应为字典"
    assert "this_week" in wow, "应有本周数据"
    assert "last_week" in wow, "应有上周数据"
    assert "cost" in wow.get("this_week", {}), "本周应有花费"
    print(f"  [PASS] 周同比: 本周花费={wow.get('this_week', {}).get('cost')}")


def test_plan_detail():
    """测试计划详情"""
    detail = get_plan_detail(plan_id=1, user_id=1)
    assert isinstance(detail, dict), "计划详情应为字典"
    assert "plan_name" in detail or "name" in detail, "计划应有名称"
    print(f"  [PASS] 计划详情: {detail.get('plan_name', detail.get('name'))}")


def test_account_detail():
    """测试账户详情"""
    account = get_account_detail(account_id=1, user_id=1)
    assert isinstance(account, dict), "账户详情应为字典"
    assert "account_name" in account or "name" in account, "账户应有名称"
    print(f"  [PASS] 账户详情: {account.get('account_name', account.get('name'))}")


def test_compare_plans():
    """测试计划对比"""
    result = compare_plans(plan_id_1=1, plan_id_2=2, user_id=1)
    assert isinstance(result, dict), "对比结果应为字典"
    print(f"  [PASS] 计划对比完成")


def test_real_trends():
    """测试搜索趋势"""
    result = get_real_trends("AI 广告")
    assert "keyword" in result
    assert "trend" in result
    assert len(result["trend"]) == 7
    print(f"  [PASS] 搜索趋势: {result['keyword']} ({len(result['trend'])}天)")


def test_hot_products():
    """测试热销产品"""
    products = get_hot_products(category="all", user_id=1)
    if isinstance(products, dict):
        plist = products.get("products", [])
    else:
        plist = products
    assert isinstance(plist, list), "产品应为列表"
    if plist:
        assert "name" in plist[0], "产品应有名称"
    print(f"  [PASS] 热销产品: {len(plist)}个")


def test_search_knowledge():
    """测试知识库搜索"""
    results = search_knowledge("ROAS")
    if isinstance(results, str):
        import json
        try:
            parsed = json.loads(results)
            if isinstance(parsed, dict):
                context = parsed.get("context", "")
                has_result = len(context) > 0
            else:
                has_result = len(results) > 10
        except:
            has_result = len(results) > 10
    else:
        has_result = len(results) > 0
    assert has_result, "应返回知识内容"
    print(f"  [PASS] 知识库搜索成功")


def test_alerts():
    """测试告警"""
    alerts = get_alerts(user_id=1)
    assert isinstance(alerts, list), "告警应为列表"
    if alerts:
        assert "message" in alerts[0], "告警应有消息"
    print(f"  [PASS] 告警: {len(alerts)}条")


def test_activity_timeline():
    """测试活动时间线"""
    timeline = get_activity_timeline(user_id=1)
    items = timeline.get("plans", []) if isinstance(timeline, dict) else (timeline if isinstance(timeline, list) else [])
    assert isinstance(items, list), "时间线应为列表"
    print(f"  [PASS] 活动时间线: {len(items)}条")


if __name__ == "__main__":
    print("=" * 50)
    print("工具函数全面测试")
    print("=" * 50)
    test_dashboard_returns_data()
    test_daily_trend()
    test_plans_summary()
    test_week_over_week()
    test_plan_detail()
    test_account_detail()
    test_compare_plans()
    test_real_trends()
    test_hot_products()
    test_search_knowledge()
    test_alerts()
    test_activity_timeline()
    print("=" * 50)
    print("所有工具函数测试通过！")
