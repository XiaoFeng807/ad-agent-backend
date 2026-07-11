"""Auto-extracted from multi_agent.py"""

import json, os, re
from datetime import datetime
from backend.agent.agent import Agent
from backend.agent.sub_agent import SubAgent
from backend.agent.llm_provider import ProviderFactory
from backend.prompts.orchestrator import ORCHESTRATOR_PROMPT
from backend.prompts.reflection import REFLECTION_PROMPT
from backend.prompts.quality import QUALITY_SCORE_PROMPT
from backend.prompts.data_agent import DATA_AGENT_PROMPT
from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT
from backend.agent.metacognition import get_metacognition
from backend.agent.intent_detector import get_intent_manager
from backend.agent.blackboard import Blackboard
from backend.memory.memory_manager import compose_context
from backend.memory.conversation_memory import store_conversation, search_conversation_tool, TOOL_DEFINITION
from backend.agent.competition import Competition
from dotenv import load_dotenv
from backend.config.config import settings

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

DATA_TOOLS = [
    "get_dashboard_data", "get_daily_trend", "get_plans_summary",
    "get_alerts", "get_plan_detail", "get_account_detail",
    "get_daily_report_by_date", "get_week_over_week",
    "get_activity_timeline", "get_real_trends",
]
ANALYSIS_TOOLS = [
    "compare_plans", "get_week_over_week",
    "get_decision_summary",
]
KNOWLEDGE_TOOLS = [
    "search_knowledge", "get_hot_products",
]

class OrchestratorAgent:
    def __init__(self):
        self.provider = ProviderFactory.create()
        self.model = settings.MODEL
        self.client = self.provider
        self.meta = get_metacognition(self.provider)
        self.intent_manager = get_intent_manager()
        self._meta_tools = ['query_data', 'analyze_data', 'query_knowledge_base']
        self.data_agent = SubAgent("数据助手", DATA_AGENT_PROMPT, DATA_TOOLS)
        self.analysis_agent = SubAgent("分析助手", ANALYSIS_AGENT_PROMPT, ANALYSIS_TOOLS)
        self.knowledge_agent = SubAgent("知识助手", KNOWLEDGE_AGENT_PROMPT, KNOWLEDGE_TOOLS)
        # Agent 互相注册
        agent_map = {
            "query_data": self.data_agent,
            "analyze_data": self.analysis_agent,
            "query_knowledge_base": self.knowledge_agent,
        }
        for name, agent in agent_map.items():
            agent.peers = {k: v for k, v in agent_map.items() if k != name}
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
            }},
            TOOL_DEFINITION
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

    def _execute_sub_agent(self, tool_name, args, user_id, blackboard=None):
        query = args.get("query") or args.get("question", "")
        if not query:
            return "查询内容为空"
        messages = [{"role": "user", "content": query}]
        # 注入 Blackboard 上下文
        board_context = blackboard.summary_for_prompt() if blackboard else ""
        if board_context:
            messages.insert(0, {"role": "system", "content": board_context})
        # 调用子 Agent
        if tool_name == "query_data":
            result = self.data_agent.chat(messages, user_id, blackboard=blackboard)
        elif tool_name == "analyze_data":
            result = self.analysis_agent.chat(messages, user_id, blackboard=blackboard)
        elif tool_name == "query_knowledge_base":
            result = self.knowledge_agent.chat(messages, user_id, blackboard=blackboard)
        else:
            return "未知的子Agent"
        # 写入 Blackboard
        if blackboard:
            blackboard.set(tool_name, result, agent_name=tool_name)
        return result
    def get_optimized_context(self, user_id):
        return self.data_agent.optimize_context(user_id)
    def _create_plan(self, user_msg):
        """动态任务拆解: 分析用户意图, 制定执行计划"""
        try:
            plan_prompt = (
                "分析用户问题, 制定执行计划。可选Agent: "
                "1.query_data 查数据, "
                "2.analyze_data 分析原因, "
                "3.query_knowledge_base 查知识。"
                "以JSON返回: {\"steps\": [{\"agent\": \"name\", \"reason\": \"原因\"}]}"
                "\n用户问题: " + user_msg
            )
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": "你是一个任务规划专家, 输出JSON格式计划。"},
                          {"role": "user", "content": plan_prompt}],
                temperature=0, max_tokens=500
            )
            plan_text = resp.choices[0].message.content or "{}"
            import json as _json
            if "```json" in plan_text:
                plan_text = plan_text.split("```json")[1].split("```")[0]
            elif "```" in plan_text:
                plan_text = plan_text.split("```")[1].split("```")[0]
            plan = _json.loads(plan_text.strip())
            steps = plan.get("steps", [])
            if steps:
                print("  [Plan]", " -> ".join([s.get("agent", "?") for s in steps]))
                return steps
        except Exception as e:
            print("  [Plan] 规划失败:", e)
        return []

    

    def chat(self, messages, user_id=None, **kwargs):
        """ReAct 循环 + 自我反思"""
        user_msg = self._extract_user_message(messages)
        detect = self._detect_sensitive(user_msg)
        if detect == "sensitive":
            return "涉及充值、修改预算等敏感操作，我无法直接执行。建议你前往后台【预算管理】页面手动操作。"
        if detect == "injection":
            return "我是智能投放助手，只能回答广告投放相关的问题。"
        # 元认知预检查
        meta_pre = self.meta.pre_check(user_msg, self._meta_tools)
        if meta_pre:
            print(f"  [Meta] pre-check issues: {meta_pre}")
        intent_result = self.intent_manager.update(user_msg)
        print(f"  [Intent] {intent_result['current']} (change: {intent_result['change_type']})")
        from backend.memory.memory_manager import compose_context
        memory_text = compose_context(user_id, messages) if user_id else ""
        # 动态任务拆解: 先分析再执行
        plan_steps = self._create_plan(user_msg)
        plan_context = ""
        if plan_steps:
            step_str = " -> ".join([s.get("agent", "?") for s in plan_steps])
            plan_context = "\n[执行计划] 按以下步骤执行: " + step_str
            for s in plan_steps:
                plan_context += "\n  - " + s.get("agent", "?") + ": " + s.get("reason", "")
        system_content = ORCHESTRATOR_PROMPT.format(memory=memory_text or "暂无历史记录")
        if plan_context:
            system_content += plan_context
        intent_section = self.intent_manager.get_prompt_section()
        if intent_section:
            system_content += "\n" + intent_section + "\n"
        # 构建完整上下文：系统提示 + 历史对话 + 当前消息
        orchestrator_msgs = [{"role": "system", "content": system_content}]
        for prev_msg in messages:
            role = prev_msg.get("role", "")
            if role in ("user", "assistant"):
                orchestrator_msgs.append({
                    "role": role,
                    "content": prev_msg.get("content", "")
                })
        # 创建共享工作区
        from backend.agent.blackboard import Blackboard
        blackboard = Blackboard()
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
                    # 并行执行 LLM 返回的多个工具调用
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    with ThreadPoolExecutor(max_workers=3) as executor:
                        futures = {}
                        for tc in msg.tool_calls:
                            fn_name = tc.function.name
                            try:
                                args = json.loads(tc.function.arguments)
                            except:
                                args = {}
                            future = executor.submit(self._execute_sub_agent, fn_name, args, user_id, blackboard)
                            futures[future] = (tc, fn_name)
                        for future in as_completed(futures):
                            tc, fn_name = futures[future]
                            result = future.result()
                            # 对话记忆检索
                            if fn_name == "search_conversation_memory":
                                try:
                                    args = json.loads(tc.function.arguments)
                                    result = search_conversation_tool(args.get("query", ""), user_id)
                                except Exception as e:
                                    result = f"检索失败: {e}"
                            tool_label = {"query_data": "查询数据", "analyze_data": "分析数据", "query_knowledge_base": "查询知识", "search_conversation_memory": "检索对话"}.get(fn_name, fn_name)
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

            # 元认知反思+修正（代码版）
            try:
                def llm_reflect(prompt):
                    r = self.client.chat.completions.create(
                        model=self.model, 
                        messages=[{"role": "system", "content": REFLECTION_PROMPT}, {"role": "user", "content": prompt}],
                        temperature=0, max_tokens=2000
                    )
                    return r.choices[0].message.content or ""
                # 先用反射 prompt 做一轮自我审核
                reflect_result = self.meta.reflect(user_msg, self._meta_tools, reply_text, llm_call_fn=llm_reflect)
                if reflect_result.get("reflection", {}).get("correction_rounds", 0) > 0:
                    reply_text = reflect_result["answer"]
                    print(f"  [Meta] corrected ({reflect_result['reflection']['correction_rounds']} rounds, confidence: {reflect_result['confidence']:.0%})")
            except Exception:
                pass

            # 存储本次对话到向量记忆
            try:
                store_conversation(user_id, user_msg, reply_text)
            except:
                pass

            # 质量评分
            quality_text = ""
            try:
                from backend.prompts.quality import QUALITY_SCORE_PROMPT
                score_msgs = [
                    {"role": "system", "content": QUALITY_SCORE_PROMPT},
                    {"role": "user", "content": "请评分：\n\n" + reply_text}
                ]
                score_resp = self.client.chat.completions.create(
                    model=self.model, messages=score_msgs, temperature=0, max_tokens=200
                )
                score_result = score_resp.choices[0].message.content or ""
                import json as _json
                try:
                    parsed = _json.loads(score_result)
                    if "total" in parsed:
                        quality_text = "\n[质量: 数据{data_score} 完整{completeness_score} 可读{readability_score} 总分{total}]".format(**parsed)
                except:
                    pass
            except Exception:
                pass

            if react_trace:
                trace_text = "\n\n---\n[思考轨迹: " + " > ".join(react_trace) + "]\n"
                return reply_text + quality_text + trace_text
            return reply_text

        except Exception as e:
            print(f"  [Orchestrator] 异常: {e}")
            try:
                return self.data_agent.chat(messages, user_id)
            except:
                return "抱歉，系统暂时无法处理您的请求，请稍后再试。"

    def chat_stream(self, messages, user_id=None, **kwargs):
        """流式聊天"""
        try:
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


