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
    mgmt)         _db_name="grape_mgmt_db";    _db_user="grape_mgmt" ;;
    event|worker) _db_name="grape_runtime_db"; _db_user="grape_runtime" ;;
    insight)      _db_name="grape_insight_db"; _db_user="grape_insight" ;;
esac
export DEVOPS_DB_NAME="$_db_name"
export DEVOPS_DB_USER="$_db_user"
# 库口令必须由环境显式提供：内置默认值等于把生产口令写进公开仓库。
: "${GRAPE_DOMAIN_DB_PASSWORD:?GRAPE_DOMAIN_DB_PASSWORD 未设置：单库分支需要显式提供库口令（不允许内置默认值）}"
export DEVOPS_DB_PASSWORD="$GRAPE_DOMAIN_DB_PASSWORD"

PY="$REPO_ROOT/venv/bin/python"
[ -x "$PY" ] || { echo "missing venv python: $PY" >&2; exit 3; }

# ── 三库独立模式（deploy/deploy3db.sh 的 systemd 等价路径）────────────────────
# GRAPE_3DB_ENV 指向三库 DSN 文件（如 /etc/grape/grape-3db.env，内含
# GRAPE_DSN_MGMT / GRAPE_DSN_RUNTIME / GRAPE_DSN_INSIGHT 三行）。设置后：
#   1) source 该文件（DSN 只在这里读，不写进 .env）；
#   2) GRAPE_DOMAIN_LIB=1 打开跨域连接（owner 域 lib）；
#   3) 把本角色 owner 域的 DSN 拆成 DEVOPS_DB_*，让进程主库 = 本域库。
# 未设置时保持原行为（按角色连 grape_<domain>_db，单库多库但不启 lib）。
_3db_env="${GRAPE_3DB_ENV:-}"
if [ -n "$_3db_env" ]; then
    [ -r "$_3db_env" ] || { echo "GRAPE_3DB_ENV 不可读: $_3db_env" >&2; exit 4; }
    # shellcheck disable=SC1090
    set -a; . "$_3db_env"; set +a
    export GRAPE_DOMAIN_LIB="${GRAPE_DOMAIN_LIB:-1}"
    case "$role" in
        mgmt)         _dsn="${GRAPE_DSN_MGMT:-}" ;;
        event|worker) _dsn="${GRAPE_DSN_RUNTIME:-}" ;;
        insight)      _dsn="${GRAPE_DSN_INSIGHT:-}" ;;
    esac
    if [ -z "$_dsn" ]; then
        echo "三库模式缺少本角色 DSN（role=$role，检查 $_3db_env 的 GRAPE_DSN_*）" >&2
        exit 5
    fi
    # 用 urlparse 拆 DSN：密码里含 @ : / 也不会拆错（deploy3db.sh 的 bash 切分在那种
    # 情况下会把 host 拆坏）。
    eval "$("$PY" - "$_dsn" <<'PYEOF'
import shlex
import sys
from urllib.parse import unquote, urlparse

u = urlparse(sys.argv[1])
print("export DEVOPS_DB_HOST=%s" % shlex.quote(u.hostname or "127.0.0.1"))
print("export DEVOPS_DB_PORT=%s" % shlex.quote(str(u.port or 3306)))
print("export DEVOPS_DB_USER=%s" % shlex.quote(unquote(u.username or "")))
print("export DEVOPS_DB_PASSWORD=%s" % shlex.quote(unquote(u.password or "")))
print("export DEVOPS_DB_NAME=%s" % shlex.quote((u.path or "/").lstrip("/").split("?")[0]))
PYEOF
)"
else
    # 领域库拆分（2026-11）：每个进程连自己的库。
    # 注意：systemd 的 EnvironmentFile=.env 在 Environment= 之后加载，会覆盖 drop-in 里写的
    # DEVOPS_DB_*，所以领域库的用户/库名必须在这里按角色 export（脚本 export 晚于 .env 注入）。
    case "$role" in
        mgmt)         _db_name="grape_mgmt_db";    _db_user="grape_mgmt" ;;
        event|worker) _db_name="grape_runtime_db"; _db_user="grape_runtime" ;;
        insight)      _db_name="grape_insight_db"; _db_user="grape_insight" ;;
    esac
    export DEVOPS_DB_NAME="$_db_name"
    export DEVOPS_DB_USER="$_db_user"
    : "${GRAPE_DOMAIN_DB_PASSWORD:?GRAPE_DOMAIN_DB_PASSWORD 未设置：单库分支需要显式提供库口令（不允许内置默认值）}"
    export DEVOPS_DB_PASSWORD="$GRAPE_DOMAIN_DB_PASSWORD"
fi

# GSB 注册地址必须是其它节点可达的地址：留空时客户端回落 127.0.0.1，
# 跨节点 round_robin 会全部打回本机（registry 里 base_url=http://127.0.0.1:3808x）。
# 优先用 drop-in 里的显式值，否则取本机首个私网地址。
if [ -z "${GRAPE_GSB_BASE_URL:-}" ]; then
    _ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [ -n "${_ip:-}" ]; then
        export GRAPE_GSB_BASE_URL="http://${_ip}:${port}"
    fi
fi

# ── 连接池上限（三库拓扑下四个进程共用一个 runtime 库）──────────────────────
# 默认 32 连接 × 4 进程 + 跨域连接会打满本地 runtime MySQL（默认 max_connections=151）。
# 打满后 pymysql 抛 1040，跨域读全部失败 → meta/scene_category/skill 满屏 1146。
# 这里按角色给保守上限（仍可用环境变量覆盖）：
#   worker 是 runtime 库的 owner，给最多；mgmt/event/insight 只做跨域读。
case "$role" in
    worker)  _pool_conn="${DB_POOL_MAX_CONNECTIONS:-48}"; _pool_cache="${DB_POOL_MAX_CACHED:-24}" ;;
    mgmt)    _pool_conn="${DB_POOL_MAX_CONNECTIONS:-16}"; _pool_cache="${DB_POOL_MAX_CACHED:-8}"  ;;
    event)   _pool_conn="${DB_POOL_MAX_CONNECTIONS:-12}"; _pool_cache="${DB_POOL_MAX_CACHED:-6}"  ;;
    insight) _pool_conn="${DB_POOL_MAX_CONNECTIONS:-12}"; _pool_cache="${DB_POOL_MAX_CACHED:-6}"  ;;
esac
export DB_POOL_MAX_CONNECTIONS="$_pool_conn"
export DB_POOL_MAX_CACHED="$_pool_cache"

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