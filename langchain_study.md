# LangChain 学习指南（面向面试）

## 一、LangChain 是什么

LangChain 是一个 LLM 应用开发框架，把调用大模型时重复要做的事情——
调 API、写提示词、管理记忆、调工具——封装成标准模块。

**核心思想：** 写 AI 应用不应该每次都从零开始搭 Prompt、Memory、Tool。

---

## 二、六大核心模块

### 1. Model I/O（模型输入输出）

- **ChatModel**：对话模型接口，统一 GPT / DeepSeek / Claude
- **PromptTemplate**：提示词模板，变量动态填充
- **OutputParser**：解析 LLM 返回的结果
**理解重点：** 切换模型只改一行配置。

### 2. Memory（记忆）

- **ConversationBufferMemory**：存最近 N 轮对话原文（短期）
- **ConversationSummaryMemory**：自动总结摘要（长期）
- **VectorStoreRetrieverMemory**：向量检索 + 语义匹配（超长历史）
**理解重点：** 记忆是在有限的 token 里保留最有价值的信息。

### 3. Tools（工具）

Tool 定义三个东西：
- name：函数名（LLM 用来识别）
- description：描述什么时候该调（LLM 靠这个选）
- args_schema：参数结构（LLM 按这个传参）
**理解重点：** Tools 是给 LLM 的一份 JSON 菜单。

### 4. Chains（链）

- **LLMChain**：Prompt -> LLM -> OutputParser
- **SimpleSequentialChain**：A -> B -> C 串行执行
- **RouterChain**：根据输入路由到不同链
**理解重点：** 一个 LLM 做不完的事，拆成多个步骤串起来。

### 5. Agents（代理）

Agent = Chain + Tools + 循环决策
流程：用户输入 -> LLM 判断要不要调工具，要调就执行再回喂 LLM

Agent 类型：
- **ReAct**：思考 -> 行动 -> 观察 -> 再思考（最主流）
- **OpenAI Tools**：原生 Function Calling
- **Plan-and-Execute**：先列计划再逐步执行
**理解重点：** Chain 是固定流水线，Agent 自己决定下一步。

### 6. Callbacks（可观测性）

在每个环节插入钩子，用于日志记录、性能监控、调试追踪。

---

## 三、常见面试问题

### Q1：LangChain 和直接调 API 有什么区别？
直接调 API 要自己管理 prompt、记忆、重试。LangChain 模块化了，但包大、难排查。

### Q2：Agent 和 Chain 的区别？
Chain 是写死的流水线，Agent 是 LLM 自己决定每一步。

### Q3：LangChain 的缺点？
1. 封装太厚，问题难定位
2. 版本不稳定，API 经常变
3. 小项目用 LangChain 反而更麻烦

---

## 四、学习建议

1. 先理解设计思想（六大模块分别做什么）
2. 看官方 Concept Guide，不看 API 文档
3. 用自己项目对照理解
4. agent.py + tools.py + memory_manager.py 就是简化版 LangChain

---

## 五、一句话总结

LangChain = 把调 LLM 时重复要做的事（Prompt、Memory、Tools、Chain）封装成标准模块。