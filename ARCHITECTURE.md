# 项目架构文档

## 目录结构（更新于 2026-07-11）

```
ad_agent_backend/
├── fast_server.py                 # 主入口（FastAPI）
├── server.py                      # 旧入口（Flask，兼容）
├── scripts/                       # 一次性脚本
├── tests/                         # 测试
├── backend/
│   ├── agent/                     # Agent 核心
│   │   ├── agent.py               # Agent 基类
│   │   ├── sub_agent.py           # 子 Agent
│   │   ├── orchestrator.py        # OrchestratorAgent（总指挥）
│   │   ├── agent_factory.py       # Agent 工厂（保守/激进/详细风格）
│   │   ├── competition.py         # 竞争机制 + 结果融合
│   │   ├── pipeline.py            # Pipeline + PipelineCoordinator
│   │   ├── agent_pool.py          # Agent 连接池
│   │   ├── tool_registry.py       # 工具注册表
│   │   ├── blackboard.py          # 共享工作区
│   │   ├── metacognition.py       # 元认知模块
│   │   ├── intent_detector.py     # 意图检测
│   │   ├── context_window.py      # 上下文窗口管理
│   │   ├── fault_tolerance.py     # 容错机制
│   │   ├── task_tracker.py        # 任务追踪
│   │   └── multi_agent.py         # 兼容入口（重新导出）
│   ├── memory/
│   │   ├── memory_manager.py      # 四层记忆系统
│   │   ├── conversation_memory.py # 对话记忆向量库（ChromaDB）
│   │   └── importance.py          # 重要性评分
│   ├── rag/
│   │   ├── rag_knowledge.py       # RAG 知识库（混合搜索）
│   │   ├── chinese_embedding.py   # 中文 Embedding 函数
│   │   ├── data_cleaner.py        # 数据清洗
│   │   └── causal_reasoning.py    # 因果推理模块
│   ├── config/
│   │   └── config.py              # pydantic-settings 统一配置
│   ├── routes/                    # API 路由
│   ├── prompts/                   # 提示词模板
│   ├── database/                  # 数据库
│   ├── tools/                     # 工具函数
│   ├── auth/                      # 认证
│   └── ...                        # 其他模块
```

## 核心架构

### Agent 系统

```
OrchestratorAgent (总指挥)
  ├── 数据助手 (SubAgent)  → query_data
  ├── 分析助手 (SubAgent)  → analyze_data  
  └── 知识助手 (SubAgent)  → query_knowledge_base

竞争机制:
  3 个 Agent 按保守/激进/详细并发回答 → 结果融合输出

对话记忆:
  LLM 按需调用 search_conversation_memory → 检索历史对话
```

### 请求处理流程

```
请求 → 安全检测 → 元认知预检 → 记忆组装 → 任务拆解 → ReAct 循环 → 自我反思 → 质量评分 → 对话存储 → 响应
```

### RAG 流程

```
query → 中文分词 → 向量检索(ChromaDB+ChineseEmbedding) → BM25 关键词 → RRF 融合 → 重排序 → LLM
因果问题 → 多路展开(原因+数据+建议) → 因果排序 → LLM
```

### 记忆系统

```
L1: 滑动窗口（最近 6 轮，按重要性裁剪）
L2: 短期记忆（30 分钟，JSON 文件）
L3: 长期画像（30 天 TTL，用户偏好/行为）
L4: 业务记忆（90 天 TTL，ChromaDB 向量库）
```