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
# 库口令不能有内置默认值（等于把生产口令写进公开仓库）。
# 但只有**单库分支**需要它：三库模式下口令来自 GRAPE_3DB_ENV 里的 DSN，
# 这里若硬性必填会把三库部署一起打死（GRAPE_3DB_ENV 有值时不校验）。
if [ -z "${GRAPE_3DB_ENV:-}" ]; then
    : "${GRAPE_DOMAIN_DB_PASSWORD:?GRAPE_DOMAIN_DB_PASSWORD 未设置：单库模式需显式提供库口令（脚本不再内置默认值）}"
    export DEVOPS_DB_PASSWORD="$GRAPE_DOMAIN_DB_PASSWORD"
else
    export DEVOPS_DB_PASSWORD="${GRAPE_DOMAIN_DB_PASSWORD:-}"
fi

PY="$REPO_ROOT/venv/bin/python"
[ -x "$PY" ] || { echo "missing venv python: $PY" >&2; exit 3; }

# 引擎 CLI 装在服务用户目录下（~/.local/bin、~/.local/npm-global/bin），
# systemd 默认 PATH 只有 /usr/local/bin:/usr/bin:/bin —— 于是 `opencode --version` /
# `hermes --version` 探测全部找不到可执行文件 → healthy=0 → 健康分低 →
# claim_by_tag 拒绝认领 → 任务永远没人跑（2026-11-15 实证：本机四进程都健康、
# 心跳正常，但流水线一个任务都跑不动，根因就是这一行 PATH）。
# deploy3db.sh（nohup 路径）早有这段，systemd 路径漏了。
export HOME="${HOME:-/home/grape}"
export PATH="$HOME/.local/bin:$HOME/.local/npm-global/bin:$PATH"

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
    # 三库 = MySQL 拓扑。DSN 只导出 host/port/user/password/name，**后端开关不含在内**：
    # session_manager 的后端判定读 DEVOPS_DB_BACKEND > configs/db-backend.json > 默认 sqlite，
    # 缺这一行时四个进程会各自落到本地 sqlite（db-guard 却照旧打印 DSN，看起来像连了库）。
    export DEVOPS_DB_BACKEND=mysql
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

# 谁跑本域迁移：mgmt 走 init_database；runtime 表只由 event 跑（worker 与 event 共库，
# 重复跑会并发下发同一 DDL）；insight 由 insight 跑。
# 必须在上面 `set -a; . "$GRAPE_3DB_ENV"` **之后**按角色强制赋值 —— DSN 文件用 set -a
# 导入时，同名变量会把前面的导出覆盖掉（review F2）。
case "$role" in
    mgmt)    export GRAPE_MIGRATION_OWNER="mgmt" ;;
    event)   export GRAPE_MIGRATION_OWNER="runtime" ;;
    insight) export GRAPE_MIGRATION_OWNER="insight" ;;
    worker)  export GRAPE_MIGRATION_OWNER="" ;;
esac

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
# Issue #4203（QA 复审 Optional）：进程角色必须绑定到部署角色。grape@ 路径此前不导出
# GRAPE_PROCESS_ROLE，worker app 的 setdefault 会把它定成 "runtime"（域别名），
# 于是启动自检日志与 grape_process_memory_* 的 process 标签变成 process=runtime，
# 与静态单元（GRAPE_PROCESS_ROLE=worker）、runbook §3.1 和告警 {{ $labels.process }} 不一致。
# 四个角色在 _ROLE_DOMAIN / openapi_merge.ENTRY_MODULES / db_setup 中均有对应项。
export GRAPE_PROCESS_ROLE="$role"
# 旧埋点路径（insight/observability/tracing.py）只在这条变量**显式设置**时启用。
# 原来这里补了 127.0.0.1:4317 的默认值，等于把"没配"变成"配了"：新路径
# （src/kernel/otel.py，认 OTEL_EXPORTER_OTLP_ENDPOINT）因此永远不生效，而本机
# 通常没有 4317 监听，span 被静默发往死端口。保持 opt-in，不再补默认值。
if [ -n "${GRAPE_OTLP_ENDPOINT:-}" ]; then export GRAPE_OTLP_ENDPOINT; fi
# Issue #3749：worker trace 中继上游（LGTM OTLP/HTTP 基址，不含 /v1/traces）。
# 同样只在 /etc/grape/grape.env 显式配置时透传——不补默认值：没配就是禁用（503），
# 补一个本机不存在的默认端点只会把「没配」伪装成「配了」。
if [ -n "${OTLP_RELAY_TARGET:-}" ]; then export OTLP_RELAY_TARGET; fi
export GRAPE_TRACE_SERVICE="${GRAPE_TRACE_SERVICE:-grape-${role}}"
export GRAPE_TRACE_RATIO="${GRAPE_TRACE_RATIO:-0.5}"
export PYTHONUNBUFFERED=1

# exec：让 uvicorn 成为 systemd 的直接子进程（Type=simple 的 PID 1 级信号管理）。
# 不加 --workers：workers=1 时 uvicorn 在进程内直接跑 Server，SIGTERM 处理最干净。
#
# Issue #4203（PR #2092 复审 Critical）：生产 worker 的实际启动路径是
#   grape@worker.service → 本脚本 → uvicorn
# 预 bind launcher 若只落在静态单元 grape-worker-server.service 上，走 grape@ 的机器
# 拿不到「先监听再 lifespan」（OOM SIGKILL 后仍要等 uvicorn bind 才恢复接连接）。
# 因此 worker 角色在这里切到 scripts/start-worker.py；其它角色保持原 exec 语义。
if [ "$role" = "worker" ]; then
    # 端口以角色映射（$port）为唯一事实源：GRAPE_GSB_BASE_URL 按 $port 注册，若 .env /
    # EnvironmentFile 里的旧值（如 #3764 前的 38082）生效，worker 会监听 A 端口却按 B
    # 端口注册 → 网关 502。不一致直接 fail-fast，把来源打出来。
    if [ -n "${GRAPE_WORKER_PORT:-}" ] && [ "${GRAPE_WORKER_PORT}" != "$port" ]; then
        echo "GRAPE_WORKER_PORT=$GRAPE_WORKER_PORT 与本角色端口事实源 $port 不一致（来源：环境变量/EnvironmentFile）" >&2
        exit 6
    fi
    export GRAPE_WORKER_PORT="$port"
    export GRAPE_HOST="${GRAPE_HOST:-0.0.0.0}"
    # 生产既有语义是单进程（原命令无 --workers），保持 1；需要多 worker 由环境显式指定
    export GRAPE_UVICORN_WORKERS="${GRAPE_UVICORN_WORKERS:-1}"
    exec "$PY" "$REPO_ROOT/scripts/start-worker.py"
fi
exec "$PY" -m uvicorn "${module}:app" --host 0.0.0.0 --port "$port" --log-level info