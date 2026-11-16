FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY locks/bge.txt /app/locks/bge.txt
RUN pip install --no-cache-dir --require-hashes -r locks/bge.txt

COPY . /app
RUN mkdir -p /app/data /app/backend/memory/user_memory && \
    ln -s /app/data/ad_agent.db /app/ad_agent.db

EXPOSE 5010
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:5010/api/health', timeout=3).close()"]
CMD ["python", "-m", "uvicorn", "fast_server:app", "--host", "0.0.0.0", "--port", "5010"]
