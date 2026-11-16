# 商品资料检索与原文回查

现行路径按商品资料的章节、FAQ 条目和规格表行切分，用真实 CPU BGE 检索，补查中英属性别名和子问题。默认从授权候选的前 20 条选证据。资料需匹配店铺、站点、SKU 和生效版本，前端可按分块回查原文。缺少必要资料时点名缺项。GPU 重排默认关闭。

使用真实 CPU BGE 验证过原 54 道冻结题和新增 48 道合成题，分别通过 54/54 与 48/48。原题集包含 44 道可答题、8 道无答案题和 2 道拒绝题。最后一轮的 88 条引用全部通过独立 HTTP 回查。当前业务在 SQLite 商品目录上验证，输入资料示例为 Markdown 说明书、FAQ 和 TSV 规格表。

## 复跑资料

- [资料清单](manifest.json)、[合成原文](sources/)、[冻结 54 题](frozen.jsonl)和[独立开发题](development.jsonl)。
- [48 道合成题](generalization-t1/questions.json)和[评分规则](generalization-t1/rubric.md)。
- [检索四组对照之一](results/C.json)、[扩展后候选基线](query-expansion/both.json)和[重排候选](rerank/frozen-candidates.json)。专项脚本可读取这些冻结输入，运行结果写入各自的本地输出目录。

## 导入、启动与核对

先按 [部署文档](../../DEPLOY.md#bge-与商品资料)安装并缓存 BGE。以下命令在仓库根目录用 PowerShell 操作独立测试库。

```powershell
$python = '.\.venv\Scripts\python.exe'
$env:AD_OFFLINE_ACCEPTANCE = '1'
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

本机使用合成账号 `product-evidence-owner` 登录，测试口令为 `local-product-evidence-fixture`。在会话中发送 `商品资料：evidence-main US METRO-500 接触饮料的内胆是什么材质？`。打开引用核对分块和原文。可从另一终端切换到 `v2`，再验证旧分块回查和新引用。

独立四组检索对照可运行 `python -m scripts.product_evidence evaluate --output logs/product-evidence-eval`。再执行 `python -m scripts.verify_product_evidence --run logs/product-evidence-eval --output logs/product-evidence-http` 核对授权、原文和版本。用新的输出路径可以保留上次结果。
