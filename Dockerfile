ARG PYTHON_BASE=python:3.12-slim
FROM ${PYTHON_BASE}

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY locks/bge.txt /app/locks/bge.txt
RUN pip install --no-cache-dir --require-hashes -r locks/bge.txt

COPY . /app
RUN mkdir -p /app/data /app/backend/memory/user_memory && \
    ln -s /app/data/ad_agent.db /app/ad_agent.db

EXPOSE 5010
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "from http.client import HTTPConnection; c = HTTPConnection('127.0.0.1', 5010, timeout=3); c.request('GET', '/api/ready'); raise SystemExit(c.getresponse().status != 200)"]
CMD ["python", "-m", "uvicorn", "fast_server:app", "--host", "0.0.0.0", "--port", "5010"]
