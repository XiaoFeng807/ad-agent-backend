"""预算管理路由：账户列表、修改预算、充值"""
from flask import request, jsonify
from backend.auth.auth import login_required
from .blueprints import account_bp
from . import shared as _shared


@account_bp.route("/api/accounts")
@login_required
def api_accounts():
    """获取当前用户的广告账户列表"""
    uid = request.current_user.get("user_id", 0)
    conn = _shared.db_provider.get_connection()
    rows = conn.execute("SELECT * FROM ad_accounts WHERE user_id=? ORDER BY id", (uid,)).fetchall()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "data": [dict(r) for r in rows]})


@account_bp.route("/api/accounts/<int:acc_id>/budget", methods=["PUT"])
@login_required
def api_set_budget(acc_id):
    """修改账户日预算"""
    uid = request.current_user.get("user_id", 0)
    data = request.get_json(silent=True) or {}
    budget = float(data.get("daily_budget", 0))
    conn = _shared.db_provider.get_connection()
    conn.execute("UPDATE ad_accounts SET daily_budget=? WHERE id=? AND user_id=?", (budget, acc_id, uid))
    conn.commit()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "msg": "预算已更新"})


@account_bp.route("/api/accounts/<int:acc_id>/recharge", methods=["POST"])
@login_required
def api_recharge(acc_id):
    """账户充值"""
    uid = request.current_user.get("user_id", 0)
    data = request.get_json(silent=True) or {}
    amount = float(data.get("amount", 0))
    if amount <= 0:
        return jsonify({"code": 400, "msg": "金额必须大于0"}), 400
    conn = _shared.db_provider.get_connection()
    conn.execute("UPDATE ad_accounts SET balance=balance+? WHERE id=? AND user_id=?", (amount, acc_id, uid))
    conn.commit()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "msg": f"充值{amount}元成功"})
