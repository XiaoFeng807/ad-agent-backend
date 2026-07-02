"""认证路由：验证码生成、登录、注册"""
import io, base64, hashlib, datetime
from flask import request, jsonify, current_app
from backend.auth.auth import login_required  # 登录检查装饰器
from backend.database.database import hash_password
from .blueprints import auth_bp
from . import shared as _shared  # 共享状态（数据库、认证实例等）
from backend.captcha.captcha import generate_captcha


@auth_bp.route("/api/captcha")
def api_captcha():
    """生成验证码，返回base64图片"""
    img, code = generate_captcha()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    cid = hashlib.md5((str(datetime.datetime.now()) + code).encode()).hexdigest()[:12]
    _shared.captcha_store[cid] = code  # 存起来，登录时比对
    return jsonify({
        "code": 200,
        "captcha_id": cid,
        "image": base64.b64encode(buf.getvalue()).decode()
    })


@auth_bp.route("/api/captcha/img")
def api_captcha_img():
    """直接返回验证码图片（带X-Captcha-Id头）"""
    img, code = generate_captcha()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    cid = hashlib.md5((str(datetime.datetime.now()) + code).encode()).hexdigest()[:12]
    _shared.captcha_store[cid] = code
    result = buf.getvalue()
    resp = current_app.make_response(result)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["X-Captcha-Id"] = cid  # 前端从这个头拿captcha_id
    return resp


@auth_bp.route("/api/login", methods=["POST"])
def api_login():
    """登录：验证用户名密码和验证码，返回JWT Token"""
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    captcha_id = data.get("captcha_id", "")
    captcha_code = data.get("captcha_code", "")

    if not username or not password:
        return jsonify({"code": 400, "msg": "用户名和密码不能为空"}), 400

    # 验证验证码
    stored = _shared.captcha_store.pop(captcha_id, None)
    if not stored or stored != captcha_code.lower():
        return jsonify({"code": 400, "msg": "验证码错误"}), 400

    # 调用认证模块验证用户名密码
    result = _shared.auth_instance.login(username, password)
    if not result:
        return jsonify({"code": 400, "msg": "用户名或密码错误"}), 400

    return jsonify({"code": 200, "data": result})


@auth_bp.route("/api/register", methods=["POST"])
@login_required
def api_register():
    """注册新用户（仅boss角色有权限）"""
    data = request.get_json(silent=True) or {}
    cur_role = request.current_user.get("role", "")
    if cur_role != "boss":
        return jsonify({"code": 403, "msg": "权限不足"}), 403

    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "user")
    if not username or not password:
        return jsonify({"code": 400, "msg": "缺少必要字段"}), 400

    conn = _shared.db_provider.get_connection()
    try:
        conn.execute("INSERT INTO users (username,password,role) VALUES (?,?,?)",
                     (username, hash_password(password), role))
        conn.commit()
        _log_action_local(request.current_user["user_id"], "创建用户", f"用户名:{username}, 角色:{role}")
        return jsonify({"code": 200, "msg": "注册成功"})
    except:
        return jsonify({"code": 400, "msg": "用户名已存在"}), 400
    finally:
        _shared.db_provider.close(conn)


def _log_action_local(user_id, action, detail=""):
    """记录操作日志"""
    try:
        conn = _shared.db_provider.get_connection()
        conn.execute("INSERT INTO operation_logs (user_id,action,detail) VALUES (?,?,?)",
                     (user_id, action, detail))
        conn.commit()
        _shared.db_provider.close(conn)
    except:
        pass
