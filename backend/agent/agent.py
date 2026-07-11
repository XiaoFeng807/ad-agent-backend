from backend.agent.llm_provider import ProviderFactory
import time  # 导入OpenAI库（DeepSeek兼容这个格式）
import os, json, re
from dotenv import load_dotenv  # 用来读取.env文件
from backend.memory.memory_manager import add_fact, get_compact_memory
from backend.agent.context_window import optimize_context, estimate_tokens
from backend.observability import record_ai_call, start_trace, add_trace_step, end_trace, record_function_call, record_error
from backend.agent.fault_tolerance import with_retry, safe_execute, validate_result

# 加载.env文件（项目根目录下的）
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from backend.prompts.main_agent import SYSTEM_PROMPT_TPL
from backend.config.config import settings


class Agent:
    """AI助手核心类"""

    def __init__(self, tool_registry=None):
        """初始化：创建AI客户端，保存函数注册表"""
        self.provider = ProviderFactory.create()
        self.client = self.provider
        from dotenv import load_dotenv
        self.model = settings.MODEL
        self.tool_registry = tool_registry  # 保存函数注册表

    def chat(self, messages, user_id=None, **kwargs):
        """核心方法：用户发消息 → AI思考 → 调函数 → 返回结果"""
        # 可观测性已在模块顶部导入
        _trace_id = None
        _user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                _user_msg = m.get("content", "")[:100]
                break
        if user_id and _user_msg:
            _trace_id = start_trace(user_id, _user_msg)
            add_trace_step(_trace_id, "think", "收到用户消息: " + _user_msg)
        
        try:
            # 1. 获取函数菜单
            tools_def = self.tool_registry.get_definitions() if self.tool_registry else []

            # 2. 组装要发给AI的消息
            # 注入长期记忆
            memory_text = get_compact_memory(user_id) if user_id else ""
            task_context = kwargs.get("task_context", "")
            if memory_text or task_context:
                system_content = SYSTEM_PROMPT_TPL.format(
                    memory=memory_text or "暂无历史记录",
                    task_context=task_context or "无"
                )
            else:
                system_content = SYSTEM_PROMPT_TPL.format(
                    memory="暂无历史记录",
                    task_context="无"
                )
            sys_msg = {"role": "system", "content": system_content}
            optimized = optimize_context(messages)
            payload = [sys_msg] + optimized

            # 3. 把消息 + 函数菜单发给DeepSeek
            from backend.agent.fault_tolerance import TIMEOUT_CONFIG
            _ai_start = time.time()
            # 加超时保护
            try:
                resp = self.client.chat.completions.create(
                                        model=self.model,
                                        messages=payload,
                    tools=tools_def if tools_def else None,
                    timeout=TIMEOUT_CONFIG["llm_call"]
                )
            except Exception as llm_err:
                # 第一次失败后重试一次（换简化消息）
                print(f"  [容错] 首次LLM调用失败: {llm_err}, 重试中...")
                # 简化消息再试（去掉部分历史）
                retry_payload = [payload[0]] + payload[-3:] if len(payload) > 4 else payload
                resp = self.client.chat.completions.create(
                    messages=retry_payload,
                    model=self.model,
                    tools=tools_def if tools_def else None,
                    timeout=TIMEOUT_CONFIG["llm_call"]
                )
            record_ai_call(time.time() - _ai_start)

            # 4. 拿到AI的回复
            msg = resp.choices[0].message

            # 5. 判断AI要不要调函数
            if msg.tool_calls:
                # AI要调函数！遍历所有要调的函数
                for tc in msg.tool_calls:
                    fn_name = tc.function.name        # 函数名
                    try:
                        args = json.loads(tc.function.arguments)  # 参数
                    except:
                        args = {}

                    # 从注册表找到实际函数
                    func = self.tool_registry.get_tool(fn_name) if self.tool_registry else None
                    if func:
                        if user_id:
                            args["user_id"] = user_id  # 注入用户ID
                        # 容错执行：重试 + 降级 + 校验
                        success, result = safe_execute(func, fn_name, args, self.tool_registry.function_map if self.tool_registry else None)
                        if not success:
                            # 降级失败，仍放回结果让LLM处理
                            pass

                        # 校验结果
                        valid, msg = validate_result(fn_name, result)
                        if not valid and success:
                            print(f"  [容错] {fn_name} 结果校验不通过: {msg}")
                            # 尽管如此，仍然继续让LLM处理（LLM可以说"数据异常"）
                        
                        # 自动记录到长期记忆
                        if user_id:
                            add_fact(user_id, fn_name, args, result)

                        # 把函数调用和结果放回对话
                        payload.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                        payload.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False)
                        })

                # 6. 把结果发给AI，让它组织语言回复用户
                _ai_start2 = time.time()
                try:
                    final = self.client.chat.completions.create(
                                                model=self.model,
                                                messages=payload,
                        temperature=0,
                        timeout=TIMEOUT_CONFIG["llm_call"]
                    )
                except Exception:
                    # 第二轮调用失败，返回简化回复
                    return self._get_last_tool_result(payload) or "查询完成，但回复生成失败，请稍后再试。"
                record_ai_call(time.time() - _ai_start2)
                return final.choices[0].message.content or ""

            # AI没调函数，直接返回文字回复
            add_trace_step(_trace_id, "observe", "返回结果", f"长度:{len(msg.content or '')}", 0)
            end_trace(_trace_id, "completed")
            return msg.content or ""

        except Exception as e:
            print(f"  [容错] agent.chat 异常: {type(e).__name__}: {e}")
            if _trace_id:
                add_trace_step(_trace_id, "observe", "系统异常", str(e), 0, "error")
                end_trace(_trace_id, "error")
                record_error("agent_error", str(e), "agent.chat异常")
            return f"抱歉，系统暂时无法处理您的请求，请稍后再试。"


    def chat_stream(self, messages, user_id=None, **kwargs):
        """流式版：逐字返回AI的回复，前端实时显示"""
        # 可观测性已在模块顶部导入
        _trace_id = None
        _user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                _user_msg = m.get("content", "")[:100]
                break
        if user_id and _user_msg:
            _trace_id = start_trace(user_id, _user_msg + " [stream]")
            add_trace_step(_trace_id, "think", "流式-收到消息: " + _user_msg)
        
        try:
            from backend.agent.fault_tolerance import TIMEOUT_CONFIG
            tools_def = self.tool_registry.get_definitions() if self.tool_registry else []
            memory_text = get_compact_memory(user_id) if user_id else ""
            # 对话摘要：注入历史对话的关键信息
            from backend.memory.memory_manager import get_conversation_summaries
            conv_summary = get_conversation_summaries(user_id) if user_id else ""
            if conv_summary:
                memory_text = memory_text + "\n【历史对话】\n" + conv_summary
            task_context = kwargs.get("task_context", "")
            system_content = SYSTEM_PROMPT_TPL.format(
                memory=memory_text or "暂无历史记录",
                task_context=task_context or "无"
            )
            sys_msg = {"role": "system", "content": system_content}
            optimized = optimize_context(messages)
            payload = [sys_msg] + optimized

            # 第一轮：检测是否需要调函数
            _llm_t1 = time.time()
            try:
                resp = self.client.chat.completions.create(
                    messages=payload,
                    model=self.model,
                    tools=tools_def if tools_def else None,
                    timeout=TIMEOUT_CONFIG["llm_call"]
                )
                add_trace_step(_trace_id, "think", "LLM推理完成(流式)", f"耗时{round(time.time()-_llm_t1,1)}s", round(time.time()-_llm_t1,1))
            except Exception as llm_err:
                add_trace_step(_trace_id, "think", "LLM首次失败", str(llm_err), 0, "error")
                record_error("llm_error", str(llm_err), "流式首次调用重试")
                retry_payload = [payload[0]] + payload[-3:] if len(payload) > 4 else payload
                resp = self.client.chat.completions.create(
                    messages=retry_payload,
                    model=self.model,
                    tools=tools_def if tools_def else None,
                    timeout=TIMEOUT_CONFIG["llm_call"]
                )
            msg = resp.choices[0].message

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except:
                        args = {}
                    func = self.tool_registry.get_tool(fn_name) if self.tool_registry else None
                    if func:
                        if user_id:
                            args["user_id"] = user_id
                        _fstart = time.time()
                        success, result = safe_execute(func, fn_name, args, self.tool_registry.function_map if self.tool_registry else None)
                        _fdur = time.time() - _fstart
                        record_function_call(fn_name, args, result, _fdur, None if success else "fail", _trace_id)
                        add_trace_step(_trace_id, "act", f"调用{fn_name}", f"耗时{round(_fdur,2)}s", _fdur, "ok" if success else "error")
                        if user_id:
                            add_fact(user_id, fn_name, args, result)
                        payload.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                        payload.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False)
                        })

                # 第二轮：流式输出
                try:
                    stream = self.client.chat.completions.create(
                        messages=payload,
                        model=self.model,
                        stream=True,
                        temperature=0,
                        timeout=TIMEOUT_CONFIG["llm_call"]
                    )
                    _reply_len = 0
                    for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                            _reply_len += len(chunk.choices[0].delta.content)
                            yield chunk.choices[0].delta.content
                    add_trace_step(_trace_id, "observe", "流式回复完成", f"长度:{_reply_len}", 0)
                except Exception as e:
                    record_error("stream_error", str(e), "流式回复生成失败")
                    yield "数据已查询完成，但生成回复时遇到问题，请重试。"
            else:
                try:
                    stream = self.client.chat.completions.create(
                        messages=payload,
                        model=self.model,
                        stream=True,
                        timeout=TIMEOUT_CONFIG["llm_call"]
                    )
                    _reply_len = 0
                    for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                            _reply_len += len(chunk.choices[0].delta.content)
                            yield chunk.choices[0].delta.content
                    add_trace_step(_trace_id, "observe", "直接回复", f"长度:{_reply_len}", 0)
                except Exception as e:
                    yield "正在处理您的请求，请稍后再试。"

            if _trace_id:
                end_trace(_trace_id, "completed")

        except Exception as e:
            print(f"  [容错] chat_stream 异常: {type(e).__name__}: {e}")
            if _trace_id:
                add_trace_step(_trace_id, "observe", "流式异常", str(e), 0, "error")
                end_trace(_trace_id, "error")
                record_error("stream_error", str(e), "chat_stream异常")
            yield f"抱歉，系统暂时无法处理您的请求，请稍后再试。"


    def _get_last_tool_result(self, payload):
        """从payload中提取最后一个工具调用的结果"""
        for msg in reversed(payload):
            if msg.get("role") == "tool":
                return json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
        return None

    def save_conversation(self, user_id, role, content, priority=1):
        """把对话保存到数据库（priority: 0=低, 1=普通, 2=重要）"""
        from backend.database.database import get_db
        conn = get_db()
        conn.execute(
            "INSERT INTO conversations (user_id, role, content, priority) VALUES (?, ?, ?, ?)",
            (user_id, role, content, priority)
        )
        conn.commit()
        conn.close()

    def get_history(self, user_id, max_messages=15):
        """获取历史消息(兼容旧接口)，走 context_window 优化"""
        msgs, _ = self.optimize_context(user_id)
        return msgs

    def optimize_context(self, user_id):
        """获取优化后的上下文: 返回 (messages, summary_text)"""
        from backend.database.database import get_db
        conn = get_db()
        rows = conn.execute(
            "SELECT id, role, content, priority FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (user_id,)
        ).fetchall()
        conn.close()

        if not rows:
            return [], ""

        messages = []
        seen_ids = set()
        for r in reversed(rows):
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            messages.append({
                "role": r["role"],
                "content": r["content"],
                "priority": r["priority"]
            })

        from backend.agent.context_window import optimize_context
        optimized_msgs, summary_text = optimize_context(messages, user_id)
        return optimized_msgs, summary_text
