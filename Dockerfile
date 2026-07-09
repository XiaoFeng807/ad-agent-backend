# ===== 智能广告投放助手 - Dockerfile =====
FROM python:3.12-slim

WORKDIR /app

# 环境变量
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 复制依赖文件先装（利用 Docker 缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 确保数据目录可写
RUN mkdir -p /app/data && chmod -R 755 /app/data

# 暴露端口
EXPOSE 5001

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5001/api/health || exit 1

# 启动服务
CMD ["python", "fast_server.py"]

