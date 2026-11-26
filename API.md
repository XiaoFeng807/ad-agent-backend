# API 接口文档

FastAPI 启动后在 `/docs` 提供 OpenAPI。本文补充浏览器使用的 SSE、来源校验、审批和重放约定。[应用入口](fast_server.py) · [启动命令](DEPLOY.md)

[认证](#认证) · [店铺订单](#店铺订单sqlite) · [巡检订阅](#后台巡检订阅与事件sqlite) · [商品审批](#工作群商品运营本地-sqlite-测试) · [商品资料](#商品资料检索与原文) · [搜索词诊断](#工作台广告搜索词诊断) · [健康检查](#健康检查)

## 数据库运行模式

默认业务库使用 SQLite。设置 `DIAGNOSIS_MYSQL_ENABLED=1` 后，FastAPI 连接预建的本机回环 MySQL 8.4 演练实例。连接需配置 `DIAGNOSIS_MYSQL_PORT` 和 `DIAGNOSIS_MYSQL_PASSWORD`。启动时校验所选 schema，MySQL 演练与 SQLite 暂停沙箱配置互斥。

| MySQL 演练配置 | 可用入口 |
| --- | --- |
| 启用 MySQL，schema 版本 1 或 2 | 验证码、登录、健康检查、诊断 SSE、诊断任务列表和详情、复核单确认和读回、规则分块原文。 |
| schema 版本 2，另设绝对路径 `MYSQL_CHAT_MEMORY_ROOT` | 增加首页、普通聊天 SSE 及普通聊天历史，主 Agent 和 Worker 的业务工具读 MySQL。 |
| 普通聊天已启用，另安装 TASK-027 会话表 | 增加持久工作群及群内聊天。 |
| schema 版本 2，另设 `DIAGNOSIS_MYSQL_PAUSE_ENABLED=1` | 增加首页、暂停确认及回执 GET。结构化诊断可为符合条件的已登记计划生成暂停提案。 |

版本由 `DIAGNOSIS_MYSQL_SCHEMA_VERSION` 选择。未纳入演练的业务路由返回 HTTP 404。计划历史指标仍读取独立的只读工件，知识检索走 Chroma。

## 认证

标有 Bearer 的 HTTP 接口从 `Authorization: Bearer <token>` 读取凭据。
同一账号重新登录后，旧令牌会失效。历史、原文、回执等接口按读取时的会话和资源权限判断。

### POST /api/login
登录并获取 token。

请求 JSON 包含 `username`、`password`，可选 `captcha_id`、`captcha_text`。

成功返回 `{ "code": 200, "data": { "token": "...", "user": { "user_id": 1, "username": "boss", "role": "boss" } } }`。

### GET /api/captcha
无需登录，返回 `code`、`captcha_id` 和 Base64 编码的 PNG `image`。

## 店铺订单（SQLite）

以下接口使用现有 Bearer 会话。每次读取按数据源、店铺与当前账号的授权查询，跨店或不存在的记录返回 HTTP 404，失效会话返回 HTTP 401。通过授权的响应使用 `Cache-Control: no-store`，成功响应格式为 `{"code":200,"data":…}`。

| 接口 | 行为 |
| --- | --- |
| `GET /api/orders/shops` | 返回可访问店铺的 `source`、`shop_id` 和 `source_label`。 |
| `GET /api/orders/shops/{source}/{shop_id}` | 返回店铺总订单及行项数、筛选后的订单数、状态选项、订单摘要和最近 10 条接入记录。支持 `status`（原始状态精确匹配）、`search`（订单号子串）、`limit`（默认 25，范围 1–100）和 `offset`。 |
| `GET /api/orders/shops/{source}/{shop_id}/orders/{order_id}` | 返回整单字段及本店行项，缺值为 JSON `null`。 |
| `GET /api/orders/shops/olist-v2/{shop_id}/sales/products` | 返回授权卖家可用的 `product_id` 和数据日期边界。 |
| `GET /api/orders/shops/olist-v2/{shop_id}/sales` | 按 `product_id`、`baseline_start`、`baseline_end`、`current_start`、`current_end` 和可选 `status` 读取两窗销量。日期窗等长、先后有序且互不重叠，每窗最多 90 天。默认纳入 `delivered` 和 `shipped`。结果包含每日件数与去重订单数、窗口总计、绝对及相对变化。零基准的相对变化为 `null`。 |
| `POST /api/orders/shops/amazon-us-fbm/{shop_id}/preflight` | 换取 LWA 令牌并用近期一小时的窗口探测 Orders 访问，读取一页、每页最多一单，写入预检回执。 |
| `POST /api/orders/shops/amazon-us-fbm/{shop_id}/sync` | 用带时区的 `updated_after`、`updated_before` 执行手动同步。结束时间需至少早于当前时间两分钟。完整分页通过后，同事务写订单、导入回执和适用的销量发布。 |
| `GET /api/orders/sales/publications` | 按 `after` 和 `limit` 读取当前账号有权查看的销量发布。 |
| `GET /api/orders/sales/publications/{publication_id}` | 回查单个发布、商品销量快照和导入批次。 |
| `GET /api/orders/shops/{source}/{shop_id}/sales-imports/{batch_id}` | 分页回查该店铺的导入批次与原行。 |

订单来源为 `olist-v2` 或 `amazon-us-fbm`。Olist 的原始状态、交运时间、混合卖家标记为整单字段，`items[].ship_by` 为行项发货截止。Olist 下单、交运和行项截止保留 CSV 原值，时区未知。Amazon 的 `ship_by` 为整单发货截止，时间统一为 UTC。两者均提供 `synced_at`，有源更新时间时提供 `source_updated_at`。

Amazon 回执包括操作、结果、窗口、已读取页数、实际写入订单和行项数、错误代码及上游 HTTP 状态。缺少凭据返回 503，限流返回 429 并转发上游以秒表示的 `Retry-After`（如有），其余上游失败返回 502，回执在响应的 `data` 字段。重复页去重，旧版本保持现有较新记录。每次同步最多读取 500 页，超过上限时缩短窗口重新运行。应用页入口为 `/orders`。

Amazon 只接收可确认的美国站卖家自发货订单。明确属于其他站点或 FBA 的记录会过滤。无法确认站点或履约方时返回 `amazon_invalid_marketplace` 或 `amazon_invalid_fulfillment`。目标订单缺少有效创建或更新时间时返回 `amazon_invalid_created_time` 或 `amazon_invalid_last_updated_time`。末页省略 `pagination`，携带 `pagination` 的响应必须提供有效 `nextToken`，否则返回 `amazon_invalid_pagination`。这些失败的 `pages` 含收到但校验失败的当前页，订单、行项和原同步时间保持上次提交的状态。可选发货截止缺失或为空时，`ship_by` 为 `null`。

## 工作群商品运营（本地 SQLite 测试）

现有 `POST /api/chat/stream` 在工作群接受可选布尔字段 `listing_operations`，配合 `conversation_id` 与 `message` 使用。显式开启或服务端识别为商品运营请求时，在当前用户有权访问的测试店铺内解析商品，也可沿用会话已选 SKU。日期可显式给出连续总范围或两个等长比较窗。默认按北京时间截至昨天取 14 个自然日，两窗各 7 天。

群历史的商品回复包含 `evidence_kind=listing_operations_v1` 和 `listing_actions`。现有群轮次证据 GET 返回已核验的测试指标、商品事实、原 Listing、产物 ID 和草案。缺证据时返回 `gaps`，没有可批准的动作。草案五点由模型从版本化事实的措辞选项中选择和排序，服务端精确校验每项来源。

群历史的 Listing 和 Olist 销量回复附带 `sales_summary`，包含两窗日期与件数、绝对变化和相对变化百分比。未取得的件数或变化为 `null`。Listing 回复也可包含 `has_product_details` 和 `evidence_gap_count`。完整数据仍通过对应轮次的证据入口回读。

同店同站内的三款商品可发起逐款只读巡检。结果用 `evidence_kind=listing_inspection_v1` 标识，保留按商品核验的证据、规格冲突、缺项及优先项。工作群请求同时包含巡检和英文卖点改写时，按北京时间最近两个完整七日窗比较同一批商品。可以点名商品，或在授权目录恰好只有同店同站三款时用「这三款商品」指代。销量齐全后，以 `LISTING_SIGNIFICANT_DECLINE_PCT` 筛选需核对当前五点的商品。唯一相对降幅最大且有原文支持的遗漏时，生成一份五点待审批提案，依据由 `operations_inspection` 保存。

| 接口 | 行为 |
| --- | --- |
| `POST /api/chat/conversations/{conversation_id}/runs/{run_id}/listing-actions/{action_id}/confirm` | 核验会话、群主、来源轮次、店铺、SKU 和证据版本。只修改本地测试 Listing 五点及版本，回执同事务提交。重复批准读回同一回执。 |
| `GET /api/chat/conversations/{conversation_id}/runs/{run_id}/listing-actions/{action_id}` | 独立读回草案、当前 `listing` 与历史 `receipt`，使用 `Cache-Control: no-store`。 |
| `GET /api/chat/conversations/{conversation_id}/runs/{run_id}/listing-actions/{action_id}/review` | 从已执行的巡检待办读取回执以及审批日后七个完整日的销量，对照提案冻结的基准窗。返回两窗日点、总量、覆盖、缺报日期及可计算的变化量，使用 `Cache-Control: no-store`。 |

审批面板打开待审批提案时可在同一 GET 附加 `?review=true`。服务端只读校验群归档状态及当前证据快照，失效时返回 `approval_blocked`。确认 POST 在写事务内重新校验。

动作不可见或不存在时返回 404。依据失效时首次批准返回 409，存储失败返回 503。销量更正时，有权用户可从 `approval_block_reason` 查看原销量与当前销量；撤权后的响应不提供这些字段。已执行动作依据动作 ID 读回原回执，同一事务中只产生一次版本更新和一条回执。只撤订单读权时，Listing 仍可见的回执继续可读，旧订单与报表依据隐藏，复盘返回 404。

## 后台巡检订阅与事件（SQLite）

以下路径以 `/api/chat/conversations/{conversation_id}/background-inspections` 为前缀。请求使用 Bearer，并需要群和目标数据的读权。部署需要配置后台模型及运行消费者。

| 接口 | 请求与结果 |
| --- | --- |
| `POST /subscriptions` | 登记 `source`、`shop_id`、`marketplace_id`、`sku`、两个日期窗的四个端点 `windows` 和布尔字段 `allow_listing_proposal`。成功返回 201 和 `subscription_id`。 |
| `POST /publications` | 显式受理指定的 `publication_id` 和 `windows`，可传 `recheck`。有效订阅也可由消费者自动受理。 |
| `POST /` | 路径为前缀本身。用 `message` 和 `Idempotency-Key` 受理后台巡检，首次返回 202，重放返回 200 和原 `run_id`。 |
| `GET /{run_id}` | 读已授权的运行状态、事件和可见答案。可用 `after` 从指定事件序号继续读取。 |
| `GET /{run_id}/events` | SSE 推送，使用 `Last-Event-ID` 或 `after` 续读，浏览器断线后消费者可以继续执行。 |

报表覆盖不足时巡检可以保存说明。证据齐备且订阅允许生成提案时，产物仍等待人工批准。[封闭 Compose 配置](DEPLOY.md#封闭运行与启动额度)

## 商品资料检索与原文

SQLite 商品目录支持按授权店铺、站点和 SKU 查询当前有效资料。使用 Bearer 会话。

| 接口 | 行为 |
| --- | --- |
| `POST /api/product-evidence/search` | 请求体含 `shop_id`、`marketplace_id`、`sku`、`query`。可选 `strategy=fixed\|structured`、`mode=vector\|hybrid`、`top_k=1–20`，默认结构化、混合、20 条。返回 `version`、`candidates`、`evidence`、`status`、`reason`。 |
| `GET /api/product-evidence/chunks/{chunk_id}` | 核对会话、商品授权和当前有效版本。返回源文档、版本、命中正文、完整源文本、原文位置和 Unicode 码点偏移。响应使用 `Cache-Control: no-store`。 |

`candidates` 用于查看召回排名。只有受支持的问题且必要原文齐全才返回 `evidence`。其余请求返回 `status=insufficient_evidence`，包括已授权但尚无有效资料的商品。未登录返回 401，越权商品、失效分块或不存在的分块返回 404。

普通聊天和工作群可在 `POST /api/chat/stream` 中输入「商品资料：店铺 站点 SKU 问题」。目标不明确时，系统展示能从题面可靠匹配且当前可读的选项，或请用户提供店铺、站点和 SKU。选择后重新提交，服务端在检索前重验权限与活动版本。澄清提示和有效来源引用可在刷新后读回。[资料导入与版本切换](docs/product-evidence/README.md)

普通聊天与工作群的历史回查按当前店铺读权和活动版本展示来源。引用失效时隐藏该来源及其版本元数据并显示失效提示。仍有效的引用、原问题和缺项说明保留。历史回执在读取时处理，响应使用 `Cache-Control: no-store`。

无预配事实或引用规则的版本可通过已配置的 Provider 对候选原文逐项取证。服务端核验原文出处和适用条件，先显示支持的原文与链接，再列出缺少依据的子问题。原文回查和保存时使用当前会话、商品授权和活动版本。 [部署配置](DEPLOY.md#bge-与商品资料)

## 数据

### GET /api/dashboard
获取仪表盘概览数据（Bearer）。

### GET /api/daily_reports?days=7
获取当前会话用户的每日趋势（Bearer）。服务端先按属主筛选，再按日期汇总并按日期倒序截取最近 `days` 个报告日，默认 7。返回 `data` 数组，每项包含 `report_date`、`cost`、`impressions`、`clicks` 和 `sales`。

`days` 须在 SQLite 有符号 64 位整数范围内，超出范围返回 HTTP 422。`0` 返回空数组，负数返回本人的全部报告日。

### GET /api/plans
获取当前会话用户的广告计划列表（Bearer）。

### POST /api/plans
为当前会话用户创建广告计划（Bearer）。请求 JSON 包含 `plan_name`，可选 `platform`（默认 `Google Ads`）和 `daily_budget`（默认 300）。服务端从会话取得 `user_id`。请求体含 `user_id` 时返回 HTTP 400，`detail` 为「计划属主由当前会话确定」。

创建参数须为有效 JSON 对象。计划名与平台须为字符串，日预算支持有限数值和可解析的有限数字字符串。字段类型无效、预算非有限或为布尔值时返回 HTTP 400。

### POST /api/plans/{plan_id}/toggle
暂停或开启当前用户的广告计划（Bearer）。成功返回 `data.new_status`。他人计划和不存在的计划均返回 HTTP 404，`detail` 为「计划不存在」。

### POST /api/plans/{plan_id}/ctr-alert-check
SQLite 投放计划页的「检查 CTR」调用此入口（Bearer）。请求只需路径里的本地计划 ID。服务端核验已提交会话和计划属主，再读取计划绑定的只读历史指标。

比较窗口截至检测日前一日（Asia/Shanghai），每窗最多七天。在两个相邻、等长且完整覆盖的窗口组合中，优先取天数最多的组合，同长度取结束日期最晚的组合。`ctr-review-v1#low-ctr` 要求每窗至少 1,000 次曝光，且总点击除以总曝光得到的 CTR 相对下降至少 20%。

HTTP 200 返回 `{"code":200,"data":{…}}` 和 `Cache-Control: no-store`。

| `data.status` | 结果 |
| --- | --- |
| `created` | 创建一条 `ctr_review / critical` 告警并返回整数 `alert_id`。 |
| `existing` | 同一属主、计划、两窗、来源绑定及规则版本已生成告警。返回原 `alert_id`、检测时间和指标快照。 |
| `below_threshold` | 两窗指标可比较，降幅低于门槛。`alert_id` 为 `null`。 |
| `insufficient_evidence` | 绑定、来源、窗口覆盖、曝光门槛或基准 CTR 有缺项。`reason` 标识缺项，`alert_id` 为 `null`。 |

`data.detected_at` 是 UTC ISO 8601 时间。已核验的 `baseline` 和 `current` 各含实际起止日、覆盖、曝光、点击和 CTR 百分比。`comparison` 在两窗达到曝光门槛且可计算时给出百分点差和相对降幅。`binding`、`rule` 和 `policy` 提供来源版本与规则门槛。工作群发现回执及已提交的复核依据在该告警关联的 `ctr_check` 中保留触发时的指标窗，复核取证日期在 `review_scope` 中。

会话无效返回 HTTP 401。他人计划与未知计划均返回 HTTP 404，`detail` 为「计划不存在」。取证后发现绑定或来源版本变化时返回 HTTP 409。拒绝请求不新增告警。告警生成与工作群的沙箱暂停审批各自提交。

### DELETE /api/plans/{plan_id}
删除当前用户的广告计划（Bearer）。他人计划和不存在的计划均返回 HTTP 404，`detail` 为「计划不存在」。

切换与删除的 `plan_id` 须在 SQLite 有符号 64 位整数范围内，超出范围返回 HTTP 422。

### GET /api/alerts
获取当前用户的告警列表（Bearer）。

### POST /api/alerts/read
标记当前用户的告警已读（Bearer）。请求 JSON 可传 `ids` 数组，数组为空或省略时可传单个 `alert_id`。未指定告警时标记当前用户的全部告警。

### GET /api/accounts
获取当前会话用户的广告账户列表（Bearer）。上述计划和账户入口在会话缺失、过期或已被替换时返回 HTTP 401。

### GET /api/materials
按 ID 倒序返回当前会话用户的素材（Bearer）。本人未关联计划的素材仍在 `data` 数组中。关联计划与素材同属主时返回 `plan_id` 和 `plan_name`。没有有效同属主关联时，两字段均为 `null`，素材仍可见。

### GET /api/decisions
按当前会话用户筛选后，按 ID 倒序返回最多 20 条决策记录（Bearer）。`data` 是记录数组，包含 `category`、`suggestion`、`status`、`outcome`、`verified_at`、`created_at` 等字段。首页按记录实际状态展示待执行、已执行和已验证的建议。

### GET /api/logs
使用已提交会话与数据库当前角色读取最近 50 条操作日志（Bearer）。`boss` 可读全部日志，`admin` 可读当前管理员和普通用户的日志，普通用户仅可读本人日志。账号已删除或会话无效时返回 HTTP 401。

### GET /api/timeline
获取当前会话用户的活动时间线（Bearer）。返回 `data.success` 和 `data.plans`，计划先按投放中优先排序，再按开始日期倒序排列。

日报、素材、决策和时间线读取均使用有效的已提交会话。无属主记录不进入结果，会话缺失、过期或已被替换时返回 HTTP 401。

## AI 对话

### POST /api/chat/stream

AI 对话（Bearer）。请求 JSON 形如 `{ "message": "今天 ROAS 多少" }`。服务端从 token 识别用户，以 SSE 返回事件。普通对话可附 `chat_view_id`，接受长度为 1–64 的字符串。服务端将有效标识随本轮消息保存，供历史接口按页面去重。

`thinking` 和终态事件带 `trace_id`，可在 `logs/traces.jsonl` 中回查。服务端提交助手回复后发送一次 `done`，执行失败（含降级也失败）或保存异常时发送一次 `error`。提交前断开的请求以 `interrupted` 结束轨迹。

普通聊天、结构化诊断和一句话诊断在每次模型调用前检查完整载荷。必需载荷超过预算时，终态为 `error`，并带有 `error_code: context_budget_exceeded`。同一 Trace 的 `context_budget` 步骤可回查输入估算、输出预留、余量及裁剪结果。

普通聊天以认证用户和本轮已提交用户消息 ID 为边界，模型及请求体不能覆盖身份或扩大历史范围。意图从本轮可见历史中的用户消息生成局部快照。向量检索须带用户和消息 ID 条件，过滤异常不重试无条件查询，工具回执及 Trace 使用固定错误码 `conversation_memory_filter_failed`。回执再次查询服务端已提交的 `conversation_turns`，核对同轮关联、属主、角色、两条消息均早于本轮用户消息及原文。相邻消息、时间接近、相同 `chat_view_id` 和向量元数据自报轮次均不作为配对依据；验证失败的内容不进入模型。

普通助手回复与问答关联在同一 SQL 事务中提交，关联使用本次请求已取得的用户消息 ID。随后在同一次工作线程调用中执行长期向量索引。提交后的客户端断开仍允许该索引完成。以本人用户消息 ID 为幂等键，并发请求倒序完成及消息 ID 空洞不改变配对。回复或关联保存失败时一同回滚，向量写入为零，SSE 只有一个 `error` 终态。提交后的向量或摘要故障分别记录 `conversation_memory_write_failed` 和 `conversation_summary_write_failed`，仍连接的客户端收到一次 `done`。

普通聊天和工作群的记忆 `ttl` 必须为严格的 ASCII `YYYY-MM-DD` 合法日期，表示 **Asia/Shanghai 到期日零点起失效**。`ttl=2026-11-13` 在北京时间 11 月 12 日 23:59:59 有效，11 月 13 日 00:00:00 起无效。写入按北京时间当日加 30 天生成到期日；召回与清理共用同一校验，缺失、错误格式、不存在的日期和无法解析的值一律拒绝，清理时删除，不依赖服务器本地时区。

SQLite 初始化幂等创建关联表，不从旧消息猜测配对。无法证明同轮的旧向量退出召回，聊天原文及历史 API 保留；只有已有可靠 SQL 关联的问答允许重新索引。工作群继续以已完成的 `run_id` 证明同轮。MySQL R2 普通聊天须先显式执行幂等升级 [mysql_task25_schema.sql](scripts/mysql_task25_schema.sql)；原五参数 `save_message` 及原 `save_conversation` 调用保持兼容，未提供关联的旧调用只保存历史，不获得向量索引资格。

#### 持久工作群

以下入口均使用 Bearer。工作群不存在和非属主访问统一返回 HTTP 404，响应为 `{"detail":"工作群不存在"}`。

| 入口 | 契约 |
| --- | --- |
| `POST /api/chat/conversations` | 可提交 `{"title":"品牌投放周报"}`。名称去除首尾空白后为 1–80 字，省略时使用「新工作群」。HTTP 201 返回工作群。 |
| `GET /api/chat/conversations` | 返回本人的工作群，含已归档项。 |
| `GET /api/chat/conversations/{conversation_id}/history` | 返回 `conversation`、`entries` 和 `runs`。页面轮询本入口订阅读取已提交的进度。 |
| `GET /api/chat/conversations/{conversation_id}/runs/{run_id}/evidence` | 群主回读已提交的多窗复核依据。返回群、轮次和答案 `entry_id`，以及采用的四窗指标、指定规则、获准的详情和缺项。无依据、轮次与群不匹配或非属主均返回 404。 |
| `PATCH /api/chat/conversations/{conversation_id}/archive` | `{"archived":true}` 归档，`false` 恢复。运行中归档返回 409。 |
| `POST /api/chat/stream` | 普通聊天提交 `{"message":"今天数据","conversation_id":"工作群 ID"}`。群已归档或已有在途轮次时返回 409。SSE 带 `conversation_id`、`run_id` 和 `trace_id`，提交回复后发送 `text` 和 `done`。 |
| `POST /api/chat/conversations/{conversation_id}/stop` | SQLite 群主提交 `{"run_id":"指定轮 ID"}`。成功及重复请求返回同一条停止记录。答案已提交时返回 409，历史及依据仍可回读。错群、未知轮次及非属主返回 404。 |
| `POST /api/chat/conversations/{conversation_id}/runs/{run_id}/pause-actions/{action_id}/confirm` | SQLite 暂停沙箱中，群主批准原答案绑定的暂停提案。无需请求体。并发及重复批准返回同一回执。 |
| `GET /api/chat/conversations/{conversation_id}/runs/{run_id}/pause-actions/{action_id}` | 群主独立读回 `data.receipt` 和计划行的 `data.actual_status`。响应带 `Cache-Control: no-store`。 |
| `POST /api/chat/conversations/{conversation_id}/date-changes` | SQLite 中的群主提交 `{"run_id":"当前轮 ID","message":"把两组 7 天改成两组 3 天"}`。返回 `proposal_id`、`run_id`、`trace_id`、`old_dates` 和 `new_dates`，或返回同轮的 `clarification`。每个日期范围含 `baseline` 和 `current`，各有含起止日的 `start_date` 和 `end_date`。 |
| `POST /api/chat/conversations/{conversation_id}/changes` | 「修改要求」使用此入口，支持改期和换一条计划。请求包含 `run_id` 和 `message`。换计划预览返回 `kind:"plan_change"`、`proposal_id`、`run_id`、`trace_id`、`remove_plan_id`、`add_plan_id`、`old_plan_ids` 和 `new_plan_ids`。含糊时返回 `clarification`。 |
| `POST /api/chat/stream` | 新入口的确认体为 `{"conversation_id":"工作群 ID","change":{"run_id":"旧轮 ID","proposal_id":"提议 ID"}}`。改期和换计划使用相同的 SSE 终态与重复确认语义。 |
| `POST /api/chat/stream` | 群主提交 `{"conversation_id":"工作群 ID","date_change":{"run_id":"旧轮 ID","proposal_id":"提议 ID"}}` 确认。新轮使用原 SSE 协议。重复确认只返回带 `already_confirmed:true` 和新轮当前 `status` 的 `done`，`run_id` 与 `trace_id` 均指向同一个新轮。 |

- `conversation_id` 跨登录和页面刷新保持稳定。`run_id` 每轮生成，与本轮 Worker 看板一致。`trace_id` 用于链路查询。`chat_view_id` 仅供兼容入口做页面去重。
- 工作群消息可带 `Idempotency-Key` 请求头。同一账号、工作群和动作内，同键参数变化返回 HTTP 409（`idempotency_key_conflict`）。消息去除首尾空白，自动商品运营、显式开启和显式关闭分别参与参数比对。相同提交的重发返回一帧 `done`，含 `replayed: true`、原 `run_id`、`trace_id`、当前 `status` 和 `history_url`。运行中的 `status` 为 `running`，已提交的答案和动作经原历史入口读取。有效键为 1–128 个可打印 ASCII 字符，首尾须无空白。无键请求沿用独立受理，每次主动新跑使用新键。
- 停止请求与答案保存竞争同一个 SQLite 写事务。停止提交后 `runs[].status` 为 `stopped`，`runs[].stop` 和停止响应的 `data` 均返回 `conversation_id`、`run_id`、`trace_id`、`stopped_by_user_id`、`stopped_by_name`（账户用户名）及 UTC `stopped_at`。群历史保留已提交进度，正在执行的调用退出后追加实际回收事件并发送一次 `stopped` SSE 终态。页面读到停止提交即可发送下一轮。停止仅操作工作群记录及指定轮的执行预算。
- 显式配置隔离的 `DIAGNOSIS_SANDBOX_DB_PATH` 后，独立复核完成、证据齐全、命中 `ctr-review-v1#low-ctr` 且目标为本人已登记的 `active` 沙箱计划时，暂停提案随答案和依据一起提交。历史在原答案的 `pause_actions` 返回 `action_id`、目标、原因、审批状态和已批准回执摘要。审批前计划保持 `active`。
- 批准事务检查当前会话、群主、群与轮次、原答案、计划属主、登记和预期状态，原子提交计划暂停和回执。回执含 `receipt_id`、`conversation_id`、`run_id`、`entry_id`、`source_trace_id`、`execution_trace_id`、`approved_by_user_id`、`approved_by_name`、UTC `created_at`、目标、证据快照和 `committed_status: "paused"`。独立 GET 读取计划当前状态。沙箱未启用、错群、错轮、非属主、登记失效或回执未提交时返回 404。无效会话返回 401，批准前目标状态变化返回 409，写入失败返回 503 并可用原动作重试。
- 运行中的多窗复核可改期。只指定天数时预览保留各自结束日。预览、澄清和日期校验失败期间，旧轮继续运行。无效日期返回 422，旧轮已结束时返回 409，未知轮次或非属主访问返回 404。确认在同一 SQLite 事务里替换旧轮、记录新旧日期并启动新轮。旧轮提交先完成时，确认返回 409 并提示发起新一轮。旧轮 SSE 以 `superseded` 结束，群历史保留 `kind:"change"`、新旧轮及 Trace 标识和具体日期。新轮重新取证，旧日期的指标调用在底层查询前被拒绝并返回 `effective_dates`。
- 换计划时，移出的计划须在原组合内，加入的计划须与原两条不同。服务端在预览和确认时查询计划归属，未知计划和非本人计划均返回 422 和同一错误正文。确认保留原日期、规则、告警及独立复核要求，历史变更事件为 `kind:"plan_change"` 并记录新旧组合。范围外的指标调用返回 `effective_plan_ids` 和 `effective_dates`，详情查询依据新轮的同轮证据和入选结果准入。
- 接收请求时读取该群已提交的历史。群内一次接收一轮，多个群可并发。Worker 接收任务单、授权工具及本轮引用的证据。群向量记忆按属主、会话和本轮消息边界过滤，随后核对有效 TTL，并回查已完成 `run_id` 的同轮问答及全文摘要校验值。无群归属的旧向量仅在兼容入口且可证明同轮时参与召回。
- `entries` 的 `kind` 包括 `user`、`assistant`、`worker` 和 `change`。`entry_id` 用于回放去重。原历史使用 `legacy:<消息 ID>`，新增条目使用 `entry:<条目 ID>`，新增条目按服务端递增 ID 排序。Worker 事件包括 `queued`、`waiting`、`dispatched`、`tool_started`、`receipt`、`review_inputs`、`failed`、`succeeded` 和 `reclaimed`。新事件以 `run_id` 和服务端分配的 `task_label` 标识任务，并附适用的依赖、Worker、工具调用及产物标识。`tool_started` 记录通过校验后发起的读取，`runtime_call_id` 可与对应回执核对。`runs[].status` 可为 `running`、`completed`、`superseded`、`error` 或 `interrupted`。带 `has_evidence: true` 的答案可通过对应轮次的依据入口回读。
- SQLite 启动时幂等建立会话表。隔离 MySQL 先完成 R2 安装或导入，再由演练库管理员应用 [TASK-027 SQL](scripts/mysql_task27_schema.sql)。普通聊天应用账号须能 SELECT 会话表并 INSERT 三张会话表，同时需要 UPDATE `chat_groups` 和 `chat_group_runs`。未安装扩展的 R2 演练继续使用兼容普通聊天。
- 已安装工作群的 MySQL 演练库可重新应用上述 SQL 添加 `chat_group_run_requests`。有键的工作群提交要求应用账号可对该表 SELECT 和 INSERT。
- 原 `conversations` 行按属主映射至稳定的「历史聊天」，消息主键和原文保留。无 `conversation_id` 的 SSE、旧历史和 WebSocket 仍访问原消息表。群页面从「历史聊天」发起的新轮次使用群存储。诊断请求使用原有独立任务契约。
- 持久工作群支持两条明确 `#计划ID`、同一绝对日期窗及一个 CTR 百分比阈值的条件读取，例如「仅在确认恰好一条计划的 CTR 达到 20% 时，再查该计划名称、状态和日预算」。服务端固定本轮条件，并按已核验回执计算 CTR。主 Agent 与 Worker 仅在两条指标均可判定、且恰好一条符合条件时读取该计划一次。检测到条件但参数不明确时，请求澄清。条件未核实时，回复列出已核验指标和缺项。该规则使用有明确边界的首版请求语法。
- 工作群的多窗复核请求支持两条 `#计划ID`、等长且互不重叠的基准窗与当前窗、明确的点击率下降复核规则版本，以及「清单确定后，只读取入选计划的详情」的前提。详情读取前核对四份计划窗口回执和精确版本规则，按已核验指标复算入选目标。若请求声明严重 CTR 告警前提，也须核实本轮本人未读严重 CTR 告警。主 Agent 和 Worker 使用同一准入，每个入选计划详情最多读取一次。已识别但参数不齐时先澄清。缺项时列出可核验的证据并暂缓清单。 [交接与结果实拍](docs/SHOWCASE.md)
- 多窗复核仅余一次模型调用时，若还有合格详情待查且工具额度可用，最后一次请求只提供 `get_plan_detail`，模型输入由服务端生成，包含待查计划和本轮采信的指标及指定版本规则产物标识。结构化调用仍须通过本轮准入，最终答复直接使用核验回执。查询失败时，文本说明详情未取得，JSON 在适用时附 `detail_failed_plan_ids`。

#### 普通聊天的动态委派

不带 `diagnosis` 的请求由主模型决定直接回答、调用只读工具或调用 `delegate_tasks`。模型提交任务 ID、目标、能力、依赖、输入引用、预期产物及停止条件。服务端注入认证用户并校验参数、资源归属和引用范围。Worker 可用能力为计划详情、计划历史指标、用户日报和知识检索。

主模型通过原始 `tool_call_id` 收到当批 `evidence`、`summaries`、`gaps` 和剩余额度，再决定回答、直查补项或追加一批。`direct_refs` 选取本轮由上述四项能力直查并校验成功的产物。`input_refs` 选取声明依赖的整组产物，`artifact_refs` 精确选择先前已成功任务的产物。引用只在当前请求的服务端看板有效。客户端消费 SSE 事件。

普通聊天和工作群的主 Agent 在每批工具回执处理完后，根据已核验进展和共享预算决定继续或收尾。新取证按工具、规范化参数及实际采信的产物识别，知识检索按分块识别。重复读取、变更调用 ID 和 Worker 的文字总结均不增加进展。首次可纠正的参数或任务单错误提供字段与原因。连续两轮无新增证据时收尾，取得新证据后清零。永久缺项沿用原参数复用及依赖阻塞规则，独立取证和定向补查继续适用。

读取在实际执行前按本轮工具名与规范化参数去重，主 Agent 和 Worker 共用同轮回执。复用计划证据前重新检查计划属主及适用的复核范围，原产物 ID 保留。向新 Worker 交接前也检查引用产物及其摘要来源的计划属主。权限变更返回 `read_scope_changed`，受影响的计划回到缺项。临时失败的单次原参重试在同一个执行段内完成，保留 `retry_of`。可选 `tasks[].read_scope` 声明确切的 `{"tool": "...", "arguments": {...}}` 动作，服务端核对能力及参数。在创建 Worker 前，根据该范围、依赖和实际输入产物拦截重复派单，返回 `duplicate_dispatch`。自由文本任务沿用 Worker 工具准入和进展判断。工作群历史持久保留复用、重复分派拦截及无进展收尾提示。

本轮共享最多 12 次模型调用、12 次实际读取和 90 秒执行时限。独立复核保留现有 16 次模型额度。动态委派累计最多 8 项任务、同时 3 个 Worker。取证为收尾预留一次模型机会，复核的最终详情查询继续使用原准入。已识别的逐项计划状态或详情读取在取证未齐时，回复列明已核验的计划、具体缺项及停止原因。Trace 的 `react_decision` 记录原因、采信产物、可确认缺项和剩余额度。SSE 的 `done` 确认回复已提交。页面和历史显示「回复已保存」，任务进度见回复和证据。

临时工具失败在额度内按原参数补查一次。断线关闭调用和回执准入，在途同步调用退出后归还 Agent。时限或模型额度用尽且无法完成回复时，终态为带 `deadline_exceeded` 或 `model_budget_exhausted` 的 `error`。

#### 一句话诊断

首页「投放诊断」默认发送以下请求。正文去除首尾空白后须为 1–2000 字符。新任务的 `diagnosis` 只包含模式标识。

```json
{
  "message": "帮我找出最近点击率下滑最明显、值得暂停复核的一条投放计划。",
  "diagnosis": {"mode": "natural"}
}
```

- 服务端只解析当前一句，支持找出 CTR / 点击率下降最明显的投放计划这一比较；例如默认示例，或将其中「最近」替换为「最近三天和之前三天」。明确窗口须是相邻等长的 1–14 天；不匹配、含糊或超范围时返回说明，不发现候选、不调用业务工具、不生成提案。识别采用保守的固定语法，不保证理解任意改写。
- `discover_analysis_candidates` 读取当前用户的所有 active 计划，包含未登记暂停的计划。2–4 条候选可比较。超过 4 条时整轮停止比较，并提示缩小分析范围或使用手动入口复核单条计划。发现后，`text` 和历史快照含 `candidate_scope`，其中 `kind` 为 `owned_active_plans`，`max_candidates` 为 4，未超限时附本人候选 `plan_ids`。
- 主编排根据历史覆盖边界确定相邻等长的共同窗口，当前窗结束于候选最新共同覆盖日。默认每窗优先七天，覆盖边界不足时缩短；明确天数时不缩短，共同覆盖不足会说明并停止。数据 Agent 按计划和窗口查曝光、点击及日期覆盖，知识 Agent 检索指定版本规则。缺报、未映射和规则无法验证时，`evidence_gaps` 列明缺项，整轮停止比较。
- 主编排用已核验的指标和规则回执复算全部候选的 CTR 及相对降幅。角色查询全部发出后直接交回回执，不再发未消费的总结请求；仍有待发查询时可在预算内继续。已核验的 `text` 和保存的快照含 `comparison`，记录共同覆盖、候选两窗总量和 CTR、排名及运行时调用 ID。每轮至多 12 次业务工具调用、2 次分派和 6 次模型调用。唯一缺项为临时失败时按原参数补查一次。
- 证据齐全且唯一首名达到规则阈值时，`done` 携带 `review_proposal`。新的一句话诊断在此阈值下只生成复核提案。保存回复前重新核验候选范围、状态及指标来源，有变化时回滚回复与提案，SSE 返回 `error_code: "candidate_revision_changed"`。未达到阈值时提交比较结果。
- 用户确认后创建本地复核单，页面另发 GET 读回原轮证据。任务可从本人历史读回，摘要带 `mode: "natural"`。尚无可比结论时，计划、规则和日期窗为 `null`。已提交且包含比较结果的轮次可作为续问来源。

自然诊断续问请求示例：

```json
{"message":"继续这次比较，改成最近三天和之前三天","diagnosis":{"mode":"natural","task_id":"原任务 ID","source_turn_id":1}}
```

`task_id` 和正整数 `source_turn_id` 必须同时提供。未知、非本人的自然任务返回 404；来源轮次不存在、不属于该任务、未提交或无比较结果返回 400，均在取证和提案前拒绝。服务端在接受时固定来源，继承比较目标、本人 active 候选范围定义及规则版本，以当前句指定新窗口；重新发现候选、覆盖、指标和规则。续问当前支持「继续这次比较，改成最近 N 天和之前 N 天」的等长 1–14 天语法。自然诊断的 `thinking` 和 `done` 带本轮 `turn_id`；任务历史记录来源、继承字段及覆盖字段。角色只接收各自本轮查询，预算判定记录于 Trace。每轮复核保留自身快照，任务摘要仅由最新接受轮次更新。

#### 创建和续问诊断任务

请求可加 `diagnosis` 对象。创建时传 `plan_id`、`rule_id`、`baseline` 和 `current`。续问时传上轮返回的 `task_id` 和两个新日期窗，计划与规则从 SQLite 恢复。两种请求均可附非空字符串 `message`。省略时服务端使用默认复盘指令。

创建请求示例

```json
{
  "message": "复盘计划的点击率，按指定规则给出建议",
  "diagnosis": {
    "plan_id": 101,
    "rule_id": "ctr-review-v1#low-ctr",
    "baseline": {"start_date": "2026-10-19", "end_date": "2026-10-25"},
    "current": {"start_date": "2026-10-26", "end_date": "2026-11-01"}
  }
}
```

续问请求示例

```json
{
  "diagnosis": {
    "task_id": "a1b2c3d4e5f6478899aabbccddeeff00",
    "baseline": {"start_date": "2026-10-29", "end_date": "2026-10-31"},
    "current": {"start_date": "2026-11-02", "end_date": "2026-11-04"}
  }
}
```

- `plan_id` 可为正的 64 位整数或字符串。`rule_id` 和 `task_id` 为字符串。标识符字符串去掉首尾空白后长度为 1–128。
- 规则引用使用 `<规则族>-v<版本号>#<条款>`。例如 `ctr-review-v1#low-ctr` 对应知识分块的 `rule_id: ctr-review#low-ctr` 和 `version: v1`。旧的未指定版本标识仍可提交，但无法生成精确规则引用。
- 窗口使用有效的 `YYYY-MM-DD` 日期，含起止日。每个窗口的结束日须等于或晚于开始日，最多相差 90 天。两个窗口均须提供。
- 结构化字段或日期无效时返回 HTTP 422。JWT 缺少有效的正整数用户 ID 时返回 HTTP 401。任务未知或属于其他用户时统一返回 HTTP 404，响应为 `{"detail":"诊断任务不存在"}`。这些拒绝发生在诊断写入及执行之前。
- 服务端按 JWT 用户 ID 和任务 ID 隔离状态与任务对话。每轮在进入 Agent 池前保存本次接受的参数并读取独立的执行快照。回复失败或较早的请求较晚完成时，SQLite 仍保留最近一次接受的参数。
- 数据助手按 JWT 用户查询计划详情和两个日期窗的计划历史指标。本人计划关联其他用户的账户时，计划详情的 `account_name` 为 `null`。知识助手使用本轮 `rule_id` 查询规则。
- 服务端在调用前校验工具名、JSON 对象、本轮计划、日期窗和规则，拒绝模型传入 `user_id`。诊断 Trace 以 `runtime_call_id` 配对调用、结果和证据回执，`parent_call_id` 和 `retry_of` 指向本轮的运行时 ID。模型工具消息沿用服务商返回的 `tool_call_id`。每轮最多执行 12 次，编排分派、业务工具、同事调用、降级和补查共用额度。并行分派在入队前预占额度。
- 首批分派后，若唯一缺项源于临时故障，服务端用原参数补查一次。空日报、指定规则缺失或额度耗尽时，服务端停止查询并在回复中列出缺项。
- 服务端分别报告缺少成功回执与成功查询后的证据缺项。计划指标缺窗、零曝光、样本不足、两窗来源/版本不一致或缺少精确规则时列出具体缺项。旧用户日报仍不参与单计划诊断；仅调用旧日报时保留缺少计划历史 CTR 的回复。
- 诊断 `text` 事件的 `evidence_gaps` 是本轮证据缺项的字符串列表，首页按轮单独展示。
- 诊断 `text` 事件包含 `citations`。服务端仅从本轮知识工具回执中提取与快照的规则 ID 和版本均一致的分块。只召回相邻版本、相关条文或召回为空时，引用列表为空，并报告缺少精确版本证据。

诊断的 `thinking` 与终态事件同时携带 `task_id` 和 `trace_id`。续问保留原 `task_id`，每次执行生成新的 `trace_id`。下例省略了 `done` 的提案字段。

```text
data: {"type":"thinking","content":"🤔 思考中...","task_id":"a1b2c3d4e5f6478899aabbccddeeff00","trace_id":"4d93ab12e671"}

data: {"type":"text","content":"证据不足。日期报表按用户和日期汇总，缺少该计划在基准窗口和当前窗口的历史 CTR。\n本轮规则引用 ctr-review-v1#low-ctr，原文 /api/knowledge/chunks/ctr-review-v1-low-ctr-01","evidence_gaps":["缺少该计划在基准窗口和当前窗口的历史 CTR"],"citations":[{"chunk_id":"ctr-review-v1-low-ctr-01","rule_id":"ctr-review#low-ctr","version":"v1","title":"点击率下降复核","url":"/api/knowledge/chunks/ctr-review-v1-low-ctr-01"}]}

data: {"type":"done","task_id":"a1b2c3d4e5f6478899aabbccddeeff00","trace_id":"4d93ab12e671"}
```

#### 读取本人的诊断任务

两个 GET 都要求 Bearer JWT、当前已提交的会话及仍存在的账号。服务端在同一个只读 SQLite 事务内核验身份和读取任务。成功响应为 `{"code":200,"data":{...}}`，并带有 `Cache-Control: no-store`。会话或账号无效返回 HTTP 401，未知及他人任务均返回 HTTP 404 和 `{"detail":"诊断任务不存在"}`。读取历史不调用模型或写入业务数据。

| 入口 | 成功响应 `data` |
| --- | --- |
| `GET /api/diagnosis/tasks?limit=20&cursor=...` | `tasks` 为任务摘要数组，含 `task_id`、`plan_id`、`rule_id` 和任务最新接受的 `baseline`、`current`。按任务创建顺序倒序排列。 |
| `GET /api/diagnosis/tasks/{task_id}` | 同一任务摘要及按接受顺序排列的 `turns` 数组。 |

列表 `limit` 默认为 20，范围为 1–50。把上一页的 `next_cursor` 作为 `cursor` 原样传入下一页，末页返回 `null`。游标以创建时的首轮 ID 和任务 ID 为边界。新建任务或续问期间，已有任务在下一页的位置保持稳定。参数无效返回 HTTP 422。

- 每轮含 `turn_id`、`message`、`reply`、`status`、`snapshot`、`review` 和 `pause`。状态为 `committed` 或 `uncommitted`。接受时即保存日期窗和源 Trace ID，提交时随回复保存 `citations`、`evidence_gaps`、`metrics`、`calculation` 和 `rule_evaluation`。
- 旧轮优先使用已保存的原轮复核提案快照。无快照时 `snapshot` 为 `null`，仍返回已保存的提问和回复。首页把缺失字段标为不可恢复。未提交轮的 `reply`、`review` 和 `pause` 均为 `null`。
- 已提交轮的 `review` 和 `pause` 在无对应提案时为 `null`，否则提供 `action_id` 和 `state`。状态为 `pending`、`approved` 或 `unavailable`。复核提供可空的 `review_id`。暂停提供原目标 `target` 和可空的 `reason`，不可用原因包括 `sandbox_disabled`、`target_unavailable`、`status_changed`。已批准暂停保留 `approved`，当前回执因沙箱关闭或登记失效而不可读时，通过 `reason` 标明。批准仍以对应确认入口的事务校验为准。
- 首页只在用户点击时发出确认 POST。恢复已批准的复核单或可读的暂停回执时使用对应的 GET。续问仍走 `POST /api/chat/stream`，沿用原 `task_id` 并提交本次日期窗，适用于已有无版本规则标识的任务。退出或切换账号会清空诊断视图，取消旧会话请求并丢弃迟到响应。

#### 确认和回读本地复核单

本人计划的诊断回复提交后，`done.review_proposal` 携带服务端生成的 `action_id` 和绑定的 `turn_id`，以及 `task_id`、`plan_id`、`rule_id`、`baseline`、`current`、`citations`、`evidence_gaps` 和 `source_trace_id`。同时保存 `metrics`、`calculation`、`rule_evaluation`，其字段与下方计划指标证据契约一致。新的一句话诊断同时保存 `candidate_scope` 和 `comparison`。提案与回复在同一事务提交。结构化诊断允许保留证据缺项并提请人工复核，续问保留既有提案的原轮快照。

- `POST /api/diagnosis/review-proposals/{action_id}/confirm` 使用 Bearer JWT，无需请求体。首次确认在所选业务库的事务内核验提案、已提交轮次、任务和计划归属，创建本地复核单。新的一句话诊断还核验候选范围、状态和来源，变化时返回 HTTP 409，请重新诊断。确认写入复核单，计划状态保持原值。重复及并发确认返回同一回执。无权确认或提案不可用时返回 HTTP 404，写入失败时返回 HTTP 503，可用原 `action_id` 重试。
- `GET /api/diagnosis/reviews/{review_id}` 使用 Bearer JWT，按本人身份读取保存的复核单。未知或他人的复核单返回 HTTP 404。页面在确认后调用此入口展示实际回执。

两接口成功时返回 `{"code":200,"data":{...}}`，其中 `data` 包含提案的完整快照以及 `review_id` 和 UTC `created_at`。缺少有效身份时返回 HTTP 401。

#### 批准沙箱暂停并读回

暂停沙箱默认关闭。SQLite 使用独立的 `DIAGNOSIS_SANDBOX_DB_PATH` 启用，MySQL 使用上文的 R2 暂停配置。结构化诊断仅在已提交轮次证据完整、命中 `ctr-review-v1#low-ctr`，且同属主的 `active` 计划已登记时返回 `done.pause_proposal`。提案与回复同事务提交。暂停提案沿用原轮证据字段，另有独立 `action_id` 和 `target`，其中包含 `plan_id`、`plan_name` 和 `expected_status: "active"`。任务卡 22 的已有暂停动作和回执沿用下列入口。

| 入口 | 成功响应 `data` |
| --- | --- |
| `POST /api/diagnosis/pause-actions/{action_id}/confirm` | 原暂停提案字段及 `receipt_id`、`execution_trace_id`、`committed_status: "paused"`、UTC `created_at`。并发及重复批准复用同一回执。 |
| `GET /api/diagnosis/pause-actions/{action_id}` | `{"receipt":{...},"actual_status":"paused"}`。`receipt` 是已提交的执行回执，`actual_status` 来自本次查询的计划行，可能随之后的状态操作变化。 |

两个入口都要求有效 Bearer JWT，无需请求体。确认在所选业务库的写事务内重查当前会话、目标属主、登记身份及预期状态。沙箱关闭、未知动作、跨用户、登记已失效或 GET 尚无回执时返回 404。会话失效返回 401。待审批目标的状态已变化时返回 409。已确认回滚的写入故障返回 503，可用原动作 ID 重试。页面批准后单独调用 GET。

#### Provider 与联调契约

接口字段沿用上述契约。模型配置由 `Settings.llm_config()` 集中解析。DeepSeek 使用 `MODEL`、`BASE_URL` 和 `API_KEY`（为空时用 `DEEPSEEK_API_KEY`）。OpenAI 使用 `OPENAI_MODEL` 和 `OPENAI_API_KEY`。智谱 GLM 使用 `GLM_API_KEY` 和 `GLM_MODEL`，默认 `glm-4.5-air`，连接 BigModel 官方接口。规划、主编排、子 Agent、动态 Worker、同步和流式降级使用所选 Provider 的模型快照。

从仓库根目录执行 `python -B -X utf8 -m scripts.run_real_diagnosis --embedding fixed` 可用隔离库和测试用户走同一 ASGI 登录、SSE、确认与回读路由。每轮保留真实 SDK 请求模型、工具 schema/回执及 Trace，收到终态时立即回读数据库，验证回复和提案已提交。续问换池恢复原任务；确认第一轮后 GET 必须仍返回第一轮窗口、引用、缺项和源 Trace。没有新增联调专用 HTTP 接口。

已用真实 DeepSeek `deepseek-flash` 完成公开计划指标两轮、第一轮确认与回读。固定嵌入用于隔离检索依赖。真实模型与离线 SDK 捕获分别记录。 [独立联调脚本](scripts/run_real_diagnosis.py)

#### 计划历史指标工具与诊断证据

`get_plan_metrics_by_date(plan_id, start_date, end_date)` 是 Agent 内部只读工具，无新增匿名 HTTP 查询入口。模型菜单只接受这三个字段；`user_id` 从认证上下文注入。日期含起止日，最多相差 90 天。先查询本人 `ad_plans`，再读取服务端 `plan_metric_bindings` 的来源、版本及来源计划 ID，最后通过 `PLAN_METRICS_DB_PATH` 以 SQLite `mode=ro`、`query_only=ON` 查询独立产物。模型不能传身份、来源计划或文件路径。旧日报工具及 MCP schema 不变。

成功回执包含 `success`、`scope: plan`、`plan_id`、`source_id`、`source_plan_id`、`data_version`、`source`、`start_date`、`end_date`、`coverage`、`impressions`、`clicks`、`daily`。`source` 含公开/合成类型、官方链接、许可、压缩文件及 TSV 哈希、采样说明和时间映射；`coverage` 含完整标志、预期与观测天数及缺失/不完整日期。无事件且处于完整观测区间的来源计划日写入零值；窗口超出完整覆盖范围明确列为缺项，不能将未知数据当作零曝光。

失败回执为 `success:false,error_code:...`，区分 `authentication_required`、`plan_not_found`、`invalid_date_window`、`plan_history_not_mapped`、`plan_history_not_configured`、`source_version_mismatch`、`plan_history_unavailable`。无权计划在打开历史产物前即拒绝。

诊断 `text` 与原轮复核快照新增以下字段：

- `metrics.baseline/current`：本轮服务端实际工具回执，含数字、覆盖和来源。不能用模型生成数字代替。
- `calculation`：总点击/总曝光计算的 `baseline_ctr_pct`、`current_ctr_pct`、`decline_pp`（基准减当前的百分点）和 `relative_decline_pct`（相对基准的下降百分比）；基准 CTR 为零时相对降幅为 `null`。
- `rule_evaluation`：仅证据完整、窗口可比、来源一致且样本达标时生成。含精确规则标识、条文原文、SHA256、分块 ID、最小曝光、相对降幅门槛及 `matched`。否则为 `null` 并填写 `evidence_gaps`。v1 为两窗各至少 1000 曝光且相对下降至少 20%，v2 为各至少 2000 且至少 15%；条文须与本轮精确版本检索原文一致。未知版本不套用 v1。阈值以整数分数比较，显示数字才转浮点数。

首页每轮显示数字、来源、日期映射、缺项及可回查引用；第二轮后确认第一轮时，确认及 GET 均只返回第一轮已提交快照。

### GET /api/chat/history
获取 token 对应用户的普通对话历史（Bearer）。可选查询参数 `exclude_chat_view_id` 排除该页面标识对应的消息，历史中没有页面标识的旧消息仍可返回。结构化诊断历史使用[本人诊断任务详情](#读取本人的诊断任务)。

### 工作台广告搜索词诊断

以下 SQLite 工作台接口以 `/api/chat/conversations/{conversation_id}/search-terms` 为前缀，需要该会话属主的 Bearer JWT。入口位于会话输入框上方的「上传广告报表」。

| 方法和路径 | 请求与结果 |
| --- | --- |
| `GET /shops` | 列出当前账号获准导入报表的美国站店铺。 |
| `POST /reports?filename=report.xlsx` | 请求体为 `.xlsx` 或 UTF-8 `.csv` 的原始字节，大小上限 16 MB，超限返回 `413`。支持美国站 Sponsored Products 搜索词报表及既有测试 CSV。可用 `shop_id` 选择授权店铺。官方格式仅有一个授权店铺时可省略该参数，多店时必须选择。格式、字段或归属不符返回 `422`。成功返回 `201` 和 `{report, products}`，含导入版本、粒度、日期、活动、广告组及可选商品。会话内同一店铺重传相同文件复用原导入版本。 |
| `GET /reports`、`GET /reports/{import_id}` | 列出近期导入或重新读取指定报表范围，详情读取会复核商品归属。 |
| `POST /runs` | 请求含 `import_id`、`message`，可选 `batch`，默认从第 1 批开始，每批至多 24 个活动、广告组与搜索词组合。测试 CSV 须传 `sku`。官方格式可省略 `sku` 查询广告组级指标。服务端核验登记范围、复算指标并校验模型建议及原文，提交后返回 `run_id` 与 `trace_id`。会话历史记录实际执行阶段和诊断卡。 |
| `POST /runs/{run_id}/rerun` | 无请求体。复用原轮的报表导入、批次、SKU 和问题，从当前资料和标题生成新一轮，返回新的 `run_id` 与 `trace_id`。 |
| `GET /runs/{run_id}/items/{item_id}` | 返回该卡绑定的报表版本、原始行及指标、标题和资料快照、决策、人工投放复核待办或最新通过校验的标题草案。 |
| `PUT /runs/{run_id}/items/{item_id}/draft` | 请求含 `title` 与上次读取的 `revision`。成功后返回已保存正文及修订号。 |
| `GET /runs/{run_id}/items/{item_id}/draft/copy?revision=2` | 核验指定修订号，返回允许复制的已保存正文及修订号。 |

`POST /runs` 和 `POST /runs/{run_id}/rerun` 接受同样格式的可选 `Idempotency-Key`。键按认证账号、工作群和动作隔离，诊断比较 `import_id`、`sku`、`batch` 和 `message`，重新诊断比较原轮 ID。相同请求重发返回原 `run_id`、`trace_id`、`replayed: true`、`status` 和 `history_url`。运行中返回 HTTP 202，终态返回 200。已提交时附原答案的 `entry_id`，详情经原 item GET 读取。重发和结果回查仍核验当前会话、群属主和店铺读权。无键沿用逐次受理，新的重新诊断操作使用新键并核对当前资料。

保存和复制均重新核对原店铺、站点、SKU、资料内容及版本、原标题正文及版本。证据过期返回 `409`，其中 `detail.code` 为 `stale_title_draft`，`detail.reasons` 列出 `facts_changed`、`title_changed` 或 `product_unavailable`。草案修订号冲突也返回 `409`，提示重开卡片。标题校验失败返回 `422` 和具体问题。

报告归属由账号下登记的测试店铺、活动、广告组和商品校验。混合广告组缺少逐行映射的组合保留广告组汇总。当前规则 `amazon-us-non-media-2026-07-27` 要求至少 100 次曝光和 20 次点击，标题候选须至少 2 单且转化率至少 10%。CTR、转化率、CPC 和 ACoS 均按总量计算，比率分母为零时返回 `null`。标题上限为 75 字符（含空格），标题词汇和商品声明须有原标题或已核验资料依据。会话写入限于导入、诊断、草案与人工待办。

官方字段夹具的尺寸、兼容性和清洗限制诊断在详情中返回 `item.demand`、`item.source_chunks`、`item.citations` 和 `item.model_validation`。证据不足或模型校验失败的卡片显示继续观察，草案编辑和复制入口返回 `404`。这类草案的编辑和复制也复核当前报表归属，变更时 `409` 的原因可为 `ownership_changed`。冻结合成夹具位于 `tests/fixtures/search_terms_generalization/`，真实模型可通过验收脚本独立记录。

适用证据诊断的多条目批次使用 v22 模型协议，逐项回传已解析需求、样本审计与分类引用。单条目沿用 v11。服务端独立核对条目和引用；批量内部协议与上述 HTTP 响应字段分开。真实 GLM 验收覆盖每批 5–6 项。

适用 v22 的 24 项满批默认先发一次请求。流式读取超时、截断或未正常结束时弃用该响应，在预算内最多另请求四批，每批六项。服务端在构造 SDK 请求前、返回后及提交事务内核验权限、版本和广告归属，按报表文件及物理行核对重组条目。失败或失效条目可返回 `action: "incomplete"`、`code: "diagnosis_incomplete"` 和 `incomplete_cause`，无草案或复核待办。整轮权限失效时返回 `409`。主动串行 4×6 由验收脚本启用。

商品归属失效的详情可按经核验的完整报表行返回 `item.attribution: "ad_group"`，`item.metrics` 和 `rows` 同属当前广告组口径。无法核验整组时返回 `item.attribution: "unavailable"`、`item.metrics: null` 和 `item.metrics_unavailable_reason`。已保存且可读取的原商品指标通过 `item.historical` 和 `historical_rows` 单列。失效项保持 `incomplete`，无新草案或复核待办。

对于归属或报表范围已失效且缺少整组核验结果的旧诊断，通用 `GET /api/chat/conversations/{conversation_id}/runs/{run_id}/evidence` 的 `data.items[]` 与专用详情的 `item` 均返回 `attribution: "unavailable"`、`metrics: null` 和空的 `row_locations`。可识别的原商品指标通过各自的 `historical` 字段返回。

在仓库根目录启动手动测试，准备仓库现有 GLM 配置，执行 `python -m scripts.accept_search_terms demo --output-dir logs/search-terms-demo`。用控制台给出的临时测试账号进入工作台，新建会话后上传 [冻结报表](tests/fixtures/search_terms/report.csv)，选择 `SP-LUNCH`，并提交 [冻结题面](tests/fixtures/search_terms/frozen.json)。验收依次执行 `python -m scripts.accept_search_terms fixed --output-dir logs/search-terms-run` 和 `python -m scripts.accept_search_terms live --output-dir logs/search-terms-run`，使用已安装的 Edge 和仓库 Playwright 运行环境。每次选择新的输出目录。

### WebSocket /ws/chat
路由约定连接后发送 `{ "token": "...", "message": "今天 ROAS 多少" }`，响应消息类型为 `thinking`、`text`、`error` 或 `done`。该兼容路由的鉴权和连接生命周期尚待验证。工作台使用 `POST /api/chat/stream`。

## 健康检查

### GET /api/health
无需登录，返回 HTTP 200，`data.status` 为 `ok`，`data.version` 为 `3.0.0`。

### GET /api/ready
无需登录。启动完成、JWT 可用且基础存储检查通过时返回 200 和 `data.status="ready"`。暂不可用时返回 503 和 `data.status="not_ready"`。该探针只检查应用基础就绪。[探针与环境说明](DEPLOY.md#存活与就绪)

## 知识库

以下知识库接口无需登录。

### GET /api/knowledge/search?q=ROAS
使用查询参数 `q` 搜索知识库，最多检索 5 个分块，返回 `results`、`sources` 和结构化 `evidence`。证据包含 `chunk_id`、`content` 和分块已有的 `rule_id`、`version`、`title`、`source`。

完整版本化规则标识（如 `ctr-review-v1#low-ctr`）按 `rule_id` 和 `version` 精确查找，未命中时返回空结果。自由文本使用向量与关键词混合检索。

### GET /api/knowledge/chunks/{chunk_id}

按引用中的 `chunk_id` 读取原文和证据字段。`content` 是 Chroma 中保存的完整分块文本。分块不存在时返回 HTTP 404。

### GET /api/knowledge/stats
返回条目数、来源数和类别数。

### GET /api/knowledge/all
返回知识条目摘要、来源和类别。

## 用户

### GET /api/users
读取用户目录（Bearer）。服务端按当前数据库角色授权。有效会话的 `admin` 和 `boss` 可读取按 ID 升序排列的 `data` 数组，每项仅有 `id`、`username`、`role`。普通用户及其他角色返回 HTTP 403，无有效会话或会话用户已删除时返回 HTTP 401。改角色后现有会话按新角色判定。
