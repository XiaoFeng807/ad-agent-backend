import sys; sys.path.insert(0, ".")
import re

with open("backend/agent/agent.py", "r", encoding="utf-8") as f:
    content = f.read()

# 在 chat() 方法后面插入 chat_stream() 方法
insert_marker = "    def save_conversation(self, user_id, role, content, priority=1):"

stream_method = '''
    def chat_stream(self, messages, user_id=None):
        """流式版：逐字返回AI的回复，前端实时显示"""
        try:
            tools_def = self.tool_registry.get_definitions() if self.tool_registry else []
            memory_text = get_compact_memory(user_id) if user_id else ""
            system_content = SYSTEM_PROMPT_TPL.format(memory=memory_text or "暂无历史记录")
            sys_msg = {"role": "system", "content": system_content}
            payload = [sys_msg] + messages[-15:]

            # 第一轮：检测是否需要调函数（非流式，但很快）
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=payload,
                tools=tools_def if tools_def else None
            )
            msg = resp.choices[0].message

            if msg.tool_calls:
                # 执行函数
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
                        result = func(**args)
                        if user_id:
                            add_fact(user_id, fn_name, args, result)
                        payload.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                        payload.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False)
                        })

                # 第二轮：流式输出AI的回答
                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=payload,
                    stream=True,
                    temperature=0
                )
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
            else:
                # 没调函数，直接流式输出
                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=payload,
                    stream=True
                )
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content

        except Exception as e:
            yield f"抱歉，我暂时无法处理这个请求。"

'''

content = content.replace(insert_marker, stream_method + "\n" + insert_marker)

with open("backend/agent/agent.py", "w", encoding="utf-8") as f:
    f.write(content)

print("agent.py 已更新 - 添加了 chat_stream() 方法")
