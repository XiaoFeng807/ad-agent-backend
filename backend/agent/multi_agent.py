"""多 Agent 协同系统"""

from backend.agent.agent import Agent
from backend.agent.intent_classifier import classify_intent

from backend.prompts.data_agent import DATA_AGENT_PROMPT
from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT



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



# ==================== 流水线协作 ====================

class Pipeline:
    """流水线：多个Agent接力完成任务"""
    
    def __init__(self, stages):
        """
        stages: [(agent, instructions), ...]
          agent: SubAgent 实例
          instructions: str 告诉这个Agent上一步的结果和它要做什么
        """
        self.stages = stages
    
    def run(self, user_message, user_id=None):
        """执行流水线：逐级传递结果"""
        context = f"用户的原始问题：{user_message}"
        final_reply = ""
        
        for i, (agent, instruction) in enumerate(self.stages):
            # 给当前Agent的消息
            stage_prompt = f"{instruction}\n\n【上下文】\n{context}"
            stage_msgs = [{"role": "user", "content": stage_prompt}]
            
            # 调用Agent（不带工具，纯分析）
            try:
                reply = agent.chat(stage_msgs, user_id)
            except Exception as e:
                reply = f"[{agent.name}暂时无法处理此步骤]"
                print(f"  [流水线] {agent.name} 阶段异常: {e}")
            final_reply = reply
            
            # 把当前结果传给下一阶段
            context += f"\n\n第{i+1}阶段（{agent.name}）的输出：\n{reply}"
        
        return final_reply


# 检测是否需要流水线的关键词
PIPELINE_KEYWORDS = [
    # 分析类
    "分析", "原因", "为什么", "怎么回事", "剖析", "深挖", "复盘",
    # 优化建议类
    "建议", "优化", "改善", "提升", "怎么办", "如何",
    # 对比评估类
    "对比", "比较", "综合", "评估", "总结", "汇报", "全面",
    # 数据洞察类
    "趋势", "波动", "变化", "差异", "异常", "解读", "洞察", "深层", "根本原因",
    # 查询分析类
    "哪个", "表现", "怎么看", "什么情况", "关联", "关系",
    # 详情扩展类
    "分析并", "详细说说", "展开", "具体", "然后", "之后", "降低", "下降",
]



def needs_pipeline(message):
    """判断是否需要用流水线（复杂问题走流水线，简单问题直接路由）"""
    msg_lower = message.lower()
    # 如果有多个分析类关键词，走流水线
    score = sum(1 for kw in PIPELINE_KEYWORDS if kw in msg_lower)
    return score >= 1


class PipelineCoordinator:
    """带流水线能力的协调者"""
    
    def __init__(self, coordinator):
        self.coordinator = coordinator
    
    def chat(self, messages, user_id=None, **kwargs):
        """先判断是否需要流水线，不需要就走原来的路由"""
        # 提取用户消息
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        
        # 简单问题走原路由
        if not needs_pipeline(user_msg):
            return self.coordinator.chat(messages, user_id, **kwargs)
        
        # 复杂问题走流水线
        return self._run_pipeline(user_msg, user_id)
    
    def _run_pipeline(self, user_msg, user_id):
        """执行数据→分析→建议流水线"""
        pipeline = Pipeline([
            (self.coordinator.data_agent, 
             "请查询用户提到的相关数据。要求：①只返回原始数据不分析 ②结构化展示（表格式）"
             "③注明时间范围 ④尽可能覆盖花费、营收、ROAS、CPC等关键指标"),
            (self.coordinator.analysis_agent,
             "基于上一步的数据进行深度分析。要求：①对比趋势找异常 ②归因分析找原因 "
             "③指出风险点和优化机会 ④给出分析结论"),
            (self.coordinator.knowledge_agent,
             "基于分析结果给出专业优化建议。要求：①建议分优先级（紧急/短期/长期）"
             "②引用行业标准作参考 ③每个建议说明预期效果"),
        ])
        return pipeline.run(user_msg, user_id)



    def save_conversation(self, user_id, role, content, priority=1):
        """保存对话（委托给内部的coordinator）"""
        if hasattr(self.coordinator, 'save_conversation'):
            self.coordinator.save_conversation(user_id, role, content, priority)

    def get_history(self, user_id, max_messages=15):
        """获取聊天历史（委托给内部的coordinator）"""
        if hasattr(self.coordinator, 'get_history'):
            return self.coordinator.get_history(user_id)
    
    def chat_stream(self, messages, user_id=None, **kwargs):
        """流式聊天（委托给内部的coordinator）"""
        if hasattr(self.coordinator, 'chat_stream'):
            return self.coordinator.chat_stream(messages, user_id, **kwargs)
        return []

    def route(self, intent):
        """路由（委托给内部的coordinator）"""
        if hasattr(self.coordinator, 'route'):
            return self.coordinator.route(intent)
        return None






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
    
    def chat_stream(self, messages, user_id=None, **kwargs):
        """共享流式聊天（委托给数据Agent）"""
        return self.data_agent.chat_stream(messages, user_id, **kwargs)
