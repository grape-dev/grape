#!/usr/bin/env bash
# Grape 四进程 systemd 启动器（grape@.service 的 ExecStart 目标）。
# 用法：grape-systemd-run.sh {mgmt|event|worker|insight}
# 角色 → 模块 / 端口 / metrics 端口映射集中在这里，避免 unit 文件里写死 exec 逻辑。
set -euo pipefail

role="${1:-}"
case "$role" in
    mgmt)    module="src.api.grape_server";          port=38081; mport=39081 ;;
    event)   module="src.api.grape_event_server";    port=38082; mport=39082 ;;
    worker)  module="src.api.grape_worker_server";   port=38083; mport=39083 ;;
    insight) module="src.api.grape_insight_server";  port=38084; mport=39084 ;;
    *) echo "unknown role: ${role:-<empty>}" >&2; exit 2 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 领域库拆分（2026-11）：每个进程连自己的库。
# 注意：systemd 的 EnvironmentFile=.env 在 Environment= 之后加载，会覆盖 drop-in 里写的
# DEVOPS_DB_*，所以领域库的用户/库名必须在这里按角色 export（脚本 export 晚于 .env 注入）。
case "$role" in
    mgmt)         _db_name="grape-mgmt-db";    _db_user="grape-mgmt" ;;
    event|worker) _db_name="grape-runtime-db"; _db_user="grape-runtime" ;;
    insight)      _db_name="grape-insight-db"; _db_user="grape-insight" ;;
esac
export DEVOPS_DB_NAME="$_db_name"
export DEVOPS_DB_USER="$_db_user"
export DEVOPS_DB_PASSWORD="${GRAPE_DOMAIN_DB_PASSWORD:-Grape@123}"

PY="$REPO_ROOT/venv/bin/python"
[ -x "$PY" ] || { echo "missing venv python: $PY" >&2; exit 3; }

# 配置由 systemd EnvironmentFile=.env 注入；这里只补 metrics/tracing 的运行期变量，
# 不写 .env（避免影响 restart-four-process.sh 等其它启动方式）。
export METRICS_ADDR="127.0.0.1:${mport}"
export GRAPE_SERVICE_NAME="${role}"
export GRAPE_OTLP_ENDPOINT="${GRAPE_OTLP_ENDPOINT:-http://127.0.0.1:4317}"
export GRAPE_TRACE_SERVICE="${GRAPE_TRACE_SERVICE:-grape-${role}}"
export GRAPE_TRACE_RATIO="${GRAPE_TRACE_RATIO:-0.5}"
export PYTHONUNBUFFERED=1

# exec：让 uvicorn 成为 systemd 的直接子进程（Type=simple 的 PID 1 级信号管理）。
# 不加 --workers：workers=1 时 uvicorn 在进程内直接跑 Server，SIGTERM 处理最干净。
exec "$PY" -m uvicorn "${module}:app" --host 0.0.0.0 --port "$port" --log-level info