"""操作日志路由：按角色权限查询"""
from flask import request, jsonify
from backend.auth.auth import login_required
from .blueprints import log_bp
from . import shared as _shared


@log_bp.route("/api/logs")
@login_required
def api_logs():
    """获取操作日志（boss看全部，admin看admin+user，user只看自己）"""
    conn = _shared.db_provider.get_connection()
    role = request.current_user.get("role", "")
    uid = request.current_user.get("user_id", 0)

    if role == "boss":
        rows = conn.execute(
            "SELECT l.*, u.username FROM operation_logs l LEFT JOIN users u ON l.user_id=u.id "
            "ORDER BY l.id DESC LIMIT 50").fetchall()
    elif role == "admin":
        rows = conn.execute(
            "SELECT l.*, u.username FROM operation_logs l LEFT JOIN users u ON l.user_id=u.id "
            "WHERE l.user_id IN (SELECT id FROM users WHERE role IN ('admin','user')) "
            "ORDER BY l.id DESC LIMIT 50").fetchall()
    else:
        rows = conn.execute(
            "SELECT l.*, u.username FROM operation_logs l LEFT JOIN users u ON l.user_id=u.id "
            "WHERE l.user_id=? ORDER BY l.id DESC LIMIT 50", (uid,)).fetchall()

    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "data": [dict(r) for r in rows]})
