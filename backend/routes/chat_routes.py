"""AI聊天路由：发送消息、获取历史记录"""
from flask import request, jsonify
from backend.auth.auth import login_required
from .blueprints import chat_bp
from . import shared as _shared


@chat_bp.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    """发送消息给AI助手"""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    user_id = request.current_user["user_id"]
    if not message:
        return jsonify({"code": 400, "msg": "消息不能为空"}), 400

    # 保存用户消息 → 调AI回复 → 保存AI回复
    _shared.agent.save_conversation(user_id, "user", message)
    history = _shared.agent.get_history(user_id)
    reply = _shared.agent.chat(history, user_id)
    _shared.agent.save_conversation(user_id, "assistant", reply)
    return jsonify({"code": 200, "data": {"reply": reply}})


@chat_bp.route("/api/chat/history")
@login_required
def api_chat_history():
    """获取聊天历史记录"""
    history = _shared.agent.get_history(request.current_user["user_id"])
    return jsonify({"code": 200, "data": history})
