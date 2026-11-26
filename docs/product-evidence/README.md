# 商品资料检索与原文回查

商品资料导入后按说明书章节、FAQ 条目和规格表行切分。检索在当前账号可读的店铺、站点、SKU 和活动版本内进行，CPU BGE 可结合属性别名及子问题补查。进入答复的引用还须支持对应事实、对象和适用条件。可答部分先展示，缺项单独标明。GPU 重排默认关闭。

历史 CPU BGE 回放记录中，两套指定夹具分别为 54/54 与 48/48，最终轮 88 条引用通过独立 HTTP 回查。54 题夹具由 44 道可答题、8 道无答案题和 2 道拒绝题组成。该结果对应指定合成资料、问题和原文模板，覆盖召回、选引和权限回查。新资料的泛化和真实模型自由生成需要分别测量。

## 复跑资料

- [资料清单](manifest.json)、[合成原文](sources/)、[冻结 54 题](frozen.jsonl)和[独立开发题](development.jsonl)。
- [48 道合成题](generalization-t1/questions.json)和[评分规则](generalization-t1/rubric.md)。
- [检索四组对照之一](results/C.json)、[扩展后候选基线](query-expansion/both.json)和[重排候选](rerank/frozen-candidates.json)。专项脚本可读取这些冻结输入，运行结果写入各自的本地输出目录。

本目录的说明书与 FAQ 是合成商品原文，评分规则、候选和偏移供脚本复跑。更新题集应创建新版本，原清单和原文的字节哈希用于复验。

## 导入、启动与核对

先按 [部署文档](../../DEPLOY.md#bge-与商品资料)安装并缓存 BGE。以下命令在仓库根目录用 PowerShell 操作独立测试库。

```powershell
$python = '.\.venv\Scripts\python.exe'
$env:AD_OFFLINE_ACCEPTANCE = '1'
$env:APP_STARTUP_MODE = 'business'
$env:DIAGNOSIS_MYSQL_ENABLED = '0'
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LLM_PROVIDER = 'mock'
$env:JWT_SECRET = & $python -c "import secrets; print(secrets.token_urlsafe(32))"
$demoDir = New-Item -ItemType Directory -Path ("logs/product-demo-" + [guid]::NewGuid().ToString("N"))
$demoDb = Join-Path $demoDir.FullName 'demo.sqlite3'

& $python -m scripts.product_evidence import --db $demoDb --manifest docs/product-evidence/manifest.json --seed-demo
& $python -m scripts.product_evidence activate --db $demoDb --shop-id evidence-main --marketplace-id US --sku METRO-500 --version v1
$env:DIAGNOSIS_SANDBOX_DB_PATH = $demoDb
& $python -m uvicorn fast_server:app --host 127.0.0.1 --port 5010
```

本机使用合成账号 `product-evidence-owner` 登录，测试口令为 `local-product-evidence-fixture`。在会话中发送 `商品资料：evidence-main US METRO-500 接触饮料的内胆是什么材质？`，打开引用核对分块及原文。

保留旧引用，使用同一 SQLite 文件执行上面的激活命令，将 `--version` 改为 `v2`。旧分块 GET 应返回 404，新提问使用当前活动版本。会话历史仍可查看该问题，失效引用显示提示。[原文与历史契约](../../API.md#商品资料检索与原文)

独立四组检索对照可运行 `python -m scripts.product_evidence evaluate --output logs/product-evidence-eval`。再执行 `python -m scripts.verify_product_evidence --run logs/product-evidence-eval --output logs/product-evidence-http` 核对授权、原文和版本。用新的输出路径可以保留上次结果。
