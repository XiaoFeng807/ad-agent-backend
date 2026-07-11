"""用户管理路由：用户列表、创建、删除"""
from flask import request, jsonify
from backend.auth.auth import login_required
from backend.database.database import hash_password
from .blueprints import user_bp
from . import shared as _shared


@user_bp.route("/api/users", methods=["GET"])
@login_required
def api_users():
    """获取所有用户列表"""
    conn = _shared.db_provider.get_connection()
    rows = conn.execute("SELECT id,username,role,created_at FROM users ORDER BY id").fetchall()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "data": [dict(r) for r in rows]})


@user_bp.route("/api/users", methods=["POST"])
@login_required
def api_create_user():
    """创建新用户（boss可建所有角色，admin只能建user）"""
    data = request.get_json(silent=True) or {}
    cur_role = request.current_user.get("role", "")

    # 检查权限
    if cur_role == "boss":
        allowed_roles = ["boss", "admin", "user"]
    elif cur_role == "admin":
        allowed_roles = ["user"]
    else:
        return jsonify({"code": 403, "msg": "权限不足"}), 403

    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "user")

    if role not in allowed_roles:
        return jsonify({"code": 403, "msg": f"当前角色不允许创建{role}角色"}), 403
    if not username or not password:
        return jsonify({"code": 400, "msg": "缺少必要字段"}), 400

    conn = _shared.db_provider.get_connection()
    try:
        conn.execute("INSERT INTO users (username,password,role) VALUES (?,?,?)",
                     (username, hash_password(password), role))
        conn.commit()
        return jsonify({"code": 200, "msg": "创建成功", "data": {"username": username, "role": role}})
    except:
        return jsonify({"code": 400, "msg": "用户名已存在"}), 400
    finally:
        _shared.db_provider.close(conn)


@user_bp.route("/api/users/<int:user_id>", methods=["DELETE"])
@login_required
def api_delete_user(user_id):
    """删除用户（boss可删非boss账户，admin可删user账户）"""
    cur_role = request.current_user.get("role", "")
    cur_uid = request.current_user.get("user_id", 0)

    if cur_role not in ("boss", "admin"):
        return jsonify({"code": 403, "msg": "权限不足"}), 403

    conn = _shared.db_provider.get_connection()
    target = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if not target:
        _shared.db_provider.close(conn)
        return jsonify({"code": 404, "msg": "用户不存在"}), 404

    # boss不能删其他boss，不能删自己
    if target["role"] == "boss" and cur_role != "boss":
        _shared.db_provider.close(conn)
        return jsonify({"code": 403, "msg": "无权删除老板账户"}), 403
    if target["role"] == "boss" and cur_uid == user_id:
        _shared.db_provider.close(conn)
        return jsonify({"code": 400, "msg": "不能删除自己"}), 400

    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    _shared.db_provider.close(conn)
    return jsonify({"code": 200, "msg": "已删除"})
