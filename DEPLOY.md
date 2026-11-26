# 部署与复跑

## 本地运行

首次体验使用 README 的独立演示库。日常业务实例使用 `business` 模式和自己的数据库。Python 3.12 的依赖由锁文件提供，浏览器验收另需 Node.js 22+ 与 npm 10+。[从空环境启动演示](README.md#本地启动)

```powershell
.\.venv\Scripts\python.exe -m uvicorn fast_server:app --host 127.0.0.1 --port 5010
```

统一入口为 `fast_server:app`。打开首页前先检查 `/api/ready`，接口文档在 `/docs`。新建业务库只有 schema，账号和业务数据需另行准备。[远程演示部署草案](docs/REMOTE_DEMO.md)

## 环境变量

已有 `.env` 可直接编辑。新环境从 [环境变量示例](.env.example) 创建，凭据只放在服务端。给每个运行实例生成独立的 JWT 签名密钥。

```powershell
if (!(Test-Path '.env')) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

| 配置 | 用途 |
| --- | --- |
| `APP_STARTUP_MODE=business` | 默认启动模式，运行原有 SQLite schema 初始化和兼容迁移。MySQL 演练沿用 `DIAGNOSIS_MYSQL_ENABLED` 与既有路由白名单。 |
| `APP_STARTUP_MODE=demo` | 本地演示启动，需要独立 `DIAGNOSIS_SANDBOX_DB_PATH` 且关闭 MySQL。可使用无业务行的 SQLite，或保留四个原始示例账号 ID、名称和角色的演示库。 |
| `JWT_SECRET` | 登录使用的签名密钥。更换后需重新登录。 |
| `LLM_PROVIDER=glm`、`GLM_API_KEY`、`GLM_MODEL` | 智谱 GLM。默认模型为 `glm-4.5-air`。 |
| `LLM_PROVIDER=deepseek`、`DEEPSEEK_API_KEY`、`MODEL` | DeepSeek。兼容的 `API_KEY` 非空时优先。 |
| `LLM_PROVIDER=openai`、`OPENAI_API_KEY`、`OPENAI_MODEL` | OpenAI。 |
| `LLM_PROVIDER=mock` | 本地模拟模型。程序化回放参见测试命令。 |
| `DIAGNOSIS_SANDBOX_DB_PATH` | 独立业务 SQLite 的绝对路径，启用本地诊断沙箱。沙箱记忆在数据库邻接的 `.memory` 目录。 |
| `RAG_DB_PATH` | 知识库 Chroma 目录。默认 `data/rag_db/`，新目录需要导入资料。 |
| `PLAN_METRICS_DB_PATH` | 计划历史聚合 SQLite 的绝对路径。应用以只读方式打开，经绑定的本地计划才能查询。 |
| `LISTING_SIGNIFICANT_DECLINE_PCT` | 可选的销量下滑判定阈值，配置或用户指定阈值后才按其判定。 |
| `STOREPILOT_BACKGROUND_MODEL` | 启用后台巡检的模型标识。本机封闭 Compose 使用 `mock`。 |
| `STOREPILOT_STARTS_PER_SECOND` | 经限额网关进入的每账号每秒新启动数。未设置时内部检查端点关闭。 |
| `STOREPILOT_GROUP_INFLIGHT` | 工作群当前可并行执行的账号与全局额度，如 `2:3`。 |

`demo` 模式在一个 SQLite 事务中补齐示例账号与最近 30 天缺失的日报。只配置 `LLM_PROVIDER=mock` 时，服务仍按 `APP_STARTUP_MODE` 初始化。

## 存活与就绪

| HTTP 探针 | 结果 |
| --- | --- |
| `GET /api/health` | 保留原有 200 响应，供存活检查。 |
| `GET /api/ready` | 启动完成、JWT 通过登录使用的配置检查且所选存储通过基础检查时返回 200 `ready`，否则返回 503 `not_ready` 和固定原因。无效配置类型或初始化存储故障也可能导致服务在监听前退出。 |

SQLite 就绪检查打开现存文件，读取登录、普通会话、广告账号、计划与日报所需列，并在短事务内用零行更新确认会话表可写，关闭连接时回滚。锁繁忙时可暂时返回 503。MySQL 演练使用只读连接复查既有 schema、登录表，以及配置的聊天记忆根路径格式和服务器标识。就绪响应只给出状态，检查全程零模型请求。向量库可用性、模型供应商、后台执行者、提交持久性及完整业务流程需分别验证。

## BGE 与商品资料

本地模拟和固定回放使用 `locks/acceptance.txt`。真实 BGE 安装 `locks/bge.txt`，并预先下载固定模型修订。运行时从本地缓存读取。

```powershell
.\.venv\Scripts\python.exe -m pip --isolated install --require-hashes -r locks/bge.txt
.\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-small-zh-v1.5', revision='7999e1d3359715c523056ef9478215996d62a620')"
```

`PRODUCT_R4_CPU_ENABLED` 默认启用。设为 `0` 后重启可回到旧 CPU 召回。GPU 重排默认关闭，只有同时开功能和配置账号名单时才参与。支持从 GPU 超时回到当前启用的 CPU 路径。通过 SSE 的引用、历史和原文接口核对结果。商品资料的合成样例和导入命令见[复跑说明](docs/product-evidence/README.md)。

`PRODUCT_EVIDENCE_MODEL_ADMISSION_ENABLED` 默认启用。没有匹配的预配引用规则时，已配置密钥的 Provider 可参与逐项选引与复核。模型输入来自本轮经过权限及活动版本检查的候选，实际调用受本轮预算控制。服务端检查选用的原文、适用条件和出处，再呈现结果与缺项。将开关设为 `0` 可关闭模型准入。Mock 或未配置密钥时使用离线路径。[公开样本与复跑](docs/product-evidence/README.md)

## 计划历史数据部署

按 [README](README.md#计划历史与可计算诊断) 导入并核验 Criteo 聚合文件，再设置 `PLAN_METRICS_DB_PATH`，以同一业务实例绑定本人计划。公开样本用于离线验证指标，原始文件和业务数据库均通过部署环境取得或生成。

```powershell
.\.venv\Scripts\python.exe -m scripts.bind_plan_metrics <业务SQLite路径> <聚合SQLite路径> --plan-id <本地计划ID> --owner-id <属主ID> --source-plan-id <来源计划ID>
```

## 验收与真实模型

- `python -m scripts.run_acceptance --mode core` 运行离线统一门禁。浏览器依赖和安装命令见 [README](README.md#离线评测与浏览器演示)。`--mode full` 另需已核验的 Criteo 聚合文件。
- `python -m scripts.verify_workbench --output-dir logs/workbench-replay` 在隔离 SQLite 中复跑工作台、销量、人工批准及刷新读回。演示截图见[项目展示](docs/SHOWCASE.md)。
- `python -m scripts.accept_task45_t3 --browser-smoke --output-dir logs/inspection-fixed` 运行三商品只读巡检固定场景。配置真实 GLM 后，用 `--fixed-results logs/inspection-fixed --output-dir logs/inspection-live` 单独记录真实模型阶段。
- `python -m scripts.run_real_diagnosis --preflight --embedding fixed` 预检隔离诊断配置，通过后去掉 `--preflight` 运行真实模型。 `fixed` 仅替换嵌入。
- Node.js 测试可设置 `AD_BROWSER_CHANNEL=msedge` 使用本机 Edge，或用 `PLAYWRIGHT_BROWSERS_PATH=0` 与 `npm run browser:install` 使用锁定的 Playwright Chromium。

`pyproject.toml`、`uv.lock`、`locks/` 和 `package-lock.json` 为依赖来源。修改 Python 依赖时同时更新解析和 pip 哈希导出，升级后复跑门禁。

## Docker

根 Compose 以 `business` 模式启动单 Web，默认把 5010 发布到宿主回环。镜像健康检查使用 `/api/ready`。先从 `.env.example` 准备 `.env` 和实例专用 `JWT_SECRET`，再启动。准备依赖时镜像构建需要访问依赖来源或使用已有缓存。

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:5010/api/ready
```

### 封闭运行与启动额度

WSL2 验收使用固定 Provider、合成 SQLite 数据、单独 Compose 项目和回环端口。封闭配置复用本地镜像，启用 `inspections` 后 Web 就绪且完成 schema 初始化，消费者再启动。Web 和消费者共享 `/app/data/business.sqlite3`。

```bash
# 预先准备本地镜像 storepilot:sealed，例如 docker build -t storepilot:sealed .
export STOREPILOT_SEALED_IMAGE="$(docker image inspect --format '{{.Id}}' storepilot:sealed)"
export STOREPILOT_SEALED_ENV_FILE="$(mktemp)"
export STOREPILOT_SEALED_PORT=15046
export STOREPILOT_BACKGROUND_MODEL=mock
python3 -c 'import secrets; print("JWT_SECRET=" + secrets.token_urlsafe(48))' >"$STOREPILOT_SEALED_ENV_FILE"
dc=(docker compose --env-file /dev/null -p storepilot-sealed -f docker-compose.yml
    -f deploy/storepilot-sealed/compose.yaml --profile inspections)
"${dc[@]}" config -q
"${dc[@]}" up -d --wait
"${dc[@]}" ps
curl -fsS http://127.0.0.1:15046/api/ready
```

基本启动后可以通过已授权群订阅订单销量发布。多 Web 拓扑经过 Envoy 时，启动额度按认证账号在共享 SQLite 文件内占额。各 Web 使用相同额度配置、业务卷和签名密钥。网关负责速率检查，工作群在途名额由业务准入控制。[网关运行配置](deploy/storepilot-gateway/) · [账号额度实现](backend/agent/start_rate.py)

## 备份与恢复

在 Web 与消费者都停止写入后，从同一停写截点取得实例卷。备份工具识别 `business.sqlite3` 和同目录约定的知识库、会话记忆、资料、审计及可选的指标库和额度库。恢复后先核对清单与 SQLite，再用新卷启动实例。

```bash
python -m scripts.sqlite_store_backup backup /path/to/stopped-instance /path/to/new-backup
python -m scripts.sqlite_store_backup verify /path/to/new-backup
python -m scripts.sqlite_store_backup restore /path/to/new-backup /path/to/new-instance
```

外部演示使用专用实例及证据走查，MySQL R2 仅覆盖文档所列的诊断演练和有限投影。[远程演示部署草案](docs/REMOTE_DEMO.md) · [MySQL 演练接口](API.md#数据库运行模式)
