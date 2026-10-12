#!/usr/bin/env bash
# Grape Worker 启动脚本（支持崩溃自动重启 daemon 保护）
# 注意：此文件是通用模板，不含任何机器路径——正式安装时 install-worker.sh 会按
# 实际安装目录重新生成（见 scripts/install-worker.sh 内嵌版本）。
# 手动运行时按脚本所在目录定位 worker（GRAPE_WORKER_DIR/GRAPE_PYTHON 可覆盖）。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/start-worker.sh"
WORKER_DIR="${GRAPE_WORKER_DIR:-$SCRIPT_DIR}"
PYTHON="${GRAPE_PYTHON:-$WORKER_DIR/.venv/bin/python}"
MAX_RESTARTS=10
RESTART_WINDOW=3600
RESTART_DELAY=5

if [ "$1" != "--no-daemon" ]; then
  RESTART_COUNT=0
  WINDOW_START=$(date +%s)
  while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting worker (attempt $((RESTART_COUNT + 1)))..."
    "$SCRIPT_PATH" --no-daemon &
    WORKER_PID=$!
    wait $WORKER_PID
    EXIT_CODE=$?
    NOW=$(date +%s)
    ELAPSED=$((NOW - WINDOW_START))
    if [ $ELAPSED -gt $RESTART_WINDOW ]; then
      RESTART_COUNT=0
      WINDOW_START=$NOW
    fi
    RESTART_COUNT=$((RESTART_COUNT + 1))
    if [ $RESTART_COUNT -ge $MAX_RESTARTS ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] CRITICAL: Worker crashed $RESTART_COUNT times, stopping daemon."
      exit 1
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Worker exited (code=$EXIT_CODE), restarting in ${RESTART_DELAY}s... ($RESTART_COUNT/$MAX_RESTARTS)"
    sleep $RESTART_DELAY
  done
  exit 0
fi

cd "$WORKER_DIR"
export PATH="$WORKER_DIR/.venv/bin:$HOME/.opencode/bin:$HOME/.local/bin:$HOME/.local/npm-global/bin:$PATH"
ENV_FILE="$HOME/.grape/runtime.env"
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi
exec "$PYTHON" -m src.runtime.agent
