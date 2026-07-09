# -*- coding: utf-8 -*-
"""多 Agent 协同系统 — Orchestrator + ReAct + 自我反思"""

import json, os
from backend.agent.agent import Agent
from backend.agent.llm_provider import ProviderFactory
from backend.prompts.data_agent import DATA_AGENT_PROMPT
from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT
from backend.prompts.orchestrator import ORCHESTRATOR_PROMPT
from backend.prompts.reflection import REFLECTION_PROMPT
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


DATA_TOOLS = [
    "get_dashboard_data",
    "get_daily_trend",
    "get_plans_summary",
    "get_alerts",
    "get_plan_detail",
    "get_account_detail",
    "get_daily_report_by_date",
    "get_week_over_week",
    "get_activity_timeline",
    "get_real_trends",
]

ANALYSIS_TOOLS = [
    "compare_plans",
    "get_week_over_week",
    "get_decision_summary",
    "get_verified_suggestions",
]

KNOWLEDGE_TOOLS = [
    "search_knowledge",
    "get_hot_products",
]


class ToolRegistry:
    def __init__(self, tool_names):
        from backend.tools.tools import func_names, function_map, tools_definition as all_defs
        self.func_names = [n for n in tool_names if n in func_names]
        self.function_map = {n: function_map[n] for n in self.func_names if n in function_map}
        self._defs = [d for d in all_defs if d.get("function", {}).get("name") in self.func_names]
    def get_definitions(self):
        return self._defs
    def get_tool(self, name):
        return self.function_map.get(name)


class SubAgent(Agent):
    def __init__(self, name, system_prompt, tool_names):
        registry = ToolRegistry(tool_names)
        super().__init__(tool_registry=registry)
        self.name = name
        self.system_prompt = system_prompt
    def chat(self, messages, user_id=None, **kwargs):
        from backend.memory.memory_manager import get_compact_memory
        memory_text = get_compact_memory(user_id) if user_id else ""
        task_context = kwargs.get("task_context", "")
        SYSTEM_PROMPT = self.system_prompt.format(
            memory=memory_text or "暂无历史记录",
            task_context=task_context or "无"
        )
        sys_msg = {"role": "system", "content": SYSTEM_PROMPT}
        from backend.agent.context_window import get_optimized_context
        optimized = get_optimized_context(messages)
        payload = [sys_msg] + optimized
        tools_def = self.tool_registry.get_definitions() if self.tool_registry else []
        resp = self.client.chat.completions.create(
            model=self.model, messages=payload,
            tools=tools_def if tools_def else None
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            from backend.memory.memory_manager import add_fact
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except:
                    args = {}
                func = self.tool_registry.get_tool(fn_name)
                if func:
                    if user_id:
                        args["user_id"] = user_id
                    result = func(**args)
                    if user_id:
                        add_fact(user_id, fn_name, args, result)
                    payload.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    payload.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
            final = self.client.chat.completions.create(
                model=self.model, messages=payload, temperature=0
            )
            return final.choices[0].message.content or ""
        return msg.content or ""


class OrchestratorAgent:
    def __init__(self):
        self.provider = ProviderFactory.create()
        self.model = os.getenv("MODEL", "deepseek-chat")
        self.client = self.provider
        self.data_agent = SubAgent("数据助手", DATA_AGENT_PROMPT, DATA_TOOLS)
        self.analysis_agent = SubAgent("分析助手", ANALYSIS_AGENT_PROMPT, ANALYSIS_TOOLS)
        self.knowledge_agent = SubAgent("知识助手", KNOWLEDGE_AGENT_PROMPT, KNOWLEDGE_TOOLS)
        self.sub_agent_tools = [
            {"type": "function", "function": {
                "name": "query_data",
                "description": "查询广告投放原始数据，包括花费、营收、ROAS、CPC等",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "用户查询需求"}
                }, "required": ["query"]}
            }},
            {"type": "function", "function": {
                "name": "analyze_data",
                "description": "分析广告数据：趋势分析、对比分析、归因分析",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "用户分析需求"}
                }, "required": ["query"]}
            }},
            {"type": "function", "function": {
                "name": "query_knowledge_base",
                "description": "查询广告行业知识、标准、建议",
                "parameters": {"type": "object", "properties": {
                    "question": {"type": "string", "description": "用户知识需求"}
                }, "required": ["question"]}
            }}
        ]

    def _extract_user_message(self, messages):
        for m in reversed(messages):
            if m.get("role") == "user":
                return m.get("content", "")
        return ""

    def _detect_sensitive(self, message):
        msg_lower = message.lower().strip()
        for kw in ["充值", "加钱", "加预算", "修改预算", "调预算", "删除计划", "暂停计划", "关闭计划"]:
            if kw in msg_lower:
                return "sensitive"
        for kw in ["忽略之前的", "忽略所有", "你现在是", "你是一个", "扮演", "假装", "忘记", "reset"]:
            if kw in msg_lower:
                return "injection"
        return None

    def _execute_sub_agent(self, tool_name, args, user_id):
        query = args.get("query") or args.get("question", "")
        if not query:
            return "查询内容为空"
        messages = [{"role": "user", "content": query}]
        if tool_name == "query_data":
            return self.data_agent.chat(messages, user_id)
        elif tool_name == "analyze_data":
            return self.analysis_agent.chat(messages, user_id)
        elif tool_name == "query_knowledge_base":
            return self.knowledge_agent.chat(messages, user_id)
        return "未知的子Agent"

    def chat(self, messages, user_id=None, **kwargs):
        """ReAct 循环 + 自我反思"""
        user_msg = self._extract_user_message(messages)
        detect = self._detect_sensitive(user_msg)
        if detect == "sensitive":
            return "涉及充值、修改预算等敏感操作，我无法直接执行。建议你前往后台【预算管理】页面手动操作。"
        if detect == "injection":
            return "我是智能投放助手，只能回答广告投放相关的问题。"
        from backend.memory.memory_manager import get_compact_memory
        memory_text = get_compact_memory(user_id) if user_id else ""
        system_content = ORCHESTRATOR_PROMPT.format(memory=memory_text or "暂无历史记录")
        orchestrator_msgs = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_msg}
        ]
        try:
            max_rounds = 5
            react_trace = []
            for round_idx in range(max_rounds):
                resp = self.client.chat.completions.create(
                    model=self.model, messages=orchestrator_msgs,
                    tools=self.sub_agent_tools, temperature=0.1
                )
                msg = resp.choices[0].message
                if msg.tool_calls:
                    tc = msg.tool_calls[0]
                    fn_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except:
                        args = {}
                    result = self._execute_sub_agent(fn_name, args, user_id)
                    tool_label = {"query_data": "查询数据", "analyze_data": "分析数据", "query_knowledge_base": "查询知识"}.get(fn_name, fn_name)
                    react_trace.append(tool_label)
                    orchestrator_msgs.append({
                        "role": "assistant", "content": None, "tool_calls": [tc]
                    })
                    orchestrator_msgs.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps({"result": result}, ensure_ascii=False)
                    })
                else:
                    return msg.content or ""

            final = self.client.chat.completions.create(
                model=self.model, messages=orchestrator_msgs, temperature=0
            )
            reply_text = final.choices[0].message.content or ""

            # 自我反思环节
            try:
                reflect_msgs = [
                    {"role": "system", "content": REFLECTION_PROMPT},
                    {"role": "user", "content": "请审核以下回答：\n\n" + reply_text}
                ]
                reflect_resp = self.client.chat.completions.create(
                    model=self.model, messages=reflect_msgs, temperature=0, max_tokens=2000
                )
                reflect_result = reflect_resp.choices[0].message.content or ""
                try:
                    parsed = json.loads(reflect_result)
                    if parsed.get("decision") == "improve":
                        improved = parsed.get("improved", "")
                        if improved:
                            reply_text = improved
                except:
                    pass
            except Exception:
                pass

            if react_trace:
                trace_text = "\n\n---\n[思考轨迹: " + " > ".join(react_trace) + "]"
                return reply_text + trace_text
            return reply_text

        except Exception as e:
            print(f"  [Orchestrator] 异常: {e}")
            try:
                return self.data_agent.chat(messages, user_id)
            except:
                return "抱歉，系统暂时无法处理您的请求，请稍后再试。"

    def chat_stream(self, messages, user_id=None, **kwargs):
        try:
            yield json.dumps({"type": "thinking", "content": "思考中..."}, ensure_ascii=False)
            full_reply = self.chat(messages, user_id, **kwargs)
            yield json.dumps({"type": "text", "content": full_reply}, ensure_ascii=False)
            yield json.dumps({"type": "done"})
        except Exception as e:
            print(f"  [chat_stream] 异常: {e}")
            try:
                for chunk in self.data_agent.chat_stream(messages, user_id):
                    yield chunk
            except:
                yield json.dumps({"type": "text", "content": "抱歉，系统暂时无法处理您的请求，请稍后再试。"}, ensure_ascii=False)
                yield json.dumps({"type": "done"})

    def save_conversation(self, user_id, role, content, priority=1):
        self.data_agent.save_conversation(user_id, role, content, priority)
    def get_history(self, user_id):
        return self.data_agent.get_history(user_id)


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
    def save_conversation(self, user_id, role, content, priority=1):
        self.orchestrator.save_conversation(user_id, role, content, priority)
    def get_history(self, user_id):
        return self.orchestrator.get_history(user_id)
    def chat_stream(self, messages, user_id=None, **kwargs):
        return self.orchestrator.chat_stream(messages, user_id, **kwargs)
    def route(self, intent):
        return None

