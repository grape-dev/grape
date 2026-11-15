#!/usr/bin/env bash
# 重启 Grape 四进程（Mgmt/Event/Worker/Insight），启用独立 metrics 端口（METRICS_ADDR）。
# 用法：./scripts/restart-four-process.sh
#
# 端口规划：
#   Mgmt    38081 / metrics 39081
#   Event   38082 / metrics 39082
#   Worker  38083 / metrics 39083
#   Insight 38084 / metrics 39084
# metrics 端口全部绑定 127.0.0.1（本机 Alloy 抓取，无外网暴露）。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

# 已切换为 systemd 托管（grape@.service）时禁止用本脚本：本脚本会杀掉 systemd 的子进程，
# 而单元重启策略 Restart=always 会把它们再拉起来 → 端口冲突/双实例。改用：
#   su -c "systemctl restart grape@mgmt grape@event grape@worker grape@insight" root
if [ "$(systemctl is-enabled grape@mgmt.service 2>/dev/null)" = "enabled" ]; then
    echo "[restart] 检测到 systemd 单元 grape@*.service（已 enable），本脚本不再适用。" >&2
    echo "[restart] 请改用: su -c \"systemctl restart grape@mgmt grape@event grape@worker grape@insight\" root" >&2
    exit 3
fi

PY="$REPO_ROOT/venv/bin/python"
LOG_PREFIX="/tmp/grape"

# 加载 .env（uvicorn 直启不读 .env；JWT_SECRET 等共享配置必须四进程一致，
# 否则各进程 fallback 到进程内随机密钥 → 跨进程 JWT 校验 401 死循环）
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
    echo "[restart] loaded .env ($(grep -cve '^\s*$' -e '^\s*#' "$REPO_ROOT/.env") vars)"
fi

stop_old() {
    for port in 38081 38082 38083 38084; do
        pid=$(ss -tlnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $NF}' | grep -oP 'pid=\K[0-9]+' | head -1)
        if [ -n "$pid" ]; then
            echo "[restart] killing old PID $pid (port $port)"
            kill "$pid" 2>/dev/null
        fi
    done
    sleep 2
    # 兜底：还没退出的强杀
    for port in 38081 38082 38083 38084; do
        pid=$(ss -tlnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $NF}' | grep -oP 'pid=\K[0-9]+' | head -1)
        [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null
    done
}

start_one() {
    local role="$1" module="$2" port="$3" metrics_port="$4" log="$5"
    echo "[restart] starting $role on :$port (metrics :$metrics_port)"
    METRICS_ADDR="127.0.0.1:$metrics_port" \
    GRAPE_OTLP_ENDPOINT="${GRAPE_OTLP_ENDPOINT:-http://127.0.0.1:4317}" \
    GRAPE_TRACE_SERVICE="grape-$role" \
    GRAPE_TRACE_RATIO="${GRAPE_TRACE_RATIO:-0.5}" \
        nohup "$PY" -m uvicorn "$module:app" --host 0.0.0.0 --port "$port" --workers 1 \
        >> "${LOG_PREFIX}-${log}.log" 2>&1 &
    echo $! > "/tmp/grape-four-${role}.pid"
}

stop_old

start_one mgmt    src.api.grape_server 38081 39081 mgmt
start_one event   src.api.grape_event_server 38082 39082 event
start_one worker  src.api.grape_worker_server 38083 39083 worker
start_one insight src.api.grape_insight_server 38084 39084 insight

echo "[restart] waiting for health checks..."
# Event/Worker 只挂 /health（loopback 专用 liveness），Mgmt/Insight 另有 /api/health
_health_path_for() {
    case "$1" in
        38081|38084) echo "/api/health" ;;
        *)            echo "/health" ;;
    esac
}
ok=0
for _ in $(seq 1 30); do
    up=0
    for port in 38081 38082 38083 38084; do
        curl -s -m 2 "http://127.0.0.1:$port$(_health_path_for "$port")" >/dev/null 2>&1 && up=$((up+1))
    done
    [ "$up" -eq 4 ] && { ok=1; break; }
    sleep 2
done

if [ "$ok" -eq 1 ]; then
    echo "[restart] all 4 processes healthy."
else
    echo "[restart] WARNING: not all processes healthy, check /tmp/grape-*.log"
fi

# metrics 端点验证
echo "[restart] metrics endpoints:"
for mport in 39081 39082 39083 39084; do
    status=$(curl -s -m 2 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$mport/metrics")
    echo "  127.0.0.1:$mport/metrics -> $status"
done

# 发布后写路径冒烟：真提交一句 chat 并断言 200 + task_id（默认自动撤回）。
# 断连 500（InterfaceError(0,'')）在 health / 只读端点上不可见，必须走一次写路径才拦得住。
if [ "$ok" -eq 1 ]; then
    echo "[restart] chat write-path smoke:"
    if "$PY" scripts/smoke_chat_submit.py --base-url "http://127.0.0.1:38081"; then
        echo "[restart] chat smoke OK"
    else
        rc=$?
        if [ "$rc" -eq 1 ]; then
            echo "[restart] ERROR: chat write-path smoke FAILED（发布闸门判红）"
            exit 1
        fi
        echo "[restart] WARN: chat smoke SKIP（环境不具备，不阻断）"
    fi
fi
