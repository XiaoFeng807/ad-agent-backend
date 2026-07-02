"""仪表盘路由：核心数据和趋势"""
from flask import request, jsonify
from backend.auth.auth import login_required
from backend.tools.tools import get_dashboard_data, get_daily_trend
from .blueprints import dashboard_bp


@dashboard_bp.route("/api/dashboard")
@login_required
def api_dashboard():
    """获取仪表盘核心数据（总花费、ROAS等）"""
    uid = request.current_user.get("user_id", 0)
    data = get_dashboard_data(user_id=uid)
    return jsonify({"code": 200, "data": data})


@dashboard_bp.route("/api/daily_reports")
@login_required
def api_daily_reports():
    """获取每日趋势数据（用于图表）"""
    uid = request.current_user.get("user_id", 0)
    trend = get_daily_trend(user_id=uid)
    return jsonify({"code": 200, "data": trend})
