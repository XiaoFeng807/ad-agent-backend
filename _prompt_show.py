from openai import OpenAI  # 导入OpenAI库（DeepSeek兼容这个格式）
import os, json, re
from dotenv import load_dotenv  # 用来读取.env文件
from backend.memory.memory_manager import add_fact, get_compact_memory

# 加载.env文件（项目根目录下的）
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# ===== 系统提示词：每次对话都发给AI的"岗位说明书" =====
SYSTEM_PROMPT_TPL = """你是智能投放助手，帮助用户管理和分析广告数据。
规则：
1. 回答简洁，用大白话
2. 涉及充值、修改预算、切换计划状态等敏感操作，只给建议，不执行
3. 不暴露内部过程
4. 回答时不要提及工具调用、数据库查询等内部细节
5. 如果数据不足以回答用户问题，直接说"数据不足"，不要编造
6. 只基于函数返回的数据说话，不要自己补充未提供的信息

【长期记忆】（该用户之前的关键操作记录）：
{memory}
"""


class Agent:
    """AI助手核心类"""

    def __init__(self, tool_registry=None):
        """初始化：创建AI客户端，保存函数注册表"""
        self.client = OpenAI(
            api_key=os.getenv("API_KEY"),     # 从.env读API密钥
            base_url=os.getenv("BASE_URL")     # 从.env读API地址
        )
        self.model = os.getenv("MODEL", "deepseek-chat")  # 模型名
        self.tool_registry = tool_registry  # 保存函数注册表

    def chat(self, messages, user_id=None):
        """核心方法：用户发消息 → AI思考 → 调函数 → 返回结果"""
        try:
            # 1. 获取函数菜单（告诉AI它能调哪些函数）
            tools_def = self.tool_registry.get_definitions() if self.tool_registry else []

            # 2. 组装要发给AI的消息
            # 注入长期记忆
            memory_text = get_compact_memory(user_id) if user_id else ""
            if memory_text:
                system_content = SYSTEM_PROMPT_TPL.format(memory=memory_text)
            else:
                system_content = SYSTEM_PROMPT_TPL.format(memory="暂无历史记录")
            sys_msg = {"role": "system", "content": system_content}
            payload = [sys_msg] + messages[-15:]  # 最近15条 + 系统提示词

            # 3. 把消息 + 函数菜单发给DeepSeek
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=payload,
                tools=tools_def if tools_def else None
            )

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
                        result = func(**args)           # 执行函数！

                        # 自动记录到长期记忆
                        if user_id:
                            # 树状记忆：自动分流到不同分支
                            add_fact(user_id, fn_name, args, result)

                        # 把函数调用和结果放回对话
                        payload.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                        payload.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False)
                        })

                # 6. 把结果发给AI，让它组织语言回复用户
                final = self.client.chat.completions.create(
                    model=self.model,
                    messages=payload,
                    temperature=0
                )
                return final.choices[0].message.content or ""

            # AI没调函数，直接返回文字回复
            return msg.content or ""

        except Exception as e:
            # 出错兜底：返回友好提示，不崩
            return "抱歉，我暂时无法处理这个请求。"

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
        """智能截断：优先保留重要对话，再用最近消息补满"""
        from backend.database.database import get_db
        conn = get_db()
        # 1. 先拿最近 30 条
        rows = conn.execute(
            "SELECT id, role, content, priority FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 30",
            (user_id,)
        ).fetchall()
        conn.close()

        if not rows:
            return []

        # 2. 按优先级排序：先保留 priority=2 的重要消息
        important = []
        recent = []
        seen_ids = set()
        for r in reversed(rows):
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            msg = {"role": r["role"], "content": r["content"]}
            if r["priority"] == 2:
                important.append(msg)
            else:
                recent.append(msg)

        # 3. 重要消息全保留，再用最近的普通消息补满 max_messages
        result = important[:]
        remaining = max_messages - len(result)
        if remaining > 0:
            result.extend(recent[-remaining:])

        return result