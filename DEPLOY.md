# 部署文档 — 智能广告投放助手

## 环境要求

| 项目 | 最低要求 | 推荐 |
|------|---------|------|
| Python | 3.10+ | 3.12 |
| 内存 | 512MB | 1GB+ |
| 磁盘 | 100MB | 500MB |
| 操作系统 | Windows 10+ / Linux / macOS | - |

## 必要依赖

```bash
pip install flask pyjwt pillow python-dotenv openai flask-cors
```

## 目录结构

```
ad_agent_backend/
├── server.py                  # 启动入口（不要改名）
├── .env                       # 配置文件（需要自己填）
├── gen_all.py                 # 前端页面生成器（改样式后运行）
├── ad_agent.db               # SQLite 数据库（自动生成）
├── DEPLOY.md                  # 本部署文档
├── backend/
│   ├── static/index.html      # 前端页面（生成）
│   ├── routes/                # 模块化路由
│   └── ...                    # 其他后端模块
```

---

## 一、快速启动（开发模式）

```bash
# 1. 进入项目目录
cd ad_agent_backend

# 2. 安装依赖（只需要运行一次）
pip install flask pyjwt pillow python-dotenv openai

# 3. 配置 .env 文件（参考下文）
# 4. 启动服务
python server.py

# 5. 打开浏览器访问
#    http://localhost:5000
```

### 预设测试账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| boss | admin123 | 老板 |
| admin | admin123 | 管理员 |
| zhangsan | user123 | 用户 |
| lisi | user123 | 用户 |

---

## 二、配置文件 (.env)

项目根目录的 `.env` 文件控制所有配置：

```env
# === DeepSeek API 配置（AI投放助手必需）===
# 在 https://platform.deepseek.com 注册获取
API_KEY=sk-你的API-KEY

# API 地址（DeepSeek 固定）
BASE_URL=https://api.deepseek.com

# 模型名称
MODEL=deepseek-chat

# === 服务配置 ===

# LLM Provider 切换（deepseek / mock / openai）
# deepseek=真实调用, mock=模拟回复(免API), openai=OpenAI
LLM_PROVIDER=deepseek

# === 服务配置 ===
PORT=5000
HOST=0.0.0.0
DEBUG=True
```

> **注意**：`.env` 文件包含 API Key，不要分享给他人。
> 不配置 API Key 时 AI 投放助手无法使用，其他功能正常。

---

## 三、生产部署

### Windows 部署（推荐 waitress）

```bash
# 1. 安装生产服务器
pip install waitress

# 2. 修改 .env 关闭调试模式
#    DEBUG=False

# 3. 创建启动脚本 start_server.py
```

创建 `start_server.py` 文件：

```python
from server import create_app
from waitress import serve

app, _, _, _, _ = create_app()
print("Server running on http://0.0.0.0:5000")
serve(app, host="0.0.0.0", port=5000)
```

```bash
# 4. 运行
python start_server.py
```

### Linux 部署（推荐 gunicorn）

```bash
# 1. 安装生产服务器
pip install gunicorn

# 2. 修改 .env
#    DEBUG=False

# 3. 运行
gunicorn -w 2 -b 0.0.0.0:5000 "server:create_app()"

# 4. （可选）后台运行
nohup gunicorn -w 2 -b 0.0.0.0:5000 "server:create_app()" > server.log 2>&1 &
```

### 使用 systemd 管理（Linux）

创建 `/etc/systemd/system/ad-agent.service`：

```ini
[Unit]
Description=Ad Agent Backend
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/ad_agent_backend
ExecStart=/usr/bin/python start_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable ad-agent
sudo systemctl start ad-agent
```

---

## 四、让朋友访问（内网穿透）

### 方式 1：ngrok（推荐，免费够用）

```bash
# 1. 下载 ngrok
#    https://ngrok.com/download

# 2. 注册账号并获取 authtoken
#    https://dashboard.ngrok.com/auth

# 3. 配置 token
ngrok config add-authtoken 你的TOKEN

# 4. 先确保项目在运行
python server.py

# 5. 新开一个终端，启动 ngrok
ngrok http 5000

# 6. 会生成一个网址，比如：
#    https://xxxx-xx-xx-xx-xx.ngrok-free.app
#    把这个网址发给朋友即可
```

> **ngrok 免费版限制**：
> - 每次启动 URL 会变化
> - 每月 40 分钟流量限制
> - 适合演示和临时测试

### 方式 2：部署到云服务器

如果长期使用，推荐购买云服务器（腾讯云轻量、阿里云ECS等，约 50元/月）：

```bash
# 1. SSH 登录服务器
ssh root@你的服务器IP

# 2. 安装 Python 和依赖
apt update && apt install -y python3 python3-pip
pip3 install flask pyjwt pillow python-dotenv openai waitress

# 3. 上传项目文件
#    使用 scp 或 git clone

# 4. 配置 .env 和防火墙
#    确保 5000 端口开放

# 5. 用 waitress 启动
python start_server.py
```

---

## 五、生成前端页面

如果你修改了样式或功能，需要重新生成前端页面：

```bash
python gen_all.py
```

这会更新 `backend/static/index.html`。

---

## 六、常见问题

### Q: 启动报错 "ModuleNotFoundError"
```
# 缺少依赖，运行：
pip install flask pyjwt pillow python-dotenv openai
```

### Q: 别人访问时验证码图片加载不出来
```
# 确保 .env 中的 HOST=0.0.0.0
# 使用 ngrok 时不需要额外配置
```

### Q: 数据库在哪里？
```
# 项目根目录的 ad_agent.db
# 删除它会自动重建（但数据会丢失）
```

### Q: 怎么重置所有数据？
```bash
# 删除数据库文件，重启服务
del ad_agent.db
python server.py
```

### Q: 500 错误？
```
# 查看命令行窗口的错误日志
# 最常见原因：API Key 无效或网络不通
```

### Q: AI 助手一直"思考中"？
```
# 检查 .env 中的 API_KEY 是否正确
# 检查网络是否能访问 api.deepseek.com
```

---

## 七、安全注意事项

1. **不要分享 `.env` 文件** — 包含你的 API Key
2. **不要用 root 用户运行** — 创建普通用户
3. **生产环境关闭 DEBUG** — `DEBUG=False`
4. **定期备份数据库** — 复制 `ad_agent.db` 文件
5. **更新依赖** — 定期运行 `pip list --outdated` 检查

---

## 八、技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | Flask 3.x |
| 前端 | 原生 HTML + CSS + JavaScript |
| AI 接口 | DeepSeek API (OpenAI SDK) |
| 数据库 | SQLite |
| 认证 | JWT (PyJWT) |
| 验证码 | PIL/Pillow 生成 |
| 图表 | ECharts CDN |

---

## 九、
---

## 十、Docker 部署

### 前提条件
- 安装 Docker: https://docs.docker.com/get-docker/
- 配置 .env 文件（API_KEY 等）

### 方式一：使用 docker-compose（推荐）

`ash
# 1. 确保 .env 已配置
# 2. 构建并启动
docker-compose up -d

# 3. 查看日志
docker-compose logs -f

# 4. 停止
docker-compose down
`

### 方式二：使用 Docker 命令行

`ash
# 1. 构建镜像
docker build -t ad-agent .

# 2. 运行容器
docker run -d \\
  --name ad-agent \\
  -p 5000:5000 \\
  -v ./.env:/app/.env:ro \\
  -v ./data:/app/data \\
  ad-agent

# 3. 查看日志
docker logs -f ad-agent
`

### 访问
浏览器打开 http://localhost:5000

### 目录挂载说明
| 挂载路径 | 说明 |
|---------|------|
| ./.env:/app/.env:ro | API Key 配置（只读） |
| ./data:/app/data | 数据库持久化（容器重启不丢失） |

项目包含 3 个测试文件，覆盖核心功能：

```bash
# 一键运行所有测试
python run_tests.py
```

### 测试文件列表

- tests/test_dashboard.py - 仪表盘数据、趋势图
- tests/test_auth.py - 登录验证、密码加密、验证码
- tests/test_tools.py - 搜索趋势、告警、广告计划

### 测试示例

```python
def test_dashboard_returns_all_fields():
    data = get_dashboard_data(1)
    assert "total_cost" in data
```

**注意**：测试会使用数据库中的模拟数据。

