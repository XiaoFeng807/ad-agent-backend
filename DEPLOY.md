# 部署文档

## 本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填写 API_KEY

# 3. 启动服务
python fast_server.py

# 或
uvicorn fast_server:app --host 0.0.0.0 --port 5010 --reload
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| API_KEY | DeepSeek / OpenAI 密钥 | - |
| BASE_URL | API 地址 | https://api.deepseek.com |
| MODEL | 模型名 | deepseek-chat |
| LLM_PROVIDER | 提供商 | deepseek |
| PORT | 端口 | 5010 |
| DEBUG | 调试模式 | True |
| JWT_SECRET | JWT 密钥 | ad_agent_secret_key_2026 |

## Docker

```bash
# 构建
docker build -t ad-agent .

# 运行
docker run -d -p 5010:5010 --env-file .env ad-agent
```

## 对外暴露

### ngrok（推荐快速演示）

```bash
ngrok http 5010
```

### 生产部署建议
- 使用 Gunicorn + Uvicorn workers
- 配置反向代理（Nginx）
- 启用 HTTPS
- 数据库从 SQLite 迁移到 PostgreSQL
- 添加 Redis 缓存