#!/usr/bin/env bash
# Grape 四进程开发模式启动脚本（Issue #699 Phase 6，TC-6.1/TC-6.2）
#
# 用法：
#   ./scripts/dev_four_process.sh                 # 单机模式（SQLite + 表前缀，默认）
#   GRAPE_MODE=cluster DEVOPS_DB_BACKEND=mysql ./scripts/dev_four_process.sh
#   PROFILE=configs/profiles/cluster.conf ./scripts/dev_four_process.sh
#
# 行为：
#   1. 加载 profile（可选）并导出模式环境变量（GRAPE_MODE / DEVOPS_DB_BACKEND /
#      GRAPE_SCHEMA_MODE 等）。
#   2. 后台启动四个进程（Mgmt :38080 / Event :38081 / Worker :38082 / Insight :38083），
#      日志写入 /tmp/grape-four-process/*.log。
#   3. 等待各进程 /api/health 就绪（默认 60s 超时）。
#   4. Ctrl-C 或 kill 时统一回收四个进程（trap）。
#
# 退出码：0=四进程全部就绪；1=启动/健康检查失败。
# MySQL 集群模式需先准备三 schema（scripts/migrate_schemas.py --backend mysql）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${GRAPE_FOUR_LOG_DIR:-/tmp/grape-four-process}"
HEALTH_TIMEOUT="${GRAPE_FOUR_HEALTH_TIMEOUT:-60}"
PIDS=()

# ---- 1. profile 与模式环境 ----
if [ -n "${PROFILE:-}" ] && [ -f "${PROFILE}" ]; then
    # shellcheck disable=SC1090
    set -a; source "${PROFILE}"; set +a
    echo "[dev-four] loaded profile: ${PROFILE}"
fi

export GRAPE_MODE="${GRAPE_MODE:-single}"
export DEVOPS_DB_BACKEND="${DEVOPS_DB_BACKEND:-sqlite}"
export GRAPE_SCHEMA_MODE="${GRAPE_SCHEMA_MODE:-1}"

echo "[dev-four] GRAPE_MODE=${GRAPE_MODE} DEVOPS_DB_BACKEND=${DEVOPS_DB_BACKEND} GRAPE_SCHEMA_MODE=${GRAPE_SCHEMA_MODE}"

# 单机 SQLite：确保 WAL + busy_timeout（防 database is locked，设计 §13 风险表）
if [ "${DEVOPS_DB_BACKEND}" = "sqlite" ]; then
    export DEVOPS_SQLITE_WAL="${DEVOPS_SQLITE_WAL:-1}"
    export DEVOPS_SQLITE_BUSY_TIMEOUT_MS="${DEVOPS_SQLITE_BUSY_TIMEOUT_MS:-5000}"
fi

# 集群 MySQL：必须配置连接（否则快速失败，避免静默回退 sqlite）
if [ "${GRAPE_MODE}" = "cluster" ] && [ "${DEVOPS_DB_BACKEND}" = "mysql" ]; then
    : "${DEVOPS_DB_HOST:?GRAPE_MODE=cluster + mysql requires DEVOPS_DB_HOST}"
    : "${DEVOPS_DB_USER:?GRAPE_MODE=cluster + mysql requires DEVOPS_DB_USER}"
    : "${DEVOPS_DB_PASSWORD:?GRAPE_MODE=cluster + mysql requires DEVOPS_DB_PASSWORD}"
    : "${DEVOPS_DB_NAME:?GRAPE_MODE=cluster + mysql requires DEVOPS_DB_NAME}"
    : "${GRAPE_SERVICE_SECRET:?GRAPE_MODE=cluster requires GRAPE_SERVICE_SECRET (internal API auth)}"
fi

mkdir -p "${LOG_DIR}"

cleanup() {
    echo "[dev-four] stopping ..."
    for pid in "${PIDS[@]:-}"; do
        [ -n "${pid}" ] && kill "${pid}" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

start_one() {  # name module port -> sets PID_<name>
    local name="$1" module="$2" port="$3"
    echo "[dev-four] starting ${name} (${module}) on :${port}"
    GRAPE_PROCESS_ROLE="${name}" GRAPE_PORT="${port}" \
        python -m uvicorn "${module}:app" --host 127.0.0.1 --port "${port}" \
        >"${LOG_DIR}/${name}.log" 2>&1 &
    declare -g "PID_${name}=$!"
    PIDS+=("$!")
}

wait_health() {  # name port pid
    local name="$1" port="$2" pid="$3" waited=0
    while [ "${waited}" -lt "${HEALTH_TIMEOUT}" ]; do
        # 进程健康端点：Mgmt/Insight 为 /api/health，Event/Worker 为 /health（Phase 4 四进程设计）
        if curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1 \
            || curl -sf "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1; then
            echo "[dev-four] ${name} healthy on :${port}"
            return 0
        fi
        if ! kill -0 "${pid}" 2>/dev/null; then
            echo "[dev-four] ERROR: ${name} (pid ${pid}) exited early; log tail:" >&2
            tail -20 "${LOG_DIR}/${name}.log" >&2 || true
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "[dev-four] ERROR: ${name} health check timeout (${HEALTH_TIMEOUT}s)" >&2
    tail -20 "${LOG_DIR}/${name}.log" >&2 || true
    return 1
}

cd "${REPO_ROOT}"

# ---- 2. 启动四进程（Mgmt / Event / Worker / Insight）----
start_one server   src.api.grape_server         38080
start_one event    src.api.grape_event_server   38081
start_one worker   src.api.grape_worker_server  38082
start_one insight  src.api.grape_insight_server 38083

# ---- 3. 健康检查 ----
ok=0
wait_health server  38080 "${PID_server}"   || ok=1
wait_health event   38081 "${PID_event}"    || ok=1
wait_health worker  38082 "${PID_worker}"   || ok=1
wait_health insight 38083 "${PID_insight}"  || ok=1

if [ "${ok}" -ne 0 ]; then
    echo "[dev-four] FAILED: some process unhealthy; logs in ${LOG_DIR}" >&2
    exit 1
fi

# ---- 4. locked 检测（TC-6.1 验收点：无 database is locked）----
if grep -i "database is locked" "${LOG_DIR}"/*.log 2>/dev/null; then
    echo "[dev-four] ERROR: 'database is locked' detected in logs" >&2
    exit 1
fi

echo "[dev-four] all four processes up (mode=${GRAPE_MODE}, backend=${DEVOPS_DB_BACKEND})"
echo "[dev-four] logs: ${LOG_DIR}  (Ctrl-C to stop)"

# ---- 5. 前台等待（保持 trap 生效）----
wait
