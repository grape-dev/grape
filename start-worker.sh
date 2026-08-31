#!/usr/bin/env bash
# Grape Worker 启动脚本（支持崩溃自动重启 daemon 保护）
#
# 用法：
#   ./start-worker.sh              # 前台运行（带 daemon 保护）
#   ./start-worker.sh --no-daemon  # 不带 daemon（原行为，调试用）
#
# daemon 模式：worker 进程崩溃后自动重启，间隔 5 秒，最多 10 次/小时

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ===== 配置 =====
WORKER_DIR="${GRAPE_WORKER_DIR:-$SCRIPT_DIR}"
PYTHON="${GRAPE_PYTHON:-$WORKER_DIR/.venv/bin/python}"
MAX_RESTARTS=10
RESTART_WINDOW=3600  # 1 小时
RESTART_DELAY=5      # 5 秒

# ===== daemon 保护 =====
if [ "$1" != "--no-daemon" ]; then
  RESTART_COUNT=0
  WINDOW_START=$(date +%s)

  while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting worker (attempt $((RESTART_COUNT + 1)))..."

    # 启动 worker（前台运行，阻塞直到退出）
    "$0" --no-daemon &
    WORKER_PID=$!
    wait $WORKER_PID
    EXIT_CODE=$?

    NOW=$(date +%s)
    ELAPSED=$((NOW - WINDOW_START))

    # 重置计数窗口
    if [ $ELAPSED -gt $RESTART_WINDOW ]; then
      RESTART_COUNT=0
      WINDOW_START=$NOW
    fi

    RESTART_COUNT=$((RESTART_COUNT + 1))

    if [ $RESTART_COUNT -ge $MAX_RESTARTS ]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] CRITICAL: Worker crashed $RESTART_COUNT times within $RESTART_WINDOW seconds. Stopping daemon to prevent crash loop."
      exit 1
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Worker exited (code=$EXIT_CODE), restarting in ${RESTART_DELAY}s... (restart #$RESTART_COUNT, window ${ELAPSED}s/${RESTART_WINDOW}s)"
    sleep $RESTART_DELAY
  done
  exit 0
fi

# ===== 实际启动 worker =====
cd "$WORKER_DIR"
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$HOME/.local/npm-global/bin:$PATH"

# 加载环境变量
ENV_FILE="${GRAPE_ENV_FILE:-$HOME/.grape/runtime.env}"
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi

exec "$PYTHON" -m src.runtime.agent
