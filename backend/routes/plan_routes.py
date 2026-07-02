"""投放计划路由：增删改查"""
from flask import request, jsonify
from backend.auth.auth import login_required
from backend.tools.tools import get_plans_summary
from .blueprints import plan_bp
from . import shared as _shared


@plan_bp.route("/api/plans", methods=["GET"])
@login_required
def api_plans():
    """获取所有广告计划"""
    uid = request.current_user.get("user_id", 0)
    data = get_plans_summary(user_id=uid)
    return jsonify({"code": 200, "data": data})


@plan_bp.route("/api/plans", methods=["POST"])
@login_required
def api_create_plan():
    """创建新的广告计划"""
    uid = request.current_user.get("user_id", 0)
    data = request.get_json(silent=True) or {}
    name = data.get("plan_name", "").strip()
    platform = data.get("platform", "")
    budget = float(data.get("daily_budget", 0))
    if not name:
        return jsonify({"code": 400, "msg": "请输入计划名称"}), 400
    conn = _shared.db_provider.get_connection()
    conn.execute("INSERT INTO ad_plans (plan_name,platform,daily_budget,user_id) VALUES (?,?,?,?)",
                 (name, platform, budget, uid))
    conn.commit()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "msg": "创建成功"})


@plan_bp.route("/api/plans/<int:plan_id>/toggle", methods=["POST"])
@login_required
def api_toggle_plan(plan_id):
    """暂停/启用广告计划"""
    uid = request.current_user.get("user_id", 0)
    conn = _shared.db_provider.get_connection()
    plan = conn.execute("SELECT status FROM ad_plans WHERE id=? AND user_id=?", (plan_id, uid)).fetchone()
    if not plan:
        _shared.db_provider.close(conn)
        return jsonify({"code": 404, "msg": "计划不存在"}), 404
    new = "paused" if plan["status"] == "active" else "active"
    conn.execute("UPDATE ad_plans SET status=? WHERE id=?", (new, plan_id))
    conn.commit()
    _shared.db_provider.close(conn)
    status_text = "暂停" if new == "paused" else "启用"
    return jsonify({"code": 200, "msg": f"已{status_text}投放"})


@plan_bp.route("/api/plans/<int:plan_id>", methods=["DELETE"])
@login_required
def api_delete_plan(plan_id):
    """删除广告计划"""
    uid = request.current_user.get("user_id", 0)
    conn = _shared.db_provider.get_connection()
    conn.execute("DELETE FROM ad_plans WHERE id=? AND user_id=?", (plan_id, uid))
    conn.commit()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "msg": "已删除"})
