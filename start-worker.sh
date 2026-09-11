#!/usr/bin/env bash
# Grape Worker 启动脚本（由 install-worker.sh 生成，支持崩溃自动重启）
# daemon 模式：worker 崩溃后自动重启，间隔 5s，最多 10 次/小时

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAX_RESTARTS=10
RESTART_WINDOW=3600
RESTART_DELAY=5

if [ "$1" != "--no-daemon" ]; then
  RESTART_COUNT=0
  WINDOW_START=$(date +%s)
  while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting worker (attempt $((RESTART_COUNT + 1)))..."
    "$0" --no-daemon &
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

cd "/home/mys/grape"
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$HOME/.local/npm-global/bin:$PATH"
set -a; . "/home/mys/.grape/runtime.env"; set +a
exec "/home/mys/grape/.venv/bin/python" -m src.runtime.agent
