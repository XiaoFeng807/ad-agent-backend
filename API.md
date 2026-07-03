# API 接口文档

> 本文档列出智能广告投放助手的所有后端 API 接口。  
> 基础路径: `http://localhost:5000`  
> 认证方式: 登录后获取 Token，后续请求在 Header 携带 `Authorization: Bearer <token>`

---

## 一、认证模块

### `GET /api/captcha`
获取验证码 ID（用于获取验证码图片）

- **无需登录**
- **返回**: `{"code": 200, "captcha_id": "xxx"}`

### `GET /api/captcha/img?captcha_id=xxx`
获取验证码图片（Base64）

- **参数**: `captcha_id`（上一步获取的ID）
- **返回**: `{"code": 200, "image": "data:image/png;base64,...", "captcha_id": "xxx"}`

### `POST /api/login`
用户登录

- **无需登录**
- **请求体**: `{"username": "boss", "password": "admin123", "captcha_id": "xxx", "captcha_text": "abcd"}`
- **返回**: `{"code": 200, "token": "jwt_token...", "role": "boss", "username": "boss"}`

### `POST /api/register`
注册新用户（仅 boss 角色有权限）

- **请求体**: `{"username": "newuser", "password": "123456", "role": "user"}`
- **返回**: `{"code": 200, "msg": "注册成功"}`

---

## 二、仪表盘

### `GET /api/dashboard`
获取仪表盘核心指标

- **返回**: `{"code": 200, "data": {"total_cost": 1234, "total_sales": 5678, "roas": 4.6, "cpc": 2.3, "ctr": 3.5, "total_orders": 45, "cpa": 27.4, "daily_trend": [...]}}`

### `GET /api/daily_reports?days=7`
获取每日趋势数据（用于图表）

- **参数**: `days`（天数，默认7）
- **返回**: `{"code": 200, "data": [{"date": "06-28", "cost": 200, "sales": 800, "roas": 4.0}, ...]}`

---

## 三、广告账户

### `GET /api/accounts`
获取当前用户的广告账户列表

- **返回**: `{"code": 200, "data": [{"id": 1, "account_name": "Google Ads", "platform": "Google Ads", "balance": 18000, "daily_budget": 500, "status": "active"}, ...]}`

### `PUT /api/accounts/:id/budget`
修改账户日预算

- **请求体**: `{"daily_budget": 600}`
- **返回**: `{"code": 200, "msg": "预算已更新"}`

### `POST /api/accounts/:id/recharge`
账户充值

- **请求体**: `{"amount": 5000}`
- **返回**: `{"code": 200, "msg": "充值成功"}`

---

## 四、告警中心

### `GET /api/alerts`
获取告警列表

- **返回**: `{"code": 200, "data": [{"id": 1, "message": "ROAS跌破保本线", "level": "warning", "is_read": 0, "created_at": "2026-06-28 10:00"}, ...]}`

### `POST /api/alerts/read`
标记告警为已读（支持单个或批量）

- **请求体**: `{"alert_id": 1}` 或 `{"ids": [1, 2, 3]}`
- **返回**: `{"code": 200, "msg": "已标记为已读"}`

---

## 五、投放计划

### `GET /api/plans`
获取所有广告计划

- **返回**: `{"code": 200, "data": [{"plan_id": 1, "plan_name": "Google搜索-品牌词", "platform": "Google Ads", "status": "active", "cost": 500, "sales": 2000, "roas": 4.0}, ...]}`

### `POST /api/plans`
创建新的广告计划

- **请求体**: `{"plan_name": "新计划", "platform": "Google Ads", "budget": 300}`
- **返回**: `{"code": 200, "msg": "计划已创建"}`

### `POST /api/plans/:id/toggle`
暂停/启用广告计划

- **返回**: `{"code": 200, "msg": "计划已暂停/启用", "new_status": "paused/active"}`

### `DELETE /api/plans/:id`
删除广告计划

- **返回**: `{"code": 200, "msg": "计划已删除"}`

---

## 六、素材管理

### `GET /api/materials`
获取素材列表

- **返回**: `{"code": 200, "data": [{"id": 1, "name": "主图_v1", "type": "image", "url": "...", "created_at": "..."}, ...]}`

### `POST /api/materials`
上传素材

- **请求体**: `{"name": "新素材", "type": "image", "url": "/uploads/xxx.jpg"}`
- **返回**: `{"code": 200, "msg": "素材已上传"}`

---

## 七、AI 聊天

### `POST /api/chat`
发送消息给 AI 助手（非流式，兼容旧版）

- **请求体**: `{"message": "今天数据怎么样"}`
- **返回**: `{"code": 200, "data": {"reply": "今天整体表现一般..."}}`

### `POST /api/chat/stream`
流式聊天（SSE 协议，逐字推送）

- **请求体**: `{"message": "今天数据怎么样"}`
- **响应格式**: Server-Sent Events
  ```
  data: {"type": "thinking"}
  data: {"type": "text", "content": "今天"}
  data: {"type": "text", "content": "整体"}
  data: {"type": "text", "content": "表现..."}
  data: {"type": "done"}
  ```

### `GET /api/chat/history`
获取聊天历史记录

- **返回**: `{"code": 200, "data": [{"role": "user", "content": "今天数据怎么样"}, {"role": "assistant", "content": "..."}]}`

---

## 八、用户管理

### `GET /api/users`
获取所有用户列表

- **返回**: `{"code": 200, "data": [{"id": 1, "username": "boss", "role": "boss"}, ...]}`

### `POST /api/users`
创建新用户

- **权限**: boss 可建所有角色，admin 只能建 user
- **请求体**: `{"username": "newuser", "password": "123456", "role": "user"}`
- **返回**: `{"code": 200, "msg": "创建成功"}`

### `DELETE /api/users/:id`
删除用户

- **权限**: boss 可删非 boss，admin 可删 user
- **返回**: `{"code": 200, "msg": "删除成功"}`

---

## 九、操作日志

### `GET /api/logs`
获取操作日志

- **权限**: boss 看全部，admin 看 admin+user，user 只看自己
- **返回**: `{"code": 200, "data": [{"username": "boss", "action": "登录", "detail": "...", "created_at": "..."}, ...]}`

---

## 十、决策分析

### `GET /api/decisions`
获取 AI 决策记录

- **返回**: `{"code": 200, "data": [...]}`

### `GET /api/timeline`
获取活动时间轴

- **返回**: `{"code": 200, "data": [...]}`

---

## 十一、健康检查

### `GET /api/health`
服务健康检查

- **无需登录**
- **返回**: `{"code": 200, "data": {"status": "ok", "uptime": "1h 23m", "db": "ok", "api_key": true}}`

---

## 通用返回格式

所有接口统一返回格式：

```json
{"code": 200, "data": ..., "msg": "..."}
```

| 字段 | 说明 |
|------|------|
| `code` | 200=成功, 400=参数错误, 401=未登录, 403=无权限, 500=服务器错误 |
| `data` | 响应数据（可能为空） |
| `msg` | 提示消息（错误时也有） |

## 预设测试账户

| 用户名 | 密码 | 角色 |
|--------|------|------|
| boss | admin123 | 老板 |
| admin | admin123 | 管理员 |
| zhangsan | user123 | 用户 |
| lisi | user123 | 用户 |
