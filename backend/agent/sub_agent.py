"""Auto-extracted from multi_agent.py"""

import json, os, re
from datetime import datetime
from backend.agent.agent import Agent
from backend.agent.tool_registry import ToolRegistry
from backend.prompts.data_agent import DATA_AGENT_PROMPT
from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT

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

