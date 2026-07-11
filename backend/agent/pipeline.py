"""Auto-extracted from multi_agent.py"""

import json, os, re
from datetime import datetime
from backend.agent.orchestrator import OrchestratorAgent

class Pipeline:
    def __init__(self, steps):
        self.steps = steps
    def run(self, user_msg, user_id):
        context = user_msg
        for agent, instruction in self.steps:
            full_query = f"{instruction}\n\n当前上下文：{context}"
            result = agent.chat([{"role": "user", "content": full_query}], user_id)
            context = f"上一步结果：{result}"
        return context

def needs_pipeline(message):
    for kw in ["分析", "建议", "优化", "为什么", "原因", "对比", "怎么办", "如何", "提升", "下降", "上涨", "异常", "整体", "总结"]:
        if kw in message:
            return True
    return False

class PipelineCoordinator:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
    def chat(self, messages, user_id=None, **kwargs):
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        # intent detection at entry point
        intent_result = self.orchestrator.intent_manager.update(user_msg)
        if intent_result["change_type"] in ("shift", "drift"):
            print(f"  [Intent] topic shift detected: {intent_result['last']} -> {intent_result['current']}")
        if not needs_pipeline(user_msg):
            return self.orchestrator.chat(messages, user_id, **kwargs)
        return self._run_pipeline(user_msg, user_id)
    def _run_pipeline(self, user_msg, user_id):
        pipeline = Pipeline([
            (self.orchestrator.data_agent, "请查询用户提到的相关数据。要求：只返回原始数据不分析，结构化展示"),
            (self.orchestrator.analysis_agent, "基于上一步的数据进行深度分析。要求：对比趋势找异常，归因分析找原因"),
            (self.orchestrator.knowledge_agent, "基于分析结果给出专业优化建议。要求：建议分优先级"),
        ])
        return pipeline.run(user_msg, user_id)
    def get_optimized_context(self, user_id):
        return self.orchestrator.get_optimized_context(user_id)
    def save_conversation(self, user_id, role, content, priority=1):
        self.orchestrator.save_conversation(user_id, role, content, priority)
    def get_history(self, user_id):
        return self.orchestrator.get_history(user_id)
    def chat_stream(self, messages, user_id=None, **kwargs):
        return self.orchestrator.chat_stream(messages, user_id, **kwargs)
    def route(self, intent):
        return None



