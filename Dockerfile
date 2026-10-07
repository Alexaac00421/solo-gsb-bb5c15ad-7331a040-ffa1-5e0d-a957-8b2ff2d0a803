FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
RUN chmod +x ./scripts/entrypoint.sh

EXPOSE 8080

# entrypoint 等待数据库就绪后由 uvicorn 启动服务；建表在应用启动时自动完成
ENTRYPOINT ["./scripts/entrypoint.sh"]
