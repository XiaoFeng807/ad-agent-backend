"""多 Agent 协同系统"""

from backend.agent.agent import Agent
from backend.agent.intent_classifier import classify_intent

# ==================== 子Agent专属提示词 ====================

DATA_AGENT_PROMPT = """你是【数据查询助手】，负责查询广告投放数据。
你的职责：
1. 只查数据，不做分析
2. 回答简洁，只基于返回的数据说话
3. 数据不足时如实回复不要编造
4. 涉及充值、修改预算等操作，拒绝并让用户去后台操作
5. 不要提及工具调用、数据库查询等内部细节

【长期记忆】
{memory}

【当前任务】
{task_context}"""

ANALYSIS_AGENT_PROMPT = """你是【数据分析助手】，负责分析广告投放数据并提供建议。
你的职责：
1. 只分析数据，不做敏感操作
2. 给出可操作的优化建议
3. 对比数据时指出关键差异
4. 数据不足时如实回复不要编造
5. 涉及充值、修改预算等操作，拒绝并让用户去后台操作
6. 不要提及工具调用、数据库查询等内部细节

【长期记忆】
{memory}

【当前任务】
{task_context}"""

KNOWLEDGE_AGENT_PROMPT = """你是【广告知识助手】，负责回答广告投放相关的行业知识。
你的职责：
1. 只回答广告行业标准、知识、技巧
2. 引用知识库中的数据，不要编造
3. 非广告相关的问题拒绝回答
4. 不要提及工具调用、数据库查询等内部细节

【长期记忆】
{memory}

【当前任务】
{task_context}"""

# ==================== 工具集划分 ====================

# 数据查询工具
DATA_TOOLS = [
    "get_dashboard_data", "get_daily_trend", "get_plans_summary",
    "get_alerts", "get_plan_detail", "get_account_detail",
    "get_daily_report_by_date",
    "get_week_over_week", "get_activity_timeline",
    "get_real_trends"
]

# 分析建议工具
ANALYSIS_TOOLS = [
    "compare_plans", "get_week_over_week",
    "get_decision_summary", "get_verified_suggestions"
]

# 知识查询工具
KNOWLEDGE_TOOLS = [
    "search_knowledge", "get_hot_products"
]

# ==================== 意图 → 子Agent 映射 ====================

INTENT_TO_AGENT = {
    "query_dashboard": "data",
    "query_account": "data",
    "query_plan": "data",
    "query_alert": "data",
    "query_trend": "data",
    "query_report": "data",
    "query_product": "knowledge",
    "query_knowledge": "knowledge",
    "optimize_suggestion": "analysis",
    "sensitive_operation": "block",   # 直接拦截
    "greeting": "data",               # 打招呼走数据Agent（最简单的回复）
    "injection_attempt": "block",     # 直接拦截
    "unknown": "data"                 # 不确定时走数据Agent
}


class ToolRegistry:
    """函数注册表（从 tools.py 加载指定工具）"""
    
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
    """子Agent：继承自 Agent，但用专属提示词和工具集"""
    
    def __init__(self, name, system_prompt, tool_names):
        registry = ToolRegistry(tool_names)
        super().__init__(tool_registry=registry)
        self.name = name
        self.system_prompt = system_prompt
    
    def chat(self, messages, user_id=None, **kwargs):
        """重写 chat()：使用子Agent自己的提示词"""
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
        
        # 获取工具定义
        tools_def = self.tool_registry.get_definitions() if self.tool_registry else []
        
        # 调用DeepSeek
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=payload,
            tools=tools_def if tools_def else None
        )
        msg = resp.choices[0].message
        
        # 处理函数调用
        if msg.tool_calls:
            from backend.memory.memory_manager import add_fact
            for tc in msg.tool_calls:
                import json
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
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
            
            final = self.client.chat.completions.create(
                model=self.model,
                messages=payload,
                temperature=0
            )
            return final.choices[0].message.content or ""
        
        return msg.content or ""


class Coordinator:
    """协调者：判断意图 → 分发给子Agent → 返回结果"""
    
    def __init__(self):
        self.data_agent = SubAgent("数据助手", DATA_AGENT_PROMPT, DATA_TOOLS)
        self.analysis_agent = SubAgent("分析助手", ANALYSIS_AGENT_PROMPT, ANALYSIS_TOOLS)
        self.knowledge_agent = SubAgent("知识助手", KNOWLEDGE_AGENT_PROMPT, KNOWLEDGE_TOOLS)
    
    def route(self, intent):
        """根据意图返回对应的子Agent"""
        agent_name = INTENT_TO_AGENT.get(intent, "data")
        if agent_name == "data":
            return self.data_agent
        elif agent_name == "analysis":
            return self.analysis_agent
        elif agent_name == "knowledge":
            return self.knowledge_agent
        return None
    
    def chat(self, messages, user_id=None, **kwargs):
        """协调者：先分类 → 路由 → 调用子Agent"""
        # 从消息中提取最后一条用户消息
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        
        # 意图识别
        intent = classify_intent(user_msg)
        
        # 拦截敏感操作和注入
        if intent == "sensitive_operation":
            return "涉及充值、修改预算等敏感操作，我无法直接执行。建议你前往后台【预算管理】页面手动操作。"
        if intent == "injection_attempt":
            return "我是智能投放助手，只能回答广告投放相关的问题。"
        
        # 路由到子Agent
        agent = self.route(intent)
        if agent:
            return agent.chat(messages, user_id, **kwargs)
        return self.data_agent.chat(messages, user_id, **kwargs)
    
    def save_conversation(self, user_id, role, content, priority=1):
        """共享保存功能（所有子Agent共用同一个数据库）"""
        self.data_agent.save_conversation(user_id, role, content, priority)
    
    def get_history(self, user_id):
        """共享获取历史"""
        return self.data_agent.get_history(user_id)
