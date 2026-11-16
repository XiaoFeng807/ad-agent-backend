"""Auto-extracted from multi_agent.py"""

import json, os, re
from datetime import datetime
from functools import partial
from backend.agent.agent import Agent
from backend.agent.diagnosis import DIAGNOSIS_TOOLS, diagnosis_tool_error
from backend.agent.llm_provider import create_completion
from backend.agent.home_react import scoped_read
from backend.agent.request_budget import ContextBudgetExceeded, tool_call_fields
from backend.agent.tool_registry import ToolRegistry
from backend.config.config import settings
from backend.database import mysql_diagnosis
from backend.observability import trace_tool_call
from backend.prompts.data_agent import DATA_AGENT_PROMPT
from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT
from backend.tools.daily_reports import query_daily_reports
from backend.tools.plan_metrics import query_plan_metrics
from backend.tools.tools import query_plan_detail


MYSQL_DIAGNOSIS_READS = {
    "get_daily_report_by_date": query_daily_reports,
    "get_plan_metrics_by_date": query_plan_metrics,
    "get_plan_detail": query_plan_detail,
}

class SubAgent(Agent):
    def __init__(self, name, system_prompt, tool_names):
        registry = ToolRegistry(tool_names)
        super().__init__(tool_registry=registry)
        self.name = name
        self.system_prompt = system_prompt
        self.peers = {}
    def call_peer(self, peer_name, question, user_id=None, *, trace_id=None, short_term_context=None,
                  diagnosis=None, dispatch_call_id=None, parent_call_id=None, budget=None):
        if peer_name not in self.peers:
            return "Agent " + peer_name + " not found, available: " + str(list(self.peers.keys()))
        peer = self.peers[peer_name]
        return peer.chat(
            [{"role": "user", "content": question}], user_id, trace_id=trace_id,
            short_term_context=short_term_context, diagnosis=diagnosis,
            dispatch_call_id=dispatch_call_id, parent_call_id=parent_call_id, budget=budget,
        )

    def _execute_diagnosis_call(self, call, allowed_tools, user_id, short_term_context, diagnosis,
                                dispatch_call_id, parent_call_id, budget):
        args, error = diagnosis.admit(call, self.name, allowed_tools, peers=self.peers,
                                      parent_call_id=parent_call_id)
        if error:
            return error
        call_id, name, _ = tool_call_fields(call)
        runtime_call_id = diagnosis.next_runtime_call_id()
        if name == "call_peer":
            with trace_tool_call(diagnosis.trace_id, self.name, name, call_id,
                                 runtime_call_id=runtime_call_id, parent_call_id=parent_call_id) as step:
                try:
                    return self.call_peer(
                        **args, user_id=user_id, trace_id=diagnosis.trace_id,
                        short_term_context=short_term_context, diagnosis=diagnosis,
                        dispatch_call_id=dispatch_call_id, parent_call_id=runtime_call_id, budget=budget,
                    )
                except ContextBudgetExceeded:
                    raise
                except Exception:
                    step["error_code"] = "tool_exception"
                    return {"success": False, "error_code": step["error_code"]}
        return self.execute_diagnosis_tool(
            name, args, user_id, diagnosis, runtime_call_id=runtime_call_id, tool_call_id=call_id,
            dispatch_call_id=dispatch_call_id, parent_call_id=parent_call_id,
        )

    def execute_diagnosis_tool(self, name, args, user_id, diagnosis, *,
                               runtime_call_id, dispatch_call_id, parent_call_id, tool_call_id=None, retry_of=None):
        with trace_tool_call(diagnosis.trace_id, self.name, name, tool_call_id,
                             runtime_call_id=runtime_call_id, parent_call_id=parent_call_id, retry_of=retry_of) as step:
            try:
                func = self.tool_registry.get_tool(name)
                scoped_args = args if name == "search_knowledge" else {**args, "user_id": user_id}
                if settings.DIAGNOSIS_MYSQL_ENABLED and name in MYSQL_DIAGNOSIS_READS:
                    func = partial(MYSQL_DIAGNOSIS_READS[name], connect=partial(mysql_diagnosis.connect, read_only=True))
                result = func(**scoped_args)
            except Exception as exc:
                result = {"success": False, "error_code": diagnosis_tool_error(exc)}
            step["error_code"] = diagnosis.record_receipt(
                name, args, result, runtime_call_id=runtime_call_id, tool_call_id=tool_call_id,
                parent_call_id=parent_call_id,
                dispatch_call_id=dispatch_call_id, retry_of=retry_of,
            )
            return result

    def chat(self, messages, user_id=None, *, short_term_context=None, diagnosis=None,
             dispatch_call_id=None, parent_call_id=None, budget=None, **kwargs):
        from backend.memory.memory_manager import get_compact_memory
        task_context = kwargs.get("task_context", "")
        if diagnosis:
            memory_text = short_term_context
            task_context = "按本轮诊断参数执行"
        else:
            memory_text = get_compact_memory(user_id, short_term_context=short_term_context) if user_id else ""
        blackboard = kwargs.get("blackboard")
        trace_id = kwargs.get("trace_id")
        if diagnosis:
            base_prompt = diagnosis.snapshot.prompt(role=self.name)
            system_prompt = base_prompt
            if memory_text:
                system_prompt += "\n【本任务已提交上下文，仅供参考】\n" + memory_text
        else:
            base_prompt = self.system_prompt.format(memory="暂无历史记录", task_context=task_context or "无")
            system_prompt = self.system_prompt.format(
                memory=memory_text or "暂无历史记录",
                task_context=task_context or "无"
            )
        suffix = ""
        if self.peers and diagnosis is None:
            peer_info = "\n[可调用的同事] 你可以直接问以下同事获取信息:\n"
            for name, peer in self.peers.items():
                peer_info += "- " + name + ": " + peer.name + "\n"
            peer_info += "调用方式: 使用 call_peer 工具，传入同事名称和你的问题"
            suffix += peer_info
        # 注入 Blackboard 共享上下文
        if blackboard:
            board_summary = blackboard.summary_for_prompt()
            if board_summary:
                suffix += "\n\n" + board_summary
        sys_msg = {"role": "system", "content": system_prompt + suffix}
        required_system = base_prompt + suffix if memory_text else None
        if budget is None:
            from backend.agent.context_window import optimize_context
            messages, _ = optimize_context(messages)
        payload = [sys_msg, *messages]
        base_tools = self.tool_registry.get_definitions() if self.tool_registry else []
        if diagnosis:
            base_tools = [tool for tool in base_tools if tool["function"]["name"] in DIAGNOSIS_TOOLS]
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
        complete = partial(create_completion, self.client, budget, model=self.model,
                           role=self.name, without_memory=required_system)
        resp = complete(
            step="sub_agent", messages=payload,
            tools=all_tools if all_tools else None
        )
        msg = resp.choices[0].message
        execution = budget.execution if budget is not None else None
        if msg.tool_calls:
            from backend.memory.memory_manager import add_fact
            if diagnosis:
                allowed_tools = {tool["function"]["name"] for tool in all_tools}
            for tc in msg.tool_calls:
                call_id, fn_name, arguments = tool_call_fields(tc)
                if diagnosis:
                    result = self._execute_diagnosis_call(
                        tc, allowed_tools, user_id, short_term_context, diagnosis, dispatch_call_id, parent_call_id,
                        budget,
                    )
                    payload.extend([
                        {"role": "assistant", "content": None, "tool_calls": [tc]},
                        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)},
                    ])
                    continue
                try:
                    args = json.loads(arguments)
                except:
                    args = {}
                if fn_name == "call_peer":
                    peer_name = args.get("peer_name", "")
                    question = args.get("question", "")
                    with trace_tool_call(trace_id, self.name, fn_name, call_id) as step:
                        if peer_name not in self.peers:
                            step["error_code"] = "unknown_peer"
                        if execution is not None and (args.keys() != {"peer_name", "question"}
                                                      or not isinstance(question, str) or not question.strip()):
                            step["error_code"] = "invalid_tool_arguments"
                            result = {"success": False, "error_code": step["error_code"]}
                        else:
                            result = self.call_peer(
                                peer_name, question, user_id, trace_id=trace_id,
                                short_term_context=short_term_context, budget=budget,
                            )
                else:
                    func = self.tool_registry.get_tool(fn_name)
                    if execution is not None:
                        with trace_tool_call(trace_id, self.name, fn_name if func else "unknown_tool", call_id) as step:
                            result = scoped_read(fn_name, arguments, func, user_id, execution, step, legacy=True)
                    elif func:
                        with trace_tool_call(trace_id, self.name, fn_name, call_id) as step:
                            if user_id:
                                import inspect
                                sig = inspect.signature(func)
                                if "user_id" in sig.parameters:
                                    args["user_id"] = user_id
                            result = func(**args)
                            if isinstance(result, dict) and result.get("success") is False:
                                step["error_code"] = "tool_reported_failure"
                        if user_id:
                            add_fact(user_id, fn_name, args, result)
                    else:
                        result = "工具 " + fn_name + " 不存在"
                payload.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                payload.append({
                    "role": "tool", "tool_call_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False)
                })
            final = complete(
                step="sub_agent_summary", messages=payload, temperature=0
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

