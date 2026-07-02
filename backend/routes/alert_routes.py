"""告警中心路由：获取告警、标记已读"""
from flask import request, jsonify
from backend.auth.auth import login_required
from backend.tools.tools import get_alerts
from .blueprints import alert_bp
from . import shared as _shared


@alert_bp.route("/api/alerts")
@login_required
def api_alerts():
    """获取告警列表"""
    uid = request.current_user.get("user_id", 0)
    data = get_alerts(user_id=uid)
    return jsonify({"code": 200, "data": data})


@alert_bp.route("/api/alerts/read", methods=["POST"])
@login_required
def api_alerts_read():
    """标记告警为已读（支持单个或批量）"""
    uid = request.current_user.get("user_id", 0)
    data = request.get_json(silent=True) or {}
    alert_ids = data.get("ids", [])
    single_id = data.get("alert_id")
    if not alert_ids and single_id is not None:
        alert_ids = [int(single_id)]
    conn = _shared.db_provider.get_connection()
    if alert_ids:
        # 标记指定ID
        placeholders = ",".join("?" * len(alert_ids))
        conn.execute(f"UPDATE alerts SET is_read=1 WHERE id IN ({placeholders}) AND user_id=?", (*alert_ids, uid))
    else:
        # 全部标记已读
        conn.execute("UPDATE alerts SET is_read=1 WHERE user_id=?", (uid,))
    conn.commit()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "msg": "已标记已读"})
