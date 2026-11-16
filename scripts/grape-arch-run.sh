#!/usr/bin/env bash
# Grape 四进程启动器（grape-arch-4p dev 实例）
# 用法：grape-arch-run.sh {mgmt|event|worker|insight}
# 设计依据（用户 2026-11-12 部署指令）：
#   4 进程共库实例（本机 MariaDB）、各挂域库：
#     mgmt:8081→grape_dev_mgmt  event:8082→grape_dev_runtime
#     worker:8083→grape_dev_runtime  insight:8084→grape_dev_insight
#   指标 9101-9104（127.0.0.1 绑定，本机抓取）。
#   IM 表钉 runtime 域（GRAPE_IM_DB_* 在 /etc/grape/grape.env）。
#   约束：迁移 DDL 只落本进程域库（域账号按库授权，跨域 ALTER 被 MySQL 直接
#   拒绝——grape_dev_mgmt 账号对 grape_dev_runtime 无权限，实测 Access denied）；
#   视图侧（跨域只读消费）不做任何 ALTER。
set -euo pipefail

role="${1:-}"
case "$role" in
    mgmt)    module="src.api.grape_server";         port=8081; mport=9101 ;;
    event)   module="src.api.grape_event_server";   port=8082; mport=9102 ;;
    worker)  module="src.api.grape_worker_server";  port=8083; mport=9103 ;;
    insight) module="src.api.grape_insight_server"; port=8084; mport=9104 ;;
    *) echo "unknown role: ${role:-<empty>} (expect mgmt|event|worker|insight)" >&2; exit 2 ;;
esac

# 域库绑定：角色 → 库名/账号（export 晚于 systemd EnvironmentFile 注入，确保覆盖生效；
# 同仓 grape-systemd-run.sh 先例：EnvironmentFile 在 Environment= 之后加载会反向覆盖）。
case "$role" in
    mgmt)         db="grape_dev_mgmt" ;;
    event|worker) db="grape_dev_runtime" ;;
    insight)      db="grape_dev_insight" ;;
esac
export DEVOPS_DB_NAME="$db"
export DEVOPS_DB_USER="$db"
export DEVOPS_DB_PASSWORD="${GRAPE_DOMAIN_DB_PASSWORD:?GRAPE_DOMAIN_DB_PASSWORD missing in /etc/grape/grape.env}"

REPO_ROOT="/usr1/zhangjian/grapeCode/grape-arch-4p"
cd "$REPO_ROOT"
PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "missing venv python: $PY" >&2; exit 3; }

# 运行期变量（只补不写 env 文件）
export GRAPE_PORT="$port"
export METRICS_ADDR="127.0.0.1:${mport}"
export GRAPE_SERVICE_NAME="${role}"
export GRAPE_OTLP_ENDPOINT="${GRAPE_OTLP_ENDPOINT:-http://127.0.0.1:4317}"
export GRAPE_TRACE_SERVICE="${GRAPE_TRACE_SERVICE:-grape-arch-4p-${role}}"
export GRAPE_TRACE_RATIO="${GRAPE_TRACE_RATIO:-0.5}"

mkdir -p "$REPO_ROOT/logs"
exec "$PY" -m uvicorn "${module}:app" --host 0.0.0.0 --port "$port" --workers 1 \
    >> "$REPO_ROOT/logs/${role}.log" 2>&1
