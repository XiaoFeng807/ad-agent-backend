# 项目架构

StorePilot 通过 FastAPI/Uvicorn 提供工作台、REST、SSE 和兼容 WebSocket。工作台的普通聊天和业务任务由独立的 Agent 委派运行时处理。 [API 契约](API.md) · [浏览器实拍](docs/SHOWCASE.md)

## 项目地图

| 位置 | 职责与主要入口 |
| --- | --- |
| [fast_server.py](fast_server.py) · [backend/static/](backend/static/) | 唯一应用启动入口 `fast_server:app`，定义 REST、SSE、WebSocket，并提供静态工作台和订单页。 |
| [backend/routes/](backend/routes/) | FastAPI 共用状态、订单和商品资料子路由。另有历史蓝图模块，部分仍用于兼容认证和离线验收。 |
| [backend/agent/](backend/agent/) · [backend/prompts/](backend/prompts/) | 编排、角色提示词、模型适配、Agent 池、意图及上下文处理。`multi_agent.py` 仍提供兼容导出。 |
| [backend/tools/](backend/tools/) · [backend/database/](backend/database/) | 运营工具、计划与订单数据、工作群轮次、Listing 证据和审批、SQLite 迁移及种子数据。 |
| [backend/auth/](backend/auth/) · [backend/captcha/](backend/captcha/) | JWT 登录与校验、验证码。主入口部分路由另有鉴权辅助函数。 |
| [backend/rag/](backend/rag/) · [backend/knowledge_base/](backend/knowledge_base/) | BGE 嵌入、ChromaDB 知识检索和知识数据。 |
| [backend/memory/](backend/memory/) | 对话重要性评分、分层记忆及历史向量检索。 |
| [配置](backend/config/config.py) · [依赖提供器](backend/di.py) | Pydantic Settings、数据库和工具等共享依赖。 |
| [scripts/](scripts/) · [tests/](tests/) | 数据及维护脚本、聚合测试入口。具体测试位于各后端模块的 `tests/`。 |

## Web 与 Agent 调用链

- **启动与业务请求。** SQLite 模式的 FastAPI lifespan 调用 `init_db()`、`seed_data()` 和模拟日报生成器。数据路由调用业务工具或访问 SQLite。身份从经认证的会话取得，业务查询按属主、角色或授权店铺限制范围，具体契约见 [API](API.md)。
- **首页对话。** `index.html` → `POST /api/chat/stream` → 校验 Bearer Token → 从 `AgentPool` 获取 `PipelineCoordinator` → 写入用户消息及获取优化上下文 → `chat_stream()` → SSE 事件及回复存储 → `finally` 归还实例。首页可并发请求历史与发送消息，历史读取排除该页已渲染的消息。[历史接口](API.md)
- **普通聊天与工作群 ReAct。** `PipelineCoordinator.chat_stream()` 交给编排器。主模型可直接作答、发起只读查询或填写任务单。直接查询与委派可共存于一个模型响应，服务端按原始调用 ID 回送结果。每批结束后，`HomeReAct` 根据经准入的证据、已知目标及共享模型、工具和时间额度作继续判断。连续两轮无新增证据时收尾，首次可纠正错误有一次反馈机会，专用补查沿用原准入。可选规划提炼用户明确的目标和条件。同步 `PipelineCoordinator.chat()` 另有关键词管线入口。
- **请求内任务与产物。** `HomeReAct` 装配认证身份、工具白名单和请求内 `DelegationRun`。运行时校验依赖与引用，按任务装配临时 Worker。依赖方在上游任务完成后取得声明引用的已验收产物。追加复用同一看板。回执按工具证据、模型总结和缺项分别投影，仅向主模型提供当批字段和产物标识。旧助手、同事调用和降级读取共用首页执行额度。对话记忆另有独立的消息边界校验。 [工具接入](backend/agent/home_react.py) · [委派运行时](backend/agent/team_runtime.py)
- **同轮重复动作。** 主 Agent 与 Worker 使用 `RunReadCache` 按业务参数串行化读取、原参补查和回执发布。重复读取优先使用通过当前属主与范围核验的原产物。派单有明确 `read_scope` 时，调度器按授权能力、读取范围、依赖和已接收产物判断是否需要创建 Worker。群历史保留运行提示，Trace 的 `business_read`、`tool_reuse`、`duplicate_dispatch` 和 `react_decision` 关联同一轮。
- **取消与回收。** SSE 断线和发送失败触发请求级取消。主模型与 Worker 的模型请求和读取共用计数及截止时间。运行时关闭新调用和回执准入，等待在途同步工具退出，回收 Worker，再归还池中的编排器。首页 Trace 用分派运行 ID 关联 Worker 生命周期、原始工具调用及回执。助手回复提交后才发送唯一 `done`。
- **一句话投放诊断。** SSE 的 `natural` 模式进入 `NaturalDiagnosisExecution`。主编排发现本人 active 计划，以最新共同覆盖日为锚点确定相邻等长窗口，每窗优先七天。数据 Agent 仅接收计划和日期清单，可查两窗指标和覆盖。知识 Agent 仅接收指定版本规则 ID，可查该规则。两角色并行调用现有 Provider，各自按供应商原始 `tool_call_id` 拼接工具消息。角色上下文只包含当前任务及本角色的取证消息。[编排实现](backend/agent/natural_diagnosis.py)
- **自然诊断证据交接。** 每轮保存独立的运行时调用序列，按分派 ID 和工具参数采纳结构化回执。指标校验范围、日期、覆盖及来源绑定。知识回执核对规则版本、原文和可读回分块。唯一缺项为临时失败时，主编排在原分派下按相同参数补查一次。业务工具共用 12 次额度，含发现和提交。每轮至多两次分派、六次模型请求，单角色至多三次。Trace 可关联分派、模型调用、工具结果、证据采纳、补查和停止原因。
- **自然诊断比较和审批。** 主编排用服务端校验过的回执复算 CTR 和排名。缺报或指定版本不可验证时，报告缺项并停止。保存比较和首次确认复核时重查候选状态和指标来源。命中阈值时保存快照和复核提案，确认后由独立 GET 返回原轮证据。暂停资格由独立的沙箱链路处理。[接口](API.md)
- **兼容 WebSocket。** `/ws/chat` 的鉴权和 Agent 池生命周期待验证。工作台使用 SSE。

## 商品运营

1. **会话与商品识别。** `POST /api/chat/stream` 在 SQLite 工作群中识别商品需求并选择业务路径。请求轮次带有认证用户、会话 ID、运行 ID 和统计日期。服务端从授权目录解析商品，或沿用本会话已选的商品。匹配不明确时要求用户补全店铺、站点或 SKU。 [商品入口与取证](backend/agent/listing_run.py)
2. **销量与巡检。** 已选商品的单窗销量请求直接读取。多商品使用相同日期窗，区分已覆盖日期的零销量和缺日报。只读巡检对同店同站的三个 SKU 查询销量、现行 Listing 与生效事实，按商品形成证据与规格冲突清单。需要委派时，运行时只向 Worker 开放本轮授权工具和读取范围；产物交接重查 SKU、参数、来源和运行 ID。固定回放验证了商品巡检的 Worker 交接，实测 GLM 巡检样本均选择直查。 [巡检实现](backend/agent/listing_inspection.py) · [巡检实拍](docs/SHOWCASE.md)
3. **草案与审批。** 改写请求只从生效事实的措辞选项中生成英文五点。服务端校验来源及版本，缺项时只给分析或澄清。证据完整的草案绑定源会话、轮次、目标 SKU 和当前版本。批准在写事务内核对所有权、来源和版本，只修改本地测试 Listing 五点并同时保存回执。页面另发 GET，按同一动作 ID 读回实际版本与历史回执。 [工作群证据存储](backend/database/chat_groups.py) · [Listing 写入与回执](backend/database/listing_operations.py)
4. **运营巡检与下一周期复盘。** 同一工作群轮次统一查询三款的完整七日窗，用配置阈值确定需要核对 Listing 与商品事实的候选。销量排序由服务端复算，主 Agent 可以直查或委派取证。唯一优先项的草案保存遗漏事实 ID 和基准窗。用户从已执行回执打开复盘时，服务端在只读事务内核对会话、动作和回执，再查询审批日之后的七个完整日。

## 知识检索

- 知识搜索端点与 Agent 知识工具调用 `RAGKnowledge.generate()`。ChromaDB 提供 BGE 向量召回，TF-IDF 索引从持久分块重建，RRF 按分块 ID 合并两路结果。知识工具保留上下文与来源，并返回结构化证据。BGE 模型为 `BAAI/bge-small-zh-v1.5`，输出 512 维，默认离线加载。
- 商品资料的发布版本、源文本、定位和 BGE 向量存入 SQLite。检索在同一读取快照内按商品目录授权和有效版本选出候选，再执行范围内向量评分。聊天默认保留原问题候选，并用属性别名和子问题补查，再逐项校验引用。回查原文时重新校验授权及有效版本。GPU 重排可按账号灰度，默认关闭。 [聊天检索](backend/rag/product_chat_search.py) · [资料导入](scripts/product_evidence.py)
- `recall()` 提供向量召回、可用时的 TF-IDF 加余弦相似度召回，以及 RRF 融合。`causal_retrieve()` 使用该方法进行因果查询展开和排序，其调用未见于主知识搜索路径。

## 数据与外部依赖

| 数据或依赖 | 当前实现 |
| --- | --- |
| 业务 SQLite | 默认使用根目录 `ad_agent.db`。含用户与会话、对话、账号、计划、日报、告警、素材及日志等表。启动时为旧对话表补齐 `priority` 与 `chat_view_id`。演示进程可用 `DIAGNOSIS_SANDBOX_DB_PATH` 选用独立业务文件。 |
| 商品与 Listing | 按店铺、站点和 SKU 组织商品及版本。销量、事实、草案、批准、独立读回和复盘通过当前会话鉴权。纯巡检将结论和证据随工作群回复保存。有待审批改动时同事务保存提案，批准后写入五点、版本和回执。 |
| 计划历史与映射 | `plan_metric_bindings` 将本人本地计划映射到来源 ID、精确数据版本和来源计划。查询先验证 `ad_plans.user_id`；独立 SQLite 由服务端 `PLAN_METRICS_DB_PATH` 选择，以 `mode=ro` 及 `query_only=ON` 打开。模型不接受身份、来源计划或路径。公开数据导入器只保留计划每日曝光与点击；原始文件不加入 Git。 |
| 诊断任务 SQLite | `diagnosis_tasks` 按属主和任务 ID 保存计划、规则及最新接受的日期窗。`diagnosis_turns.snapshot` 在接受轮次时记录原日期窗和源 Trace，证据和回复同事务提交。启动迁移保留旧轮，读取时可用原轮复核提案快照补齐。 |
| 本地复核 SQLite | `diagnosis_review_proposals` 与本人计划的诊断回复同事务提交，绑定原轮及证据快照。首次确认以 `BEGIN IMMEDIATE` 核验归属并写入 `diagnosis_reviews`，以唯一 `action_id` 复用回执。按认证用户及 `review_id` 读回保存的快照。 |
| 沙箱暂停 SQLite | `diagnosis_sandbox_targets` 记录经本地登记的目标和属主。删除或转移计划会使原登记失效。`diagnosis_pause_proposals` 将独立动作 ID、登记和预期状态绑定到原轮的复核快照。`BEGIN IMMEDIATE` 内校验会话和目标，定向更新计划并提交唯一 `diagnosis_pause_receipts`。GET 同时返回原回执和计划的当前状态。 |
| L1 对话窗口 | `compose_context()` 调用 `build_important_window()`，最多取六轮并考虑内容重要性。 |
| L2 短期上下文 | 首页 SSE 从本次消息 ID 截止并经裁剪的 SQLite 历史提取短期问答。排除本次用户消息后，最多取 16 条，主编排、子 Agent、同事调用与降级共用。旧入口可继续使用 30 分钟的 JSON 短期摘要。 |
| L3 与 L4 用户记忆 | 普通实例在 `backend/memory/user_memory/` 下按用户保存 JSON。沙箱使用解析后的 SQLite 文件相邻的 `<SQLite 文件名>.memory/user_memory/`，画像和业务记忆分别配置 30 天和 90 天。缺少消息边界，首页传入短期快照时保守排除，避免同用户后发请求的工具事实进入先发请求。旧入口保留原契约。 |
| 对话向量记忆 | 普通实例使用 `data/conv_memory/`，沙箱使用解析后的 SQLite 文件相邻的 `<SQLite 文件名>.memory/conv_memory/`。ChromaDB `conversations` 集合使用 BGE。召回应用 30 天期限。首页普通回复在同一次工作线程调用中先提交 SQLite，再索引，SQLite 用户消息 ID 为幂等键。schema v2 保存问题和回复 ID。查询及回执核验均限定认证用户和本轮 SQLite 消息边界，并复核原文。 |
| 知识向量库 | 默认使用 `data/rag_db/` 的 ChromaDB `ad_knowledge` 集合，可用 `RAG_DB_PATH` 指向单独的演示库。知识索引与会话记忆分别配置。 |
| LLM | `Settings.llm_config()` 集中解析 Provider、模型、密钥和地址；`ProviderFactory` 按配置快照缓存 DeepSeek、OpenAI、GLM 或 Mock。主编排、临时 Worker 和降级路径从所选 Provider 取模型。SDK 的调用超时还受本轮执行预算约束。 |

本地和独立演示实例的启动与数据配置见 [部署说明](DEPLOY.md) 和 [异地演示](docs/REMOTE_DEMO.md)。

首页诊断用本人任务列表和详情恢复轮次。GET 在同一 SQLite 只读事务内校验会话、账号和属主。详情按持久快照展示历史，检查待批提案的当前可用性。已批准的暂停保留历史状态，同时检查原登记和目标归属以确定当前回执能否独立 GET。退出或切换账号时清空视图并取消旧请求。[接口契约](API.md#读取本人的诊断任务)

## Provider 验收边界

[离线 SDK 回归](backend/agent/tests/test_provider_contract.py) 使用真实 Provider 和 OpenAI SDK，将 HTTP 传输替换为本地捕获，核对实际请求模型、工具定义和按原 ID 回喂的工具结果。[真实联调入口](scripts/run_real_diagnosis.py) 不替换模型，先建立独立 SQLite、测试用户与演示规则库，再经现有 ASGI 路由完成两轮、确认第一轮及回读。请求与响应钩子只记录允许的模型和工具字段；自由格式日志丢弃，归档 Trace 和报告统一脱敏。

离线链路与真实模型分开报告。广告指标与原轮复核回读已用真实 DeepSeek 验证。总点击/总曝光、百分点差、相对降幅及指定规则版本的判定从服务端实际回执产生。证据与规则版本不一致或缺项时停止判定。SSE 将 `metrics/calculation/rule_evaluation` 连同引用、缺项传至同事务保存的原轮快照。 [真实 GLM 协作与商品审批实拍](docs/SHOWCASE.md)


TASK-024：自然诊断可指定同任务已提交的比较轮次作为来源；接受事务固定继承的比较定义和规则，执行重新发现候选并按角色最小字段取证。每轮证据和提案独立保存，摘要只允许最新接受轮次更新。
