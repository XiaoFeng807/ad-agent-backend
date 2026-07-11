# -*- coding: utf-8 -*-
"""多 Agent 协同系统 — Orchestrator + ReAct + 自我反思"""

import json, os, re
from datetime import datetime
from backend.agent.agent import Agent
from backend.agent.llm_provider import ProviderFactory
from backend.prompts.data_agent import DATA_AGENT_PROMPT
from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT
from backend.prompts.orchestrator import ORCHESTRATOR_PROMPT
from backend.prompts.reflection import REFLECTION_PROMPT
from backend.agent.metacognition import get_metacognition
from backend.agent.intent_detector import get_intent_manager
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
        self.peers = {}
    def call_peer(self, peer_name, question, user_id=None):
        if peer_name not in self.peers:
            return "Agent " + peer_name + " not found, available: " + str(list(self.peers.keys()))
        peer = self.peers[peer_name]
        return peer.chat([{"role": "user", "content": question}], user_id)
    def chat(self, messages, user_id=None, **kwargs):
        from backend.memory.memory_manager import get_compact_memory
        memory_text = get_compact_memory(user_id) if user_id else ""
        task_context = kwargs.get("task_context", "")
        blackboard = kwargs.get("blackboard")
        SYSTEM_PROMPT = self.system_prompt.format(
            memory=memory_text or "暂无历史记录",
            task_context=task_context or "无"
        )
        if self.peers:
            peer_info = "\n[可调用的同事] 你可以直接问以下同事获取信息:\n"
            for name, peer in self.peers.items():
                peer_info += "- " + name + ": " + peer.name + "\n"
            peer_info += "调用方式: 使用 call_peer 工具，传入同事名称和你的问题"
            SYSTEM_PROMPT += peer_info
        # 注入 Blackboard 共享上下文
        if blackboard:
            board_summary = blackboard.summary_for_prompt()
            if board_summary:
                SYSTEM_PROMPT += "\n\n" + board_summary
        sys_msg = {"role": "system", "content": SYSTEM_PROMPT}
        from backend.agent.context_window import optimize_context as get_optimized_context
        optimized, _ = get_optimized_context(messages)
        payload = [sys_msg] + optimized
        base_tools = self.tool_registry.get_definitions() if self.tool_registry else []
        call_peer_tool = {
            "type": "function",
            "function": {
                "name": "call_peer",
                "description": "向另一个同事Agent提问，获取它的专业帮助",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "peer_name": {"type": "string", "description": "同事名称, 如 query_data / analyze_data / query_knowledge_base"},
                        "question": {"type": "string", "description": "你要问的问题"}
                    },
                    "required": ["peer_name", "question"]
                }
            }
        }
        all_tools = base_tools + [call_peer_tool] if self.peers else base_tools
        resp = self.client.chat.completions.create(
            model=self.model, messages=payload,
            tools=all_tools if all_tools else None
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
                if fn_name == "call_peer":
                    peer_name = args.get("peer_name", "")
                    question = args.get("question", "")
                    result = self.call_peer(peer_name, question, user_id)
                else:
                    func = self.tool_registry.get_tool(fn_name)
                    if func:
                        if user_id:
                            import inspect
                            sig = inspect.signature(func)
                            if "user_id" in sig.parameters:
                                args["user_id"] = user_id
                        result = func(**args)
                        if user_id:
                            add_fact(user_id, fn_name, args, result)
                    else:
                        result = "工具 " + fn_name + " 不存在"
                    payload.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    payload.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
            final = self.client.chat.completions.create(
                model=self.model, messages=payload, temperature=0
            )
            result_text = final.choices[0].message.content or ""
            result_text = re.sub(r'<invoke name="[^"]+">.*?</invoke>', '', result_text, flags=re.DOTALL)
            result_text = re.sub(r'<tool_calls>.*?</tool_calls>', '', result_text, flags=re.DOTALL)
            result_text = re.sub(r'<parameter[^>]*>.*?</parameter>', '', result_text, flags=re.DOTALL)
            result_text = re.sub(r'\\n{3,}', '\\n\\n', result_text).strip()
            return result_text
        msg_content = msg.content or ""
        msg_content = re.sub(r'<invoke name="[^"]+">.*?</invoke>', '', msg_content, flags=re.DOTALL)
        msg_content = re.sub(r'<parameter[^>]*>.*?</parameter>', '', msg_content, flags=re.DOTALL)
        msg_content = re.sub(r'\\n{3,}', '\\n\\n', msg_content).strip()
        return msg_content



# ==================== 动态Agent创建 + 竞争机制 ====================

class AgentFactory:
    """Agent工厂：根据任务类型动态创建不同风格的Agent"""

    STYLES = {
        "conservative": {
            "suffix": "谨慎分析，只基于确定的数据说话。没有充分依据的判断不要下结论。"
        },
        "aggressive": {
            "suffix": "大胆分析，即使数据有限也要给出推测方向。敢于下结论，标注置信度。"
        },
        "detail": {
            "suffix": "极度详细，逐条数据展开分析。每个指标都单独说明，不遗漏细节。"
        },
    }

    @staticmethod
    def create_agent(agent_type, style="conservative", peer_agents=None):
        from backend.prompts.data_agent import DATA_AGENT_PROMPT
        from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
        from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT

        suffix = AgentFactory.STYLES.get(style, AgentFactory.STYLES["conservative"])["suffix"]

        if agent_type == "data":
            prompt = DATA_AGENT_PROMPT + "\n\n[风格指令]" + suffix
            tools = ["get_dashboard_data", "get_daily_trend", "get_plans_summary",
                     "get_daily_report_by_date", "get_week_over_week", "get_activity_timeline"]
        elif agent_type == "analysis":
            prompt = ANALYSIS_AGENT_PROMPT + "\n\n[风格指令]" + suffix
            tools = ["compare_plans", "get_week_over_week", "get_decision_summary"]
        elif agent_type == "knowledge":
            prompt = KNOWLEDGE_AGENT_PROMPT + "\n\n[风格指令]" + suffix
            tools = ["search_knowledge", "get_hot_products"]
        else:
            raise ValueError(f"Unknown agent type: {agent_type}")

        agent = SubAgent(f"{agent_type}_{style}", prompt, tools)
        if peer_agents:
            agent.peers = peer_agents
        return agent

    @staticmethod
    def create_competing_agents(agent_type, peer_agents=None):
        agents = []
        for style in ["conservative", "aggressive", "detail"]:
            agent = AgentFactory.create_agent(agent_type, style, peer_agents)
            agents.append(agent)
        return agents


class Competition:
    """竞争机制：多个Agent分别回答，Judge评选最佳答案"""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.results_history = []

    def run(self, query, user_id, agent_type="analysis", peer_agents=None):
        competitors = AgentFactory.create_competing_agents(agent_type, peer_agents)
        task_id = f"comp_{hash(query) % 100000}_{id(query)}"

        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            for agent in competitors:
                future = executor.submit(agent.chat, [{"role": "user", "content": query}], user_id)
                futures[future] = agent

            for future in as_completed(futures):
                agent = futures[future]
                try:
                    result = future.result()
                    style = agent.name.split("_")[-1]
                    results.append({"agent": agent.name, "style": style, "result": result})
                except Exception as e:
                    results.append({"agent": agent.name, "style": "error", "result": str(e)})

        best = self._judge(query, results)
        self.results_history.append({"task_id": task_id, "query": query[:30], "winner": best.get("style", "?")})
        return best

    def _judge(self, query, results):
        if not results:
            return {"answer": "所有Agent都未能生成回答", "winner": "none", "style": "none"}
        if len(results) == 1:
            return {"answer": results[0]["result"], "winner": results[0]["agent"],
                    "style": results[0]["style"], "reason": "only_candidate"}

        candidates = "\n\n".join([
            f"候选人{i+1}({r["style"]}):\n{r["result"][:800]}" for i, r in enumerate(results)
        ])

        judge_prompt = ("用户问题: " + query + "\n\n"
            + "以下是多个Agent的答案，请评选最佳。标准: 准确性>完整性>可读性\n\n"
            + candidates
            + "\n\n只输出JSON: {\"winner\": \"conservative/aggressive/detail\", \"reason\": \"理由\", \"best_index\": 0}")

        try:
            resp = self.orchestrator.client.chat.completions.create(
                model=self.orchestrator.model,
                messages=[{"role": "user", "content": judge_prompt}],
                temperature=0.1, max_tokens=500
            )
            reply = resp.choices[0].message.content or ""
            import json as _json
            if "```json" in reply:
                reply = reply.split("```json")[1].split("```")[0]
            elif "```" in reply:
                reply = reply.split("```")[1].split("```")[0]

            judge_result = _json.loads(reply.strip())
            winner_style = judge_result.get("winner", "conservative")
            for r in results:
                if r["style"] == winner_style:
                    return {"answer": r["result"], "winner": r["agent"],
                            "style": winner_style, "reason": judge_result.get("reason", "")}
        except:
            pass

        return {"answer": results[0]["result"], "winner": results[0]["agent"],
                "style": results[0]["style"], "reason": "judge_fallback"}

    def stats(self):
        return {"total_competitions": len(self.results_history)}


class OrchestratorAgent:
    def __init__(self):
        self.provider = ProviderFactory.create()
        self.model = os.getenv("MODEL", "deepseek-chat")
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
        from backend.memory.memory_manager import get_compact_memory
        memory_text = get_compact_memory(user_id) if user_id else ""
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



class AgentPool:
    """Agent 连接池：支持并发请求，避免单实例阻塞"""
    
    def __init__(self, min_size=2, max_size=10):
        self._min_size = min_size
        self._max_size = max_size
        self._pool = []
        self._in_use = set()
        self._lock = __import__("threading").Lock()
        self._init_pool()
    
    def _init_pool(self):
        for _ in range(self._min_size):
            agent = PipelineCoordinator(OrchestratorAgent())
            self._pool.append(agent)
    
    def acquire(self):
        """从池中获取一个可用 agent"""
        import time
        start = time.time()
        while time.time() - start < 10:  # 最多等 10 秒
            with self._lock:
                # 找空闲的
                for agent in self._pool:
                    if id(agent) not in self._in_use:
                        self._in_use.add(id(agent))
                        return agent
                # 没空闲但没超上限，创建新的
                if len(self._pool) < self._max_size:
                    agent = PipelineCoordinator(OrchestratorAgent())
                    self._pool.append(agent)
                    self._in_use.add(id(agent))
                    return agent
            # 池满了，等一会再试
            time.sleep(0.1)
        raise TimeoutError("Agent 池已满，请稍后再试")
    
    def release(self, agent):
        """释放 agent 回池中"""
        with self._lock:
            self._in_use.discard(id(agent))
    
    @property
    def stats(self):
        """池状态"""
        with self._lock:
            return {
                "total": len(self._pool),
                "in_use": len(self._in_use),
                "available": len(self._pool) - len(self._in_use),
                "max_size": self._max_size
            }
