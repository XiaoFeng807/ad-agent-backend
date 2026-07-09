# 部署文档 — 智能广告投放助手

## 环境要求

| 项目 | 最低要求 | 推荐 |
|------|---------|------|
| Python | 3.10+ | 3.12 |
| 内存 | 1GB | 2GB+ |
| 磁盘 | 500MB | 1GB |
| 操作系统 | Windows 10+ / Linux / macOS | - |

## 必要依赖

```bash
pip install fastapi uvicorn pyjwt pillow python-dotenv openai chromadb pydantic
```

## 目录结构

```
ad_agent_backend/
├── fast_server.py              # 主入口（FastAPI，推荐）
├── server.py                   # 旧版 Flask 入口（备用）
├── .env                        # 配置文件（需要自己填）
├── ad_agent.db                # SQLite 数据库（自动生成）
├── DEPLOY.md                   # 本部署文档
├── backend/
│   ├── static/index.html       # 前端页面
│   ├── agent/                  # Agent 核心（多Agent + ReAct + 反思）
│   │   ├── multi_agent.py      # Orchestrator + ReAct 循环
│   │   ├── agent.py            # Agent 基类
│   │   ├── llm_provider.py     # LLM Provider（deepseek/mock/openai）
│   │   ├── context_window.py   # 上下文窗口管理
│   │   └── fault_tolerance.py  # 容错机制
│   ├── prompts/                # 提示词目录（模块化）
│   │   ├── orchestrator.py     # 总指挥提示词
│   │   ├── data_agent.py       # 数据助手提示词
│   │   ├── analysis_agent.py   # 分析助手提示词
│   │   ├── knowledge_agent.py  # 知识助手提示词
│   │   ├── reflection.py       # 自我反思提示词
│   │   └── quality.py          # 质量评分提示词
│   ├── tools/tools.py          # 26 个工具函数
│   ├── rag/                    # RAG 知识库（ChromaDB）
│   ├── memory/                 # 长期记忆管理
│   ├── routes/                 # 路由模块
│   ├── mock_ad_data/           # 模拟数据生成
│   └── database/               # 数据库连接
├── data/
│   └── rag_db/                 # ChromaDB 向量数据库
└── tests/                      # 测试文件
```

---

## 一、快速启动（开发模式）

```bash
# 1. 进入项目目录
cd ad_agent_backend

# 2. 安装依赖（只需运行一次）
pip install fastapi uvicorn pyjwt pillow python-dotenv openai chromadb pydantic

# 3. 配置 .env 文件（参考下文）

# 4. 启动服务（FastAPI 模式，推荐）
python fast_server.py
# 或指定端口
python fast_server.py --port 5050

# 5. 打开浏览器访问
#    http://localhost:5001（端口自动递增，看控制台输出）

# 备用：旧版 Flask 启动
python server.py
```

### 预设测试账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| boss | admin123 | 老板 |
| admin | admin123 | 管理员 |
| zhangsan | user123 | 用户 |
| lisi | user123 | 用户 |

---

## 二、配置文件 (.env)

项目根目录的 `.env` 文件控制所有配置：

```env
# === API 配置 ===
# DeepSeek（推荐，性价比高）
# 在 https://platform.deepseek.com 注册获取
API_KEY=sk-你的API-KEY
BASE_URL=https://api.deepseek.com
MODEL=deepseek-chat

# 或者 OpenAI
# API_KEY=sk-你的OpenAI-KEY
# BASE_URL=https://api.openai.com/v1
# MODEL=gpt-3.5-turbo

# === LLM Provider 切换 ===
# deepseek = 真实调用 DeepSeek API
# mock     = 模拟回复（不需要 API Key，适合演示）
# openai   = 调用 OpenAI API
LLM_PROVIDER=deepseek
```

> **自动降级**：配置了 API Key 就自动用真实调用，没配或调用失败自动降级为 mock 模式。

---

## 三、核心架构

### 多 Agent 系统

```
用户提问
    │
    ▼
┌─────────────────────────────────────────────────┐
│  OrchestratorAgent（总指挥）                      │
│  · ReAct 循环（思考→行动→观察，最多5轮）           │
│  · 自我反思（LLM 审核回答质量）                    │
│  · 质量评分（数据支撑/完整性/可读性）                │
└────────────┬────────────────────────────────────┘
             │ 调用子 Agent
    ┌────────┼────────┬────────┐
    ▼        ▼        ▼        ▼
 数据助手  分析助手  知识助手  安全拦截
```

### 对话方式

| 方式 | 端点 | 说明 |
|------|------|------|
| **SSE 流式** | `POST /api/chat/stream` | 前端轮询，兼容性好 |
| **WebSocket** | `ws://host/ws/chat` | 实时双向通信，推荐 |

### Agent 间通信
```
数据Agent 查完数据发现CPC异常
    → 主动调用 call_peer(analyze_data, "CPC涨了15%，帮我分析原因")
    → 分析Agent 收到请求，分析原因
    → 返回分析结果给 数据Agent
    → 数据Agent 继续组织最终回复
```

### 共享工作区 (Blackboard)
```
所有 Agent 共享的"黑板":
[query_data] raw_data: {"cost":5000, "sales":15000}
[analyze_data] analysis: CPC上涨15%，建议优化
[query_knowledge] knowledge: 行业ROAS基准: 2.5-3.5
每个 Agent 都能读写，不是串行传递
```

### 三种 LLM Provider

| Provider | 命令 | 需要 API Key | 特点 |
|----------|------|-------------|------|
| deepseek | `LLM_PROVIDER=deepseek` | ✅ | 真实调用 DeepSeek API |
| mock | `LLM_PROVIDER=mock` | ❌ | 模拟数据，快速演示 |
| openai | `LLM_PROVIDER=openai` | ✅ | 调用 OpenAI GPT |

---

## 四、生产部署

### Windows 部署

```bash
# 1. 修改 .env 关闭调试模式
#    默认就是生产模式，无需额外配置

# 2. 用 uvicorn 启动（推荐）
uvicorn fast_server:app --host 0.0.0.0 --port 5000 --workers 2
```

### Linux 部署

```bash
# 1. 安装依赖
pip install fastapi uvicorn pyjwt pillow python-dotenv openai chromadb pydantic

# 2. 启动
nohup uvicorn fast_server:app --host 0.0.0.0 --port 5000 --workers 2 > server.log 2>&1 &
```

### 使用 systemd 管理（Linux）

创建 `/etc/systemd/system/ad-agent.service`：

```ini
[Unit]
Description=Ad Agent Backend
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/ad_agent_backend
ExecStart=/usr/local/bin/uvicorn fast_server:app --host 0.0.0.0 --port 5000 --workers 2
Restart=always
RestartSec=5
EnvironmentFile=/path/to/ad_agent_backend/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable ad-agent
sudo systemctl start ad-agent
```

---

## 五、API 文档

启动服务后访问：http://localhost:5001/docs（Swagger 自动生成）

### 核心接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` | 登录获取 Token |
| POST | `/api/chat/stream` | AI 助手流式对话（SSE） |
| WS | `/ws/chat` | AI 助手实时对话（WebSocket） |
| GET | `/api/dashboard` | 仪表盘核心指标 |
| GET | `/api/daily_reports` | 每日报告数据 |
| GET | `/api/plans` | 广告计划列表 |
| GET | `/api/accounts` | 账户预算管理 |
| GET | `/api/alerts` | 告警列表 |
| GET | `/api/materials` | 素材管理 |
| GET | `/api/decisions` | AI 决策记录 |
| GET | `/api/logs` | 操作日志 |
| GET | `/api/chat/history` | 聊天历史 |
| GET | `/api/captcha` | 验证码图片 |

---

## 六、Docker 部署

### Dockerfile

项目内置 `Dockerfile`：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["uvicorn", "fast_server:app", "--host", "0.0.0.0", "--port", "5000"]
```

### 构建和运行

```bash
# 构建镜像
docker build -t ad-agent .

# 运行容器
docker run -d \
  --name ad-agent \
  -p 5000:5000 \
  -v ./.env:/app/.env:ro \
  -v ./data:/app/data \
  -v ./ad_agent.db:/app/ad_agent.db \
  ad-agent

# 查看日志
docker logs -f ad-agent
```

### docker-compose（推荐）

```bash
# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止
docker-compose down
```

---

## 七、内网穿透（让朋友访问）

### 方式 1：ngrok（免费够用）

```bash
# 1. 确保项目在运行
python fast_server.py

# 2. 另开终端，启动 ngrok
ngrok http 5001

# 3. 会生成一个网址，如：
#    https://xxxx-xx-xx-xx-xx.ngrok-free.app
```

> **ngrok 免费版限制**：每次启动 URL 变化，每月 40 分钟流量。

### 方式 2：云服务器

```bash
# 1. SSH 登录服务器
ssh root@你的服务器IP

# 2. 安装依赖
pip install fastapi uvicorn pyjwt pillow python-dotenv openai chromadb pydantic

# 3. 上传项目文件（scp 或 git clone）

# 4. 启动
uvicorn fast_server:app --host 0.0.0.0 --port 5000
```

---

## 八、测试

```bash
# 一键运行所有测试
python run_tests.py
```

### 测试覆盖

| 文件 | 测试数 | 覆盖内容 |
|------|--------|---------|
| `tests/test_multi_agent.py` | 10 | Agent 初始化、工具调用、安全拦截 |
| `tests/test_tools.py` | 13 | 工具函数、数据查询、预算管理 |

---

## 九、常见问题

### Q: 启动报错 "ModuleNotFoundError"
```bash
pip install fastapi uvicorn pyjwt pillow python-dotenv openai chromadb pydantic
```

### Q: API Key 错误
```
# 检查 .env 中的 API_KEY 是否正确
# 测试网络：ping api.deepseek.com
```

### Q: AI 助手一直"思考中"但无回复
```bash
# 切换到 mock 模式测试
# 在 .env 中设置：LLM_PROVIDER=mock
```

### Q: 数据库在哪里？
```
# 项目根目录的 ad_agent.db
# 删除它会自动重建（数据会丢失）
```

### Q: 怎么重置所有数据？
```bash
del ad_agent.db
python fast_server.py
```

### Q: 端口被占用？
```
# 程序会自动递增端口（5000 -> 5001 -> 5002...）
# 或手动指定：python fast_server.py --port 5050
```

---

## 十、安全注意事项

1. **不要分享 `.env` 文件** — 包含你的 API Key
2. **生产环境不要用 debug 模式**
3. **定期备份数据库** — 复制 `ad_agent.db` 文件

---

## 十一、技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 前端 | 原生 HTML + CSS + JavaScript |
| AI 接口 | DeepSeek API / OpenAI |
| Agent 架构 | Orchestrator + ReAct + 多Agent |
| 向量数据库 | ChromaDB |
| RAG 检索 | 语义向量检索 + TF-IDF 回退 |
| 数据库 | SQLite |
| 认证 | JWT (PyJWT) |
| 验证码 | PIL/Pillow 生成 |
| 图表 | ECharts CDN |
| 部署 | Docker / docker-compose |
