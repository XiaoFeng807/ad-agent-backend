# 智能广告投放助手

> 基于 FastAPI + DeepSeek API 的多 Agent 广告投放管理平台，支持数据看板、AI 智能分析、RAG 知识库、多账户权限管理。

## ✨ 核心功能

### 🤖 多 Agent AI 助手
- **Orchestrator 架构**：总指挥 Agent 调度 3 个子 Agent（数据查询 / 数据分析 / 知识建议）
- **ReAct 循环**：思考→行动→观察，最多 5 轮迭代
- **自我反思**：AI 自动审核回答质量并修正
- **质量评分**：从数据支撑、完整性、可读性三维度评分
- **安全拦截**：敏感操作检测 + 注入攻击检测

### 📊 数据管理
- 仪表盘核心指标（花费、营收、ROAS、CPC、点击量）
- 每日趋势图、计划表现对比
- 预算管理（充值、调整）
- 告警中心、操作日志

### 🧠 RAG 知识库
- 基于 ChromaDB 的语义向量检索
- 知识库涵盖广告投放核心概念（ROAS、CPC、CTR 等）
- 向量检索失败时自动降级为 TF-IDF 检索

### 🔄 三种 LLM Provider 一键切换
| Provider | 需要 API Key | 说明 |
|----------|-------------|------|
| deepseek | ✅ | 真实调用 DeepSeek API（推荐） |
| mock | ❌ | 模拟回复，无需 API Key，适合演示 |
| openai | ✅ | 调用 OpenAI GPT |

> **自动降级**：API Key 无效或网络异常时自动切换为 mock 模式。

### 🔐 权限系统
| 功能 | boss | admin | user |
|------|------|-------|------|
| 仪表盘/计划/预算/告警 | ✅ | ✅ | ✅ |
| AI 投放助手 | ✅ | ✅ | ✅ |
| 账户管理 | ✅ | ✅ | ❌ |
| 操作日志 | 全部 | admin+user | 仅自己 |
| 创建用户 | 全部角色 | 仅 user | ❌ |

---

## 🚀 快速开始

### 环境要求
- Python 3.10+
- 依赖安装

```bash
pip install fastapi uvicorn pyjwt pillow python-dotenv openai chromadb pydantic
```

### 启动

```bash
# 1. 配置 API Key（可选，不配会自动走 mock 模式）
#    编辑 .env 文件，填入：
#    API_KEY=sk-你的DeepSeek-API-Key
#    LLM_PROVIDER=deepseek

# 2. 启动服务
python fast_server.py

# 3. 打开浏览器
#    访问 http://localhost:5001（端口自动递增）
```

### 测试账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| boss | admin123 | 老板 |
| admin | admin123 | 管理员 |
| zhangsan | user123 | 用户 |
| lisi | user123 | 用户 |

---

## 🏗️ 项目架构

```
ad_agent_backend/
├── fast_server.py                 # 服务入口（FastAPI）
├── server.py                      # 旧版 Flask 入口（备用）
├── .env                           # 环境配置
├── ad_agent.db                    # SQLite 数据库
│
├── backend/
│   ├── agent/                     # Agent 核心
│   │   ├── multi_agent.py         # Orchestrator + ReAct 循环 + 反思 + 评分
│   │   ├── agent.py               # Agent 基类
│   │   ├── llm_provider.py        # LLM Provider（deepseek/mock/openai）
│   │   ├── context_window.py      # 上下文窗口管理
│   │   └── fault_tolerance.py     # 容错机制
│   │
│   ├── prompts/                   # 模块化提示词
│   │   ├── orchestrator.py        # 总指挥提示词
│   │   ├── data_agent.py          # 数据助手提示词
│   │   ├── analysis_agent.py      # 分析助手提示词
│   │   ├── knowledge_agent.py     # 知识助手提示词
│   │   ├── reflection.py          # 自我反思提示词
│   │   └── quality.py             # 质量评分提示词
│   │
│   ├── tools/tools.py             # 26 个工具函数
│   ├── rag/                       # RAG 知识库（ChromaDB）
│   ├── memory/                    # 长期记忆管理
│   ├── routes/                    # 12 个模块化路由
│   ├── mock_ad_data/              # 模拟数据生成
│   ├── static/index.html          # 前端页面
│   └── database/database.py       # 数据库初始化
│
└── data/
    └── rag_db/                    # ChromaDB 向量存储
```

---

## 🧠 AI 助手工作机制

```
用户提问
    │
    ▼
┌─────────────────────────────────────────────┐
│           安全检测（敏感/注入）                │
└────────────────┬────────────────────────────┘
                 │ 通过
                 ▼
┌─────────────────────────────────────────────┐
│       OrchestratorAgent（总指挥）             │
│                                             │
│   ┌────────── ReAct 循环（最多5轮）─────────┐ │
│   │  思考(Think) → 行动(Act) → 观察(Observe)│ │
│   └────────────────────────────────────────┘ │
│                                             │
│   ┌────────── 调用子 Agent ────────────────┐ │
│   │  query_data  →  数据查询助手           │ │
│   │  analyze_data →  数据分析助手           │ │
│   │  query_knowledge →  知识助手            │ │
│   └────────────────────────────────────────┘ │
│                                             │
│   ① 自我反思 → LLM 审核并修正回答            │
│   ② 质量评分 → 数据支撑/完整性/可读性         │
└─────────────────────────────────────────────┘
                 │
                 ▼
           返回自然语言回复
```

### 对话方式
| 方式 | 端点 | 说明 |
|------|------|------|
| SSE 流式 | `POST /api/chat/stream` | 前端轮询，兼容性好 |
| WebSocket | `ws://host/ws/chat` | 实时双向通信 |

---

## 📡 API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` | 登录获取 Token |
| POST | `/api/chat/stream` | AI 流式对话（SSE） |
| WS | `/ws/chat` | AI 实时对话（WebSocket） |
| GET | `/api/dashboard` | 仪表盘核心指标 |
| GET | `/api/daily_reports` | 每日报告 |
| GET | `/api/plans` | 广告计划 |
| POST | `/api/plans/{id}/toggle` | 暂停/启用计划 |
| GET | `/api/accounts` | 账户预算 |
| POST | `/api/accounts/recharge` | 充值 |
| GET | `/api/alerts` | 告警列表 |
| GET | `/api/materials` | 素材管理 |
| GET | `/api/decisions` | AI 决策记录 |
| GET | `/api/logs` | 操作日志 |
| GET | `/api/chat/history` | 聊天历史 |
| GET | `/api/captcha` | 验证码 |
| GET | `/api/health` | 健康检查 |

> 启动后访问 `http://localhost:5001/docs` 查看 Swagger 完整文档。

---

## 🧪 测试

```bash
python run_tests.py
```

覆盖 23 个测试用例：
- Agent 初始化、工具调用、安全拦截（10 个）
- 工具函数、数据查询、预算管理（13 个）

---

## 🐳 Docker 部署

```bash
# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f
```

详见 [DEPLOY.md](./DEPLOY.md) 完整部署文档。

---

## 🛠️ 技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 前端 | 原生 HTML + CSS + JavaScript |
| AI 接口 | DeepSeek API / OpenAI |
| Agent 架构 | Orchestrator + ReAct + 多 Agent 协同 |
| 向量数据库 | ChromaDB |
| RAG 检索 | 语义向量检索 + TF-IDF 回退 |
| 数据库 | SQLite |
| 认证 | JWT (PyJWT) |
| 验证码 | PIL/Pillow |
| 图表 | ECharts |
| 部署 | Docker / docker-compose |

---

## 📄 许可

MIT License
