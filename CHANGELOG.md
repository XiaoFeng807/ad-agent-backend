# 变更日志

> 本项目所有重要变更均记录在此文件中。

## [8.0.0] - 2026-07-03

### 新增
- Chroma 向量检索 + RAG 知识库（ad_knowledge.json，12条广告行业知识）
- 双引擎自动切换：优先 Chroma 语义检索，不可用时降级 TF-IDF
- search_knowledge 函数注册到 tools.py，AI 可查询知识库
- ARCHITECTURE.md 架构文档

### 基础设施
- 安装 chromadb 1.5.9，下载 ONNX 语义嵌入模型（79.3MB）
- 新建 backend/knowledge_base/ 和 backend/chroma_db/ 目录

## [7.0.0] - 2026-07-01

### 新增
- 树状记忆系统（backend/memory/memory_manager.py）
- 4个记忆分支：平台账户、最近操作、关注偏好、告警与问题
- 自动学习用户偏好（累计3次触发推导）
- 记忆注入系统提示词

## [6.0.0] - 2026-07-01

### 修复
- 仪表盘 500 错误
- AI 搜索趋势"数据不足"（替换为百度搜索估算）
- 标记已读变全部已读（增加 alert_id 字段兼容）

### 新增
- 单元测试（tests/ 目录，3个文件14个用例）
- 后端返回 total_orders 和 cpa 字段，修复 ¥undefined

## [5.0.0] - 2026-06-29

### 重构
- 项目解耦模块化：server.py 从356行瘦身到60行
- 创建 backend/routes/ 目录，11个独立蓝图文件
- 通过 routes/shared.py 管理共享状态

### 修复
- 修复循环导入（蓝图移到独立 blueprints.py）
- 禁用 Flask 重载器防止请求500错误

## [4.0.0] - 2026-06-28

### 修复
- gen_all.py 中 rechargeAccount 函数的 `\n` 被转义问题

## [3.0.0] - 2026-06-27

### 修复
- ECharts CDN 阻塞页面加载
- 验证码改用 fetch + blob URL 加载

## [2.0.0] - 2026-06-25

### 新增
- AI 投放助手（DeepSeek API + Function Calling）
- 24 个函数工具注册
- 预算管理、告警中心、素材管理功能

## [1.0.0] - 2026-06-24

### 新增
- 项目初始化
- 登录 + 验证码
- 仪表盘数据看板
- 基础权限系统（boss/admin/user）
- 模拟数据生成

## [9.0.0] - 2026-07-03

### 新增
- **流式响应（Streaming）**：AI回复逐字显示，前端无需等待完整回复
- chat_stream() 方法：agent.py 新增流式聊天方法，调用 DeepSeek stream=True
- /api/chat/stream 端点：chat_routes.py 新增 SSE 端点，逐块推送回复
- **前端实时显示**：sendChat 改为 fetch + ReadableStream，边接收边渲染

### 文档规范
- CHANGELOG.md：项目变更日志（本文件）
- config/config.yaml.example：配置示例模板
- .env.example：环境变量配置模板

### 测试结构优化
- 测试放代码旁边：auth/tests/、tools/tests/、routes/tests/
- 更规范的模块级测试组织

## [10.0.0] - 2026-07-03

### 新增
- **API 接口文档**：API.md，列出全部 21 个接口的路径、参数、返回格式
- **意图识别层**：backend/agent/intent_classifier.py
  - 12 个意图分类（query_dashboard/account/plan/alert/trend/report/product/knowledge...）
  - 91 个关键词规则，先规则匹配后 LLM 兜底
  - 注入攻击提前拦截（比之前更彻底）
  - 敏感操作直接拦截（不经过 LLM，更安全）
- **chat_routes.py 升级**：非流式和流式接口都接入意图识别

## [11.0.0] - 2026-07-03

### 新增
- **多轮任务追踪**：backend/agent/task_tracker.py
  - 跨轮跟踪用户意图（started → continued → switched → completed）
  - 9 种任务类型映射（仪表盘/账户/计划/告警/趋势/产品/建议/知识/敏感操作）
  - 自动识别任务延续/切换/完成信号
- **agent.py 升级**：支持 task_context 注入到系统提示词
  - 提示词新增【当前任务】段落，AI 知道用户上一轮在做什么
  - chat() 和 chat_stream() 均支持 task_context 参数
- **chat_routes.py 升级**：非流式/流式接口均接入任务追踪

## [12.0.0] - 2026-07-03

### 新增
- **上下文窗口管理**：backend/agent/context_window.py
  - 用 token 估算替代消息条数限制（中文1.5字/token，英文4字/token）
  - 优先级保留：重要消息（priority≥2）优先保留，低优先级先丢弃
  - 总窗口 32000 token（DeepSeek 上限），安全边距 4000 token
- **agent.py 升级**：移除硬编码 messages[-15:]，改用 get_optimized_context()
  - chat() 和 chat_stream() 均接入
