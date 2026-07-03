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

    # 注入检测：防止用户试图劫持AI身份
    injection_patterns = [
        "忽略之前的", "忽略所有", "忽略指令", "无视",
        "你现在是", "你是一个", "扮演", "假装",
        "system prompt", "system_message",
        "不要遵守", "忘记", "reset"
    ]
    for pattern in injection_patterns:
        if pattern in message.lower():
            return jsonify({"code": 200, "data": {"reply": "我是智能投放助手，只能回答广告投放相关的问题。"}})


    # 保存用户消息（普通优先级） → 调AI回复 → 保存AI回复
    _shared.agent.save_conversation(user_id, "user", message, priority=1)
    history = _shared.agent.get_history(user_id)
    reply = _shared.agent.chat(history, user_id)
    # AI回复如果是调函数拿到的数据，标记为重要
    has_tool_call = any("调用了" in m.get("content","") for m in history[-3:]) if history else False
    ai_priority = 2 if has_tool_call else 1
    _shared.agent.save_conversation(user_id, "assistant", reply, priority=ai_priority)
    return jsonify({"code": 200, "data": {"reply": reply}})


@chat_bp.route("/api/chat/history")
@login_required
def api_chat_history():
    """获取聊天历史记录"""
    history = _shared.agent.get_history(request.current_user["user_id"])
    return jsonify({"code": 200, "data": history})
