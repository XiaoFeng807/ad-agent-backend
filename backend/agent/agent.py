from backend.agent.llm_provider import ProviderFactory, create_completion
from backend.agent.request_budget import ContextBudgetExceeded, ExecutionStopped, tool_call_fields
from backend.agent.home_react import scoped_read
import time  # 导入OpenAI库（DeepSeek兼容这个格式）
import os, json, re
from contextlib import closing
from functools import partial
from dotenv import load_dotenv  # 用来读取.env文件
from backend.memory.memory_manager import add_fact, get_compact_memory
from backend.agent.context_window import optimize_context, estimate_tokens
from backend.observability import record_ai_call, start_trace, add_trace_step, end_trace, record_function_call, record_error, trace_tool_call
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
        self.model = ProviderFactory.model_for(self.provider)
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
            optimized_msgs, _ = optimize_context(messages)
            payload = [sys_msg] + optimized_msgs

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


    def chat_stream(self, messages, user_id=None, *, raise_on_error=False,
                    short_term_context=None, budget=None, **kwargs):
        """流式版：逐字返回AI的回复，前端实时显示"""
        # 可观测性已在模块顶部导入
        trace_id = kwargs.get("trace_id")
        _trace_id = None
        _user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                _user_msg = m.get("content", "")[:100]
                break
        if not trace_id and user_id and _user_msg:
            _trace_id = start_trace(user_id, _user_msg + " [stream]")
            add_trace_step(_trace_id, "think", "流式-收到消息: " + _user_msg)
        
        try:
            from backend.agent.fault_tolerance import TIMEOUT_CONFIG
            tools_def = self.tool_registry.get_definitions() if self.tool_registry else []
            memory_text = get_compact_memory(user_id, short_term_context=short_term_context) if user_id else ""
            # 旧入口仍额外注入共享摘要，首页的短期上下文已包含在 compact memory 中。
            if user_id and short_term_context is None:
                from backend.memory.memory_manager import get_conversation_summaries
                conv_summary = get_conversation_summaries(user_id)
                if conv_summary:
                    memory_text = memory_text + "\n【历史对话】\n" + conv_summary
            task_context = kwargs.get("task_context", "")
            system_content = SYSTEM_PROMPT_TPL.format(
                memory=memory_text or "暂无历史记录",
                task_context=task_context or "无"
            )
            sys_msg = {"role": "system", "content": system_content}
            required_system = None
            if budget is None:
                messages, _ = optimize_context(messages)
            elif memory_text:
                required_system = SYSTEM_PROMPT_TPL.format(memory="暂无历史记录", task_context=task_context or "无")
            payload = [sys_msg, *messages]
            complete = partial(create_completion, self.client, budget, model=self.model,
                               role=getattr(self, "name", "agent"), without_memory=required_system,
                               timeout=TIMEOUT_CONFIG["llm_call"])

            # 第一轮：检测是否需要调函数
            _llm_t1 = time.time()
            try:
                resp = complete(
                    step="stream_prepare", messages=payload, tools=tools_def if tools_def else None,
                )
                add_trace_step(_trace_id, "think", "LLM推理完成(流式)", f"耗时{round(time.time()-_llm_t1,1)}s", round(time.time()-_llm_t1,1))
            except (ContextBudgetExceeded, ExecutionStopped):
                raise
            except Exception as llm_err:
                add_trace_step(_trace_id, "think", "LLM首次失败", str(llm_err), 0, "error")
                if not trace_id:
                    record_error("llm_error", str(llm_err), "流式首次调用重试")
                if budget is not None:
                    # Retry with the current turn and all system messages.
                    current = max((i for i, item in enumerate(payload) if item["role"] == "user"), default=0)
                    retry_payload = [item for i, item in enumerate(payload)
                                     if i >= current or item["role"] == "system"]
                else:
                    retry_payload = [payload[0]] + payload[-3:] if len(payload) > 4 else payload
                resp = complete(
                    step="stream_retry", messages=retry_payload, tools=tools_def if tools_def else None,
                )
            msg = resp.choices[0].message
            execution = budget.execution if budget is not None else None

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    call_id, fn_name, arguments = tool_call_fields(tc)
                    try:
                        args = json.loads(arguments)
                    except:
                        args = {}
                    func = self.tool_registry.get_tool(fn_name) if self.tool_registry else None
                    if func or execution is not None:
                        if execution is not None:
                            with trace_tool_call(trace_id, getattr(self, "name", "agent"),
                                                 fn_name if func else "unknown_tool", call_id) as step:
                                result = scoped_read(fn_name, arguments, func, user_id, execution, step)
                            payload.extend([
                                {"role": "assistant", "content": None, "tool_calls": [tc]},
                                {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)},
                            ])
                            continue
                        if user_id:
                            args["user_id"] = user_id
                        _fstart = time.time()
                        with trace_tool_call(trace_id, getattr(self, "name", "agent"), fn_name, call_id) as step:
                            success, result = safe_execute(func, fn_name, args, self.tool_registry.function_map if self.tool_registry else None)
                            if not success or (isinstance(result, dict) and result.get("success") is False):
                                step["error_code"] = "tool_reported_failure"
                        _fdur = time.time() - _fstart
                        if not trace_id:
                            record_function_call(fn_name, args, result, _fdur, None if success else "fail", _trace_id)
                        add_trace_step(_trace_id, "act", f"调用{fn_name}", f"耗时{round(_fdur,2)}s", _fdur, "ok" if success else "error")
                        if user_id:
                            add_fact(user_id, fn_name, args, result)
                        payload.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                        payload.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(result, ensure_ascii=False)
                        })

                # 第二轮：流式输出
                try:
                    stream = complete(
                        step="stream_reply", messages=payload, stream=True, temperature=0,
                    )
                    _reply_len = 0
                    for chunk in stream:
                        if execution is not None and (code := execution.stop_code()):
                            raise ExecutionStopped(code)
                        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                            _reply_len += len(chunk.choices[0].delta.content)
                            yield chunk.choices[0].delta.content
                    add_trace_step(_trace_id, "observe", "流式回复完成", f"长度:{_reply_len}", 0)
                except (ContextBudgetExceeded, ExecutionStopped):
                    raise
                except Exception as e:
                    if raise_on_error:
                        raise
                    if not trace_id:
                        record_error("stream_error", str(e), "流式回复生成失败")
                    yield "数据已查询完成，但生成回复时遇到问题，请重试。"
            else:
                try:
                    stream = complete(
                        step="stream_reply", messages=payload, stream=True,
                    )
                    _reply_len = 0
                    for chunk in stream:
                        if execution is not None and (code := execution.stop_code()):
                            raise ExecutionStopped(code)
                        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                            _reply_len += len(chunk.choices[0].delta.content)
                            yield chunk.choices[0].delta.content
                    add_trace_step(_trace_id, "observe", "直接回复", f"长度:{_reply_len}", 0)
                except (ContextBudgetExceeded, ExecutionStopped):
                    raise
                except Exception:
                    if raise_on_error:
                        raise
                    yield "正在处理您的请求，请稍后再试。"

            if _trace_id:
                end_trace(_trace_id, "completed")

        except (ContextBudgetExceeded, ExecutionStopped):
            raise
        except Exception as e:
            if raise_on_error:
                raise
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

    def save_conversation(self, user_id, role, content, priority=1, *, chat_view_id=None, user_message_id=None):
        """保存对话并返回提交后的消息 ID（priority: 0=低, 1=普通, 2=重要）"""
        from backend.database.chat_storage import save_message

        return save_message(user_id, role, content, priority, chat_view_id, user_message_id=user_message_id)

    def get_history(self, user_id, max_messages=15):
        """获取历史消息(兼容旧接口)，走 context_window 优化"""
        msgs, _ = self.optimize_context(user_id)
        return msgs

    def optimize_context(self, user_id, *, through_message_id=None, exclude_chat_view_id=None):
        """获取优化后的上下文: 返回 (messages, summary_text)"""
        from backend.database.chat_storage import connect, read_messages

        with closing(connect(read_only=True)) as conn:
            messages = read_messages(conn, user_id, through=through_message_id,
                                     exclude_chat_view_id=exclude_chat_view_id)
        if not messages:
            return [], ""

        if through_message_id is not None:
            # Homepage SSE trims complete turns at the final Provider boundary.
            return messages, ""
        from backend.agent.context_window import optimize_context
        optimized_msgs, summary_text = optimize_context(messages, user_id)
        return optimized_msgs, summary_text
