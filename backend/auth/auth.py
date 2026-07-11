"""认证模块：登录验证、JWT Token生成与验证、登录检查装饰器"""
import jwt, datetime
from functools import wraps
from flask import request, jsonify

# JWT加密密钥（实际项目应该放在.env里）
import os
from backend.config.config import settings
SECRET = settings.JWT_SECRET


class Auth:
    """认证类：处理登录和Token验证"""

    def __init__(self, db_provider=None):
        self.db = db_provider

    def login(self, username, password):
        """验证用户名密码，成功返回JWT Token"""
        from backend.database.database import hash_password
        conn = self.db.get_connection()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        self.db.close(conn)
        if not user or user["password"] != hash_password(password):
            return None
        token = jwt.encode({
            "user_id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=24)
        }, SECRET, algorithm="HS256")
        # 记录当前 token，旧 token 自动失效
        conn = self.db.get_connection()
        conn.execute("INSERT OR REPLACE INTO sessions (user_id, token, created_at) VALUES (?, ?, datetime('now'))",
                     (user["id"], token))
        self.db.close(conn)
        return {"token": token, "user": {"user_id": user["id"], "username": user["username"], "role": user["role"]}}

    def validate_token(self, token):
        """验证Token是否有效"""
        try:
            return jwt.decode(token, SECRET, algorithms=["HS256"])
        except:
            return None


def login_required(f):
    """装饰器：需要登录才能访问的接口加上这个装饰器"""
    @wraps(f)
    def wrapper(*a, **kw):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "")
        if not token:
            return jsonify({"code": 401, "msg": "未登录"}), 401
        try:
            payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        except:
            return jsonify({"code": 401, "msg": "Token无效或已过期"}), 401
        if not payload:
            return jsonify({"code": 401, "msg": "Token已过期"}), 401
        request.current_user = payload
        return f(*a, **kw)
    return wrapper


def set_auth_instance(auth):
    """保存Auth实例到全局"""
    global _auth_instance
    _auth_instance = auth
