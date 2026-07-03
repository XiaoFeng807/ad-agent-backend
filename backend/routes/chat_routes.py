from flask import request, jsonify, Response, stream_with_context
from backend.auth.auth import login_required
from .blueprints import chat_bp
from . import shared as _shared
import json, os, sys, traceback

@chat_bp.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    """发送消息给AI助手（非流式，兼容旧版）"""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    user_id = request.current_user["user_id"]
    if not message:
        return jsonify({"code": 400, "msg": "消息不能为空"}), 400

    injection_patterns = [
        "忽略之前的", "忽略所有", "忽略指令", "无视",
        "你现在是", "你是一个", "扮演", "假装",
        "system prompt", "system_message",
        "不要遵守", "忘记", "reset"
    ]
    for pattern in injection_patterns:
        if pattern in message.lower():
            return jsonify({"code": 200, "data": {"reply": "我是智能投放助手，只能回答广告投放相关的问题。"}})

    _shared.agent.save_conversation(user_id, "user", message, priority=1)
    history = _shared.agent.get_history(user_id)
    reply = _shared.agent.chat(history, user_id)
    _shared.agent.save_conversation(user_id, "assistant", reply, priority=1)
    return jsonify({"code": 200, "data": {"reply": reply}})


@chat_bp.route("/api/chat/stream", methods=["POST"])
@login_required
def api_chat_stream():
    """流式聊天：逐字返回AI回复，前端实时显示"""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    user_id = request.current_user["user_id"]
    if not message:
        return jsonify({"code": 400, "msg": "消息不能为空"}), 400

    injection_patterns = [
        "忽略之前的", "忽略所有", "忽略指令", "无视",
        "你现在是", "你是一个", "扮演", "假装",
        "system prompt", "system_message",
        "不要遵守", "忘记", "reset"
    ]
    for pattern in injection_patterns:
        if pattern in message.lower():
            def inject_block():
                yield "data: " + json.dumps({"type": "done", "content": "我是智能投放助手，只能回答广告投放相关的问题。"}) + "\n\n"
            return Response(inject_block(), mimetype="text/event-stream")

    import traceback
    def generate():
        try:
            # 先发"思考中"信号
            yield "data: " + json.dumps({"type": "thinking"}) + "\n\n"

            _shared.agent.save_conversation(user_id, "user", message, priority=1)
            history = _shared.agent.get_history(user_id)
            full_reply = ""
            for chunk in _shared.agent.chat_stream(history, user_id):
                full_reply += chunk
                yield "data: " + json.dumps({"type": "text", "content": chunk}) + "\n\n"

            # 保存完整回复到数据库
            _shared.agent.save_conversation(user_id, "assistant", full_reply, priority=1)

            yield "data: " + json.dumps({"type": "done"}) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"type": "error", "content": str(e)[:200]}) + "\n\n"
            yield "data: " + json.dumps({"type": "done"}) + "\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@chat_bp.route("/api/chat/history")
@login_required
def api_chat_history():
    """获取聊天历史记录"""
    history = _shared.agent.get_history(request.current_user["user_id"])
    return jsonify({"code": 200, "data": history})
