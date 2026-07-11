# API 接口文档

## 认证

### POST /api/login
登录并获取 token。

参数：`{ "username": "boss", "password": "admin123" }`
返回：`{ "token": "...", "role": "boss" }`

### GET /api/captcha
获取验证码图片（不存 token 可用）。

## 数据

### GET /api/dashboard
获取仪表盘概览数据。

### GET /api/daily_reports?days=7
获取每日报告。

### GET /api/plans
获取广告计划列表。

### GET /api/plans/:id/toggle
暂停/开启广告计划。

### GET /api/alerts
获取告警列表。

### GET /api/accounts
获取广告账户列表。

### GET /api/materials
获取素材列表。

### GET /api/decisions?days=7
获取决策记录。

### GET /api/logs?days=7
获取操作日志。

### GET /api/timeline
获取时间线。

### GET /api/trends?keyword=xxx
获取搜索趋势。

## AI 对话

### POST /api/chat/stream
流式 AI 对话（SSE 协议）。
参数：`{ "message": "今天ROAS多少", "user_id": 1 }`

### GET /api/chat/history?user_id=1
获取对话历史。

### WebSocket /ws/chat
WebSocket 聊天端点。

## 健康检查

### GET /api/health
服务健康检查。

## 知识库

### POST /api/knowledge/search
检索知识库。
参数：`{ "query": "ROAS怎么算", "top_k": 3 }`

### POST /api/knowledge/add
添加知识。
参数：`{ "text": "...", "source": "..." }`

## 管理员

### GET /api/users
用户列表（管理员权限）。

### PUT /api/users/:id/role
修改用户角色。