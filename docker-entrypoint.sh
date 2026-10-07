#!/bin/sh
# 容器入口：初始化数据库（首次启动导入种子/示例数据）后启动 HTTP 服务。
set -e

echo "[entrypoint] 初始化数据库..."
python -m app.cli init-db

echo "[entrypoint] 启动 HTTP 服务 ${APP_HOST:-0.0.0.0}:${APP_PORT:-8080}"
exec python -m uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8080}"
