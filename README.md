# StorePilot Agent

StorePilot 是面向跨境电商运营的 AI 工作台。用户可以用自然语言查销量、巡检商品资料、起草 Listing 英文五点，审核后再应用变更。每项结论都可以回查数据窗口、商品事实或执行回执。

[项目截图和讲解](docs/SHOWCASE.md) · [架构](ARCHITECTURE.md) · [接口](API.md) · [部署](DEPLOY.md) · [变更日志](docs/CHANGELOG.md)

![StorePilot 工作台中的销量分析和英文五点待审批卡片](docs/showcase/01-workbench.png)

## 可以做什么

| 业务任务 | Agent 的处理方式 |
| --- | --- |
| 一句话查销量 | 沿用会话已选 SKU 或请用户确认同名商品，按已核验的日期范围汇总销量，区分零销量和缺日报。 |
| 上新前巡检 | 在同店同站逐款比较两窗销量，核对 Listing 与商品资料，给出规格冲突、缺项和处理优先级。 |
| 商品资料问答 | 按店铺、站点、SKU 和生效版本检索，回答带可点击的原文引用。多项问题分别核验证据，缺项会单独指出。 |
| 生成和应用英文五点 | 逐条附上商品事实依据，展示改动和版本。人工批准后更新本地测试 Listing，页面另外读取实际版本与回执。 |
| 广告 CTR 独立复核 | 主 Agent 选择直查或临时派单。取证 Worker 可并行查询指标和规则，复核 Worker 等待已核验产物，工作台实时展示进度。 |

## 工程设计

- **运行时**采用 FastAPI、SSE 和支持工具调用的模型。主 Agent 按任务进展、证据和预算决定继续或停止。动态委派使用任务依赖和结构化产物，拦截重复工具调用与重复派单。
- **上下文**按账号、会话和轮次隔离。长期问答记忆使用 BGE 和 Chroma，召回需核对已提交的源问答和有效期。
- **商品 RAG**采用版本化入库、按结构切分和 CPU BGE。原问题候选与属性别名、子问题补查合并后逐项核验。GPU 重排默认关闭。
- **执行边界**由服务端校验属主、证据与版本。审批时再次核验提案，并在同一事务更新目标与保存回执。用户可以停止当前任务；广告暂停和商品修改分别使用各自的审批动作。

## 验收摘要

| 场景 | 已验证的结果 |
| --- | --- |
| 商品自然语言查询、比较与审批 | 真实 GLM 7/7。交换两款商品的销量后，入选对象随证据变化；缺证据时无提案、无业务写入；通过 Edge 实际批准并独立读回。 |
| 三款商品只读巡检 | 固定回放 18/18。真实 GLM 仅直查和开放委派各 8/8；两组都实际选择直查。商品 Worker 交接和读取重叠由固定回放验证。 |
| 广告多 Agent 复核 | 真实 GLM 验证直查、同批任务依赖、先取证再追加复核、缺证据时停止。展示页有同一轮的等待与完成截图。 |
| 商品资料 RAG | 真实 CPU BGE 两套题分别为 48/48 和 54/54，88 条引用独立 HTTP 回查通过。 |

销量分析使用 Olist 公开历史及合成业务数据，广告演示使用公开 Criteo 指标与本地沙箱。真实 Amazon 订单读取和 Listing 写入仍待取得卖家授权后联合验收。

## 本地启动

服务入口是 [`fast_server.py`](fast_server.py)，由 Uvicorn 启动 `fast_server:app`。仓库内的 `uv.lock` 和 `locks/` 固定了依赖版本。下面用 Python 3.12 在 Windows PowerShell 启动隔离的本地模拟实例。

```powershell
py -3.12 -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip --isolated install --require-hashes -r locks/acceptance.txt

$env:AD_OFFLINE_ACCEPTANCE = '1'
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LLM_PROVIDER = 'mock'
$env:JWT_SECRET = & $python -c "import secrets; print(secrets.token_urlsafe(32))"
$demoDir = New-Item -ItemType Directory -Path ("logs/local-demo-" + [guid]::NewGuid().ToString("N"))
$env:DIAGNOSIS_SANDBOX_DB_PATH = Join-Path $demoDir.FullName 'demo.sqlite3'
& $python -m uvicorn fast_server:app --host 127.0.0.1 --port 5010
```

浏览器打开 [本地工作台](http://127.0.0.1:5010)。本地回环实例可用初始化的普通测试账号 `zhangsan`，测试口令为 `user123`。测试商品、公开历史和完整流程可用下节的浏览器验收脚本准备。使用真实 Provider 或 BGE 时，在新终端按 [部署文档](DEPLOY.md) 配置模型、密钥与缓存。对外试用使用独立的环境和账号，见 [异地演示](docs/REMOTE_DEMO.md)。

## 离线评测与浏览器演示

在仓库根目录，用 Node.js 22+（含 npm 10+）安装 Playwright 并复跑本地隔离验收。固定回放使用合成数据，可在无模型密钥和无 BGE 权重的环境运行。

```powershell
npm ci
$env:PLAYWRIGHT_BROWSERS_PATH = '0'
npm run browser:install
& .\.venv\Scripts\python.exe -B -X utf8 -m scripts.run_acceptance --mode core
& .\.venv\Scripts\python.exe -B -X utf8 -m scripts.verify_workbench --output-dir "logs/workbench-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

浏览器演示生成销量、待审批、已执行和刷新读回记录。验收过程使用临时数据库和独立的运行目录。

## 计划历史与可计算诊断

在需要复跑广告历史指标案例时，将 [Criteo 固定版本公开历史](https://huggingface.co/datasets/criteo/criteo-attribution-dataset/tree/904188a63cbad78bee43cd26ff5ee4ac77903986) 中的 `criteo_attribution_dataset.tsv.gz` 放在 `data/public/criteo/`，从仓库根目录执行以下命令。导入会生成只读指标产物，核验命令从原始数据独立复算。

```powershell
& .\.venv\Scripts\python.exe -m scripts.import_criteo_metrics data/public/criteo/criteo_attribution_dataset.tsv.gz
& .\.venv\Scripts\python.exe -m scripts.verify_criteo_metrics data/public/criteo/criteo_attribution_dataset.tsv.gz data/plan_metrics/criteo.sqlite3
& .\.venv\Scripts\python.exe -B -X utf8 -m scripts.run_acceptance --mode full
```

账户绑定见 [部署文档](DEPLOY.md#计划历史数据部署)。
