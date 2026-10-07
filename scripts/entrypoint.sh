#!/bin/sh
# 容器入口：依赖（数据库）在 compose 中以 healthcheck 保证就绪，
# 应用启动时自带 wait_for_database + 建表重试，这里直接启动 uvicorn。
set -e

HOST="${APP_HOST:-0.0.0.0}"
PORT="${APP_PORT:-8080}"

echo "[entrypoint] starting emergency broadcast adjudication service on ${HOST}:${PORT}"
exec uvicorn app.main:app --host "${HOST}" --port "${PORT}"
