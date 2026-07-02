# 智能广告投放助手

## 项目概述
基于 Flask + DeepSeek API 的广告投放管理平台，支持多账户权限管理、数据看板、AI 智能分析等功能。

部署文档：[DEPLOY.md](./DEPLOY.md)

## 快速启动

```bash
cd ad_agent_backend
pip install flask pyjwt pillow python-dotenv openai
python server.py
```

访问 http://localhost:5000

## 预设账户

| 用户名 | 密码 | 角色 |
|--------|------|------|
| boss | admin123 | 老板 |
| admin | admin123 | 管理员 |
| zhangsan | user123 | 用户 |
| lisi | user123 | 用户 |

## 项目结构

```
ad_agent_backend/
├── server.py                   # Flask 服务入口 + 蓝图注册
├── gen_all.py                  # 前端 HTML 生成器(输出到static/)
├── .env                        # 环境配置 (API Key等)
├── ad_agent.db                 # SQLite 数据库
├── index.html                  # 旧版前端(兼容)
├── backend/
│   ├── memory/memory_manager.py  # 树状记忆系统
│   ├── memory/user_memory/       # 用户记忆文件
│   ├── static/                 # 静态资源目录
│   │   ├── index.html          # 前端单页应用(生成)
│   │   ├── css/
│   │   ├── js/
│   │   └── img/
│   ├── templates/              # Flask 模板目录
│   ├── routes/                 # 模块化路由蓝图
│   │   ├── __init__.py         # 蓝图注册入口
│   │   ├── shared.py           # 蓝图共享状态
│   │   ├── auth_routes.py      # 认证/验证码路由
│   │   ├── user_routes.py      # 用户管理路由
│   │   ├── dashboard_routes.py # 仪表盘路由
│   │   ├── plan_routes.py      # 投放计划路由
│   │   ├── account_routes.py   # 预算管理路由
│   │   ├── alert_routes.py     # 告警中心路由
│   │   ├── chat_routes.py      # AI聊天路由
│   │   ├── log_routes.py       # 操作日志路由
│   │   ├── material_routes.py  # 素材管理路由
│   │   ├── health_routes.py    # 健康检查路由
│   │   └── decision_routes.py  # AI决策路由
│   ├── agent/agent.py          # DeepSeek AI 代理
│   ├── auth/auth.py            # JWT 认证
│   ├── captcha/captcha.py      # 验证码生成
│   ├── config/config.py        # 配置加载
│   ├── database/database.py    # 数据库初始化
│   ├── di.py                   # 依赖注入
│   ├── health/health.py        # 健康检查
│   ├── mock_ad_data/           # 模拟广告数据
│   └── tools/tools.py          # 24个AI函数工具
```

## 解耦架构说明

### 后端模块化（Flask Blueprints）
所有 API 路由已拆分为独立的蓝图文件，每个文件只负责一类功能：

| 文件 | 路由前缀 | 功能 |
|------|----------|------|
| `auth_routes.py` | /api/captcha, /api/login, /api/register | 验证码、登录、注册 |
| `user_routes.py` | /api/users | 用户CRUD |
| `dashboard_routes.py` | /api/dashboard, /api/daily_reports | 仪表盘 |
| `plan_routes.py` | /api/plans | 投放计划 |
| `account_routes.py` | /api/accounts | 预算管理 |
| `alert_routes.py` | /api/alerts | 告警中心 |
| `chat_routes.py` | /api/chat | AI聊天 |
| `log_routes.py` | /api/logs | 操作日志 |
| `material_routes.py` | /api/materials | 素材管理 |
| `health_routes.py` | /api/health | 健康检查 |
| `decision_routes.py` | /api/decisions, /api/timeline | AI决策 |

**优势**：
- 每个蓝图互相独立，一个路由崩溃不影响其他路由
- 可单独测试、单独修改
- 添加新功能只需新增蓝图文件，不改动现有代码

### 共享状态
`routes/shared.py` 统一管理各蓝图间的共享依赖（数据库连接、认证实例、AI Agent等），
避免循环导入问题。

---

## 迭代记录

### 第1版 - 项目重建
- 重新构建完整后端结构
- 生成前端单页应用(index.html)
- 添加缺失API路由(decisions, timeline, materials POST)
- 修复首页路由(404问题)

### 第2版 - 权限与UI优化
- 预算管理页面补充充值按钮
- 角色权限前端控制(boss/admin/user)
- 操作日志按角色查询

### 第3版 - 验证码修复
- 修复ECharts CDN阻塞页面加载问题
- 将ECharts移到页面底部异步加载
- 验证码改用 fetch + blob URL 方式加载
- 修复captcha_id与验证码图片不匹配的bug

### 第4版 - 修复JavaScript SyntaxError
- 修复 `gen_all.py` 中 `rechargeAccount` 函数的 `\n` 被Python解释为实际换行符的问题
- 将 `\n` 改为 `\\n`，使JavaScript正确识别为换行转义
- 重新生成 `index.html`，`prompt()` 调用现在正确在一行内


### 第5版修复 - 登录500错误修复
- **修复循环导入**：将蓝图定义移到独立文件 lueprints.py，避免 __init__.py 与路由文件之间的循环依赖
- **禁用Flask重载器**：use_reloader=False 防止服务器重启过程中请求500错误
- **清除旧文件**：删除根目录旧 index.html，避免新旧文件冲突

### 第5版 - 项目解耦模块化（防崩溃）- **修复循环导入**：将蓝图定义移到独立 lueprints.py 文件，避免循环依赖
- **禁用Flask重载器**：use_reloader=False 防止服务器重启时请求500错误
- **清除旧文件**：删除根目录旧 index.html，避免新旧文件冲突
- **新增 ackend/routes/blueprints.py**：集中管理所有Blueprint对象
- **更新 
outes/__init__.py**：从 lueprints.py 统一导入并注册
- **server.py**：添加 use_reloader=False 参数
- **后端路由解耦**：将 `server.py` 中31条API路由拆分为11个独立的Flask蓝图文件
  - 创建 `backend/routes/` 目录，每个功能一个文件
  - 通过 `routes/shared.py` 集中管理共享状态
  - 通过 `routes/__init__.py` 统一注册蓝图
- **server.py瘦身**：从356行减少到60行，只保留初始化、CORS和入口
- **静态文件分离**：`index.html` 输出到 `backend/static/` 目录
- **错误隔离**：单个路由蓝图的崩溃不会影响其他功能模块
- **扩展友好**：新增功能只需新建蓝图文件 + 在__init__.py注册

## 已知未做
- ~~单元测试~~ ✅ tests/ 目录，3个文件14个用例
- ~~部署文档~~ ✅ [DEPLOY.md](./DEPLOY.md)
- 数据迁移脚本
- 第三方广告平台API对接
- 支付接口对接
- 前端静态资源独立(目前仍在gen_all.py中生成)

## 权限系统

| 页面/功能 | 老板(boss) | 管理员(admin) | 用户(user) |
|-----------|-----------|--------------|------------|
| 仪表盘 | ✅ | ✅ | ✅ |
| 投放计划 | ✅ | ✅ | ✅ |
| 预算管理 | ✅ | ✅ | ✅ |
| 告警中心 | ✅ | ✅ | ✅ |
| 素材管理 | ✅ | ✅ | ✅ |
| AI投放助手 | ✅ | ✅ | ✅ |
| 账户管理 | ✅ | ✅ | ❌ 隐藏 |
| 操作日志 | ✅ 看全部 | ✅ 看admin+user | ✅ 只看自己 |
| 创建用户 | ✅ 全部角色 | ✅ 只能建user | ❌ 无权限 |
| 删除老板 | ❌ 不可删除 | ❌ 无权 | ❌ 无权 |

---

## 近期迭代记录

### 第6版 - 3个Bug修复 + 单元测试（2026-07-01）
- **Bug 1 仪表盘500**：代码和数据库正常，重启服务器解决
- **Bug 2 AI搜索趋势"数据不足"**：替换Google Trends为百度搜索估算，沙箱不可用时智能模拟
- **Bug 3 标记已读变全部已读**：增加 alert_id 字段兼容，单条标记功能修复
- **单元测试**：创建 tests/ 目录，3个文件14个用例覆盖仪表盘/登录/工具函数
- **新增字段**：后端返回 total_orders 和 cpa，修复前端显示 ¥undefined

### 第7版 - 树状记忆系统（2026-07-01）
- **树状记忆**：创建 backend/memory/memory_manager.py，按4个分支（平台账户/最近操作/关注偏好/告警与问题）存储用户操作记录
- **自动学习偏好**：统计用户操作频率，累计3次自动推导偏好，写入【用户偏好】分支
- **记忆注入**：每次聊天时把记忆树格式化注入系统提示词，AI知道用户历史操作
- **持久化存储**：每个用户独立 JSON 文件（backend/memory/user_memory/user_{id}.json）

### 待做功能
- 概率匹配升级偏好学习（当前是硬阈值，改为按占比百分比）
- 时间衰减机制（越早的记录权重越低）
- 分支间关联分析（发现用户行为模式）
- Git 版本管理
- SQL 巩固
