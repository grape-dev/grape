#!/usr/bin/env bash
# Grape Docker 一键部署脚本
# 用法：./deploy/run-grape.sh [build|run|restart|all]
#   build   — docker build --no-cache（确保 configs/ 进镜像）
#   run     — 启动容器（挂载数据目录 + 环境变量 + 自动 seed）
#   restart — 停旧容器 → 启动新容器
#   all     — build + run（默认）
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-grape:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-grape}"
HOST_PORT="${HOST_PORT:-3000}"
DATA_DIR="${DATA_DIR:-./data}"

# === 环境变量（按需覆盖，勿提交真实凭据）===
# 模型网关（OpenAI 兼容）；不配置则 codex 直连官方端点
CODEX_BASE_URL="${CODEX_BASE_URL:-}"
CODEX_WIRE_API="${CODEX_WIRE_API:-responses}"
CODEX_MODEL="${CODEX_MODEL:-}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"

ACTION="${1:-all}"

echo "=== Grape Deploy ==="
echo "Action: $ACTION"
echo "Image:  $IMAGE_NAME"
echo "Port:   $HOST_PORT"
echo "Data:   $DATA_DIR"

do_build() {
    echo ""
    echo "--- Docker build --no-cache ---"
    cd "$(dirname "$0")/.."
    docker build --no-cache -t "$IMAGE_NAME" .
    echo "Build OK"
}

do_run() {
    echo ""
    echo "--- Stop old container ---"
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

    mkdir -p "$DATA_DIR"

    echo ""
    echo "--- Start container ---"
    docker run -d --name "$CONTAINER_NAME" --rm \
      -p "${HOST_PORT}:3000" \
      -v "${DATA_DIR}:/app/data" \
      -e CODEX_BASE_URL="$CODEX_BASE_URL" \
      -e CODEX_WIRE_API="$CODEX_WIRE_API" \
      -e CODEX_MODEL="$CODEX_MODEL" \
      -e OPENAI_API_KEY="$OPENAI_API_KEY" \
      "$IMAGE_NAME"

    echo ""
    echo "--- Wait for startup ---"
    sleep 4

    echo ""
    echo "--- Seed agent_runtimes (codex) ---"
    docker exec "$CONTAINER_NAME" python3 -c "
import sqlite3; conn = sqlite3.connect('/app/data/sessions.db'); c = conn.cursor()
import os; hn = os.popen('hostname').read().strip()
c.execute(\"SELECT id FROM agent_runtimes WHERE name='codex'\")
if not c.fetchone():
    c.execute('''INSERT INTO agent_runtimes
      (credential_id, name, display_name, installed, healthy, scope, status, host_name, pip_package, max_concurrent, agent_tags, scene_tags)
      VALUES (0, 'codex', 'Codex Agent', 1, 1, 'global', 'online', ?, 'openai-codex', 10, '[]', '[\"default\"]')''', (hn,))
    conn.commit()
    print('codex runtime seeded')
else:
    print('codex runtime already exists')
conn.close()
"

    echo ""
    echo "=== Deploy OK ==="
    echo "  URL:  http://localhost:${HOST_PORT}"
    echo "  Logs: docker logs -f $CONTAINER_NAME"
    echo "  Stop: docker stop $CONTAINER_NAME"
}

case "$ACTION" in
    build)   do_build ;;
    run)     do_run ;;
    restart) do_run ;;  # run 会先 rm -f 再启动
    all)     do_build; do_run ;;
    *) echo "Unknown action: $ACTION (use: build|run|restart|all)"; exit 1 ;;
esac
