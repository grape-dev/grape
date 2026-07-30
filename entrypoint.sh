#!/bin/bash
set -e

# 启动主服务（SPA + API）
uvicorn src.api.main:app --host 0.0.0.0 --port 3000 &

# 启动 Webhook 服务
uvicorn src.api.run_webhook:app --host 0.0.0.0 --port 8080 &

# 等待所有后台进程
wait -n
