"""AI决策路由：决策统计、活动时间轴"""
from flask import request, jsonify
from backend.auth.auth import login_required
from backend.tools.tools import get_decision_summary, get_verified_suggestions, get_activity_timeline
from .blueprints import decision_bp


@decision_bp.route("/api/decisions")
@login_required
def api_decisions():
    """获取AI决策记录和已验证的建议"""
    uid = request.current_user.get("user_id", 0)
    summary = get_decision_summary(user_id=uid)
    verified = get_verified_suggestions(user_id=uid)
    return jsonify({"code": 200, "data": {"summary": summary, "verified": verified}})


@decision_bp.route("/api/timeline")
@login_required
def api_timeline():
    """获取活动时间轴"""
    uid = request.current_user.get("user_id", 0)
    result = get_activity_timeline(user_id=uid)
    return jsonify({"code": 200, "data": result})
