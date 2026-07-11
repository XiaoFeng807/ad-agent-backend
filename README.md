# 智能广告投放助手

> 基于 FastAPI + DeepSeek API 的多 Agent 广告投放管理平台
> 支持数据看板、AI 智能分析、RAG 知识库、多账户权限管理

## 核心功能

### 多 Agent AI 助手
- **Orchestrator 架构**：总指挥 Agent 调度 3 个子 Agent
- **ReAct 循环**：思考→行动→观察，最多 5 轮迭代
- **结果融合**：保守/激进/详细 三种风格合并输出
- **竞争机制**：复杂问题启动多 Agent 竞争，简单问题直接回答
- **Agent 间通信**：通过 call_peer 直接对话
- **共享工作区 (Blackboard)**：所有 Agent 读写共享上下文
- **安全拦截**：敏感操作检测 + 注入攻击检测

### RAG 知识库
- **混合搜索**：向量检索(ChromaDB) + BM25 关键词 + RRF 融合
- **中文分词**：广告领域词典 + 前向最大匹配
- **中文 Embedding**：自研 ChineseEmbedding（jieba + TF-IDF）
- **因果检索**：检测因果问题，多路展开（原因+数据+建议）
- **重排序**：检索后按相关性精排

### 上下文管理
- **四层记忆**：滑动窗口 + 短期摘要 + 长期画像 + 业务记忆
- **重要性裁剪**：按 1-10 分保留重要对话，非纯时间窗口
- **时间衰减**：自动清理过期记忆（30min/30天/90天）
- **检索式记忆**：LLM 按需调用工具检索历史对话

### 数据看板
- 仪表盘：ROAS/花费/营收趋势图
- 广告计划管理：多账户多平台管理
- 告警中心：ROAS 异常/预算超支预警
- AI 对话：自然语言查询数据

## 快速启动

```bash
pip install -r requirements.txt
cp .env.example .env  # 填写 API_KEY
python fast_server.py
```

访问 http://localhost:5010

默认账号：boss/admin123 | admin/admin123 | zhangsan/user123

## 技术栈

| 技术 | 用途 |
|------|------|
| FastAPI | Web 框架 |
| DeepSeek API | LLM 服务 |
| ChromaDB | 向量数据库 |
| SQLite | 业务数据库 |
| ECharts | 数据可视化 |