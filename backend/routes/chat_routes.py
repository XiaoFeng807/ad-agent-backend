from flask import request, jsonify, Response, stream_with_context
from backend.auth.auth import login_required
from .blueprints import chat_bp
from . import shared as _shared
from backend.agent.intent_classifier import classify_intent, should_skip_llm, get_intent_description
from backend.agent.task_tracker import track_message, get_task_context
import json


@chat_bp.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    """发送消息给AI助手（非流式，兼容旧版）"""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    user_id = request.current_user["user_id"]
    if not message:
        return jsonify({"code": 400, "msg": "消息不能为空"}), 400

    # 意图识别：先分类再处理
    intent = classify_intent(message)

    # 注入攻击 → 直接拦截
    if intent == "injection_attempt":
        return jsonify({"code": 200, "data": {"reply": "我是智能投放助手，只能回答广告投放相关的问题。"}})

    # 敏感操作 → 只给建议不执行
    if intent == "sensitive_operation":
        reply = "涉及充值、修改预算等敏感操作，我无法直接执行。建议你前往后台【预算管理】页面手动操作。"
        return jsonify({"code": 200, "data": {"reply": reply}})

    # 任务追踪
    task_status, task_type, task_detail = track_message(intent, message)
    task_context = get_task_context()

    _shared.agent.save_conversation(user_id, "user", message, priority=1)
    history = _shared.agent.get_history(user_id)
    reply = _shared.agent.chat(history, user_id, task_context=task_context)
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

    # 意图识别
    intent = classify_intent(message)

    def generate():
        # 注入检测
        if intent == "injection_attempt":
            yield "data: " + json.dumps({"type": "done", "content": "我是智能投放助手，只能回答广告投放相关的问题。"}) + "\n\n"
            return

        # 敏感操作
        if intent == "sensitive_operation":
            yield "data: " + json.dumps({"type": "text", "content": "涉及充值、修改预算等敏感操作，我无法直接执行。建议你前往后台【预算管理】页面手动操作。"}) + "\n\n"
            yield "data: " + json.dumps({"type": "done"}) + "\n\n"
            return

        try:
            yield "data: " + json.dumps({"type": "thinking"}) + "\n\n"
            task_status, task_type, task_detail = track_message(intent, message)
            task_context = get_task_context()
            _shared.agent.save_conversation(user_id, "user", message, priority=1)
            history = _shared.agent.get_history(user_id)
            full_reply = ""
            for chunk in _shared.agent.chat_stream(history, user_id, task_context=task_context):
                full_reply += chunk
                yield "data: " + json.dumps({"type": "text", "content": chunk}) + "\n\n"
            _shared.agent.save_conversation(user_id, "assistant", full_reply, priority=1)
            # 对话摘要：保存关键信息供后续参考
            from backend.memory.memory_manager import store_conversation_summary
            store_conversation_summary(user_id, message, full_reply)
            yield "data: " + json.dumps({"type": "done"}) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"type": "error", "content": str(e)[:200]}) + "\n\n"
            yield "data: " + json.dumps({"type": "done"}) + "\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@chat_bp.route("/api/chat/history")
@login_required
def api_chat_history():
    """获取聊天历史记录"""
    history = _shared.agent.get_history(request.current_user["user_id"])
    return jsonify({"code": 200, "data": history})
