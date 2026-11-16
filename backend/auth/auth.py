"""认证模块：登录验证、JWT Token生成与验证、登录检查装饰器"""
from datetime import datetime, timedelta, timezone
from functools import wraps
from uuid import uuid4

import jwt
from flask import request, jsonify

from backend.config.config import settings
from backend.database.mysql_diagnosis import is_mysql


class JWTConfigurationError(RuntimeError):
    """登录服务缺少有效的 JWT 签名密钥。"""


def _signing_key():
    secret = settings.JWT_SECRET.strip()
    if not secret or secret == "ad_agent_secret_key_2026":
        raise JWTConfigurationError("请配置专用 JWT_SECRET")
    return secret


class Auth:
    """认证类：处理登录和Token验证"""

    def __init__(self, db_provider=None):
        self.db = db_provider

    def login(self, username, password):
        """验证用户名密码，成功返回JWT Token"""
        from backend.database.database import hash_password
        secret = _signing_key()
        conn = self.db.get_connection()
        try:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not user or user["password"] != hash_password(password):
                return None
            token = jwt.encode({
                "user_id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "exp": datetime.now(timezone.utc) + timedelta(hours=24),
                "jti": uuid4().hex,
            }, secret, algorithm="HS256")
            with conn:
                if is_mysql(conn):
                    conn.execute(
                        "INSERT INTO sessions (user_id, token) VALUES (?, ?) AS incoming "
                        "ON DUPLICATE KEY UPDATE token=incoming.token, created_at=CURRENT_TIMESTAMP(3)",
                        (user["id"], token),
                    )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO sessions (user_id, token, created_at) VALUES (?, ?, datetime('now'))",
                        (user["id"], token),
                    )
        finally:
            self.db.close(conn)
        return {"token": token, "user": {"user_id": user["id"], "username": user["username"], "role": user["role"]}}

    def validate_token(self, token, *, connection=None, lock_session=False):
        """验证签名、有效期、用户 ID 和当前已提交的会话。"""
        try:
            payload = jwt.decode(token, _signing_key(), algorithms=["HS256"], options={"require": ["exp", "user_id"]})
        except (JWTConfigurationError, jwt.InvalidTokenError, TypeError, ValueError, OverflowError):
            return None
        user_id = payload["user_id"]
        if type(user_id) is not int or not 0 < user_id <= 2**63 - 1:
            return None
        if connection is not None:
            return payload if self._session_matches(connection, user_id, token, lock=lock_session) else None
        conn = self.db.get_connection()
        try:
            return payload if self._session_matches(conn, user_id, token) else None
        finally:
            self.db.close(conn)

    @staticmethod
    def _session_matches(conn, user_id, token, *, lock=False):
        suffix = " FOR UPDATE" if lock else ""
        row = conn.execute("SELECT token FROM sessions WHERE user_id=?" + suffix, (user_id,)).fetchone()
        return row is not None and row["token"] == token


def login_required(f):
    """装饰器：需要登录才能访问的接口加上这个装饰器"""
    @wraps(f)
    def wrapper(*a, **kw):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "")
        if not token:
            return jsonify({"code": 401, "msg": "未登录"}), 401
        payload = _auth_instance.validate_token(token)
        if payload is None:
            return jsonify({"code": 401, "msg": "Token无效或已过期"}), 401
        request.current_user = payload
        return f(*a, **kw)
    return wrapper


def set_auth_instance(auth):
    """保存Auth实例到全局"""
    global _auth_instance
    _auth_instance = auth
