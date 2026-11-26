# 部署与复跑

## 本地运行

使用 Python 3.12。浏览器验收另需 Node.js 22+ 和 npm 10+。用 [README 的本地启动](README.md#本地启动) 建立隔离模拟实例，或在新终端配置下节的 Provider。统一入口为 `fast_server:app`。

```powershell
.\.venv\Scripts\python.exe -m uvicorn fast_server:app --host 127.0.0.1 --port 5010
```

首页为 `http://127.0.0.1:5010/`，OpenAPI 为 `/docs`。默认 `business` 模式可初始化空 SQLite，新库的业务账号需另外准备。本机示例账号随 README 的 `demo` 命令在独立库中创建。 [异地演示指南](docs/REMOTE_DEMO.md)

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

`LLM_PROVIDER=mock` 和 `AD_OFFLINE_ACCEPTANCE=1` 独立于演示种子开关。演示库应使用专用路径。启动在一个 SQLite 事务内为示例账号补齐本地日期最近 30 天中缺失的组合，已有日期和用户组合的所有旧行均保留。

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

`PRODUCT_EVIDENCE_MODEL_ADMISSION_ENABLED` 默认启用。对没有预配事实或引用规则的商品资料版本，聊天可用已配置的 Provider 从本轮授权候选逐项选择并复核原文。每轮最多两次请求，只提交当前问题、候选原文及本轮编号，答复仍由服务端原文模板呈现。设为 `0` 后回到只按预配规则引用的模式。Mock 和未配置密钥的实例沿用离线行为。

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

## Docker 与对外演示

容器配置统一使用端口 5010。根 Compose 固定 `business` 模式，将端口发布到宿主机回环地址，使用独立卷保存业务 SQLite、知识库与记忆。镜像健康检查使用 `/api/ready`，因此应在 `.env` 中配置独立 `JWT_SECRET`。固定 Provider 已在本机 WSL2 的 Linux 容器验收。

完整产品封闭预发使用显式 `DIAGNOSIS_SANDBOX_DB_PATH` 的 SQLite `business` 模式。备份时对业务库、只读聚合、知识库、会话记忆、资料清单和审计文件取同一停写截点。使用 `scripts/sqlite_store_backup.py` 核验和恢复备份。MySQL R2 提供回环诊断演练及有限的数据投影。

异地部署使用专用实例，先在外网完成访问控制、SSE 和审批回执走查，再分享地址。 [异地演示指南](docs/REMOTE_DEMO.md)

### 封闭运行与启动额度

使用 `deploy/storepilot-sealed/compose.yaml` 叠加配置和 `inspections` profile，可在本机启动 Web 及巡检消费者。封闭配置使用固定 Provider，Web 绑定宿主回环。需指定本地镜像和实例专用环境文件。

网关配置 `STOREPILOT_STARTS_PER_SECOND` 后，多个同机 Web 共享业务卷中的启动额度库。部署同一实例的各 Web 时使用相同额度和数据卷。审批、历史查询与工作群在途额度维持各自的接口约定。
