"""素材管理路由：获取列表、上传素材"""
from flask import request, jsonify
from backend.auth.auth import login_required
from .blueprints import material_bp
from . import shared as _shared


@material_bp.route("/api/materials", methods=["GET"])
@login_required
def api_materials():
    """获取素材列表"""
    uid = request.current_user.get("user_id", 0)
    conn = _shared.db_provider.get_connection()
    rows = conn.execute(
        "SELECT m.*,p.plan_name FROM materials m LEFT JOIN ad_plans p ON m.plan_id=p.id "
        "WHERE m.user_id=? ORDER BY m.id", (uid,)).fetchall()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "data": [dict(r) for r in rows]})


@material_bp.route("/api/materials", methods=["POST"])
@login_required
def api_create_material():
    """上传素材"""
    uid = request.current_user.get("user_id", 0)
    data = request.get_json(silent=True) or {}
    conn = _shared.db_provider.get_connection()
    conn.execute("INSERT INTO materials (name,type,url,plan_id,user_id) VALUES (?,?,?,?,?)",
                 (data.get("name"), data.get("type", "image"), data.get("url"),
                  data.get("plan_id", 0), uid))
    conn.commit()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "msg": "素材已上传"})
