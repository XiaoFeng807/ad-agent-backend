lines = open("backend/prompts/data_agent.py", "r", encoding="utf-8").readlines()

# 找到 {task_context}""" 这行（prompt 结尾）
for i, line in enumerate(lines):
    if '{task_context}"""' in line:
        # 截断到 """ 前面，插入新内容，再加上 """
        lines[i] = line.replace('"""', '')
        new_rules = [
            '\n## 重要规则\n',
            '- 用户问"今天数据" → 直接返回最新数据，不要说"查不到"\n',
            '- 不要说"系统同步延迟、数据可能不全"等话\n',
            '- 数据都是真实的，直接展示\n',
            '\n## 回答示例\n',
            '用户：今天数据怎么样？\n',
            '回答：今天花费851元，销售额达到3380元，ROAS为3.97，整体表现不错。\n',
        ]
        # 插入新行
        for j, new_line in enumerate(new_rules):
            lines.insert(i + 1 + j, new_line)
        # 最后加回 """
        lines.insert(i + 1 + len(new_rules) + 1, '"""')
        break

open("backend/prompts/data_agent.py", "w", encoding="utf-8").write("".join(lines))

# 验证
compile(open("backend/prompts/data_agent.py", "r", encoding="utf-8").read(), "da.py", "exec")
print("Fixed data_agent.py")

# 同样检查 orchestrator.py
with open("backend/prompts/orchestrator.py", "r", encoding="utf-8") as f:
    content = f.read()
if '{memory}"""' in content:
    content = content.replace('{memory}"""', '{memory}\n\n## 硬性规则\n- 绝对不要说"数据没拉出来、系统同步延迟"\n- 函数返回的数据就是真实的，直接用来回答\n- 自信回答："数据显示..."\n"""')
    open("backend/prompts/orchestrator.py", "w", encoding="utf-8").write(content)
    compile(open("backend/prompts/orchestrator.py", "r", encoding="utf-8").read(), "orc.py", "exec")
    print("Fixed orchestrator.py")
