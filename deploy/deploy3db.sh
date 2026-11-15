#!/usr/bin/env bash
# 三库独立模式：一套独立实例（38081 前端+API / 38082 event / 38083 worker / 38084 insight）
# 库：stage 三库（grape_stage_mgmt / grape_stage_runtime / grape_stage_insight），
#     由 GRAPE_DOMAIN_LIB=1 + GRAPE_DSN_* 指定，互不干扰生产（38081-38084）。
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOGS=/home/grape/grape-logs
mkdir -p "$LOGS"
cd "$REPO" || exit 1

set -a
[ -f .env ] && . ./.env
. /home/grape/.grape-rds-stage.env
set +a
export GRAPE_DOMAIN_LIB=1
export PYTHONUNBUFFERED=1
export PATH="$REPO/venv/bin:$PATH"

PY="$REPO/venv/bin/python"

# 引擎探测需要 HOME 与 CLI 搜索路径：hermes/opencode 装在用户目录下
# （~/.local/bin、~/.local/npm-global/bin）。缺 HOME 时探测把引擎判成 healthy=0，
# worker 认领闸门会永久拒绝（116 实测：claim_by_tag ... no verified executor，每 3s 刷屏）。
export HOME="${HOME:-/root}"
export PATH="$HOME/.local/bin:$HOME/.local/npm-global/bin:/usr/local/bin:$PATH"

_MODS="grape_server grape_event_server grape_worker_server grape_insight_server"

stop_ports() {
  # 1) 按 pid 文件停：覆盖「起了但没绑端口」的进程（只按端口 kill 会漏掉它们，
  #    残留进程随后抢端口 → address already in use → 新进程起不来，116 踩过两次）
  for role in mgmt event worker insight; do
    f="/home/grape/$role.pid"
    [ -f "$f" ] && { kill "$(cat "$f")" 2>/dev/null; rm -f "$f"; }
  done
  # 2) 按监听端口兜底
  for port in 38081 38082 38083 38084; do
    pid=$(ss -tlnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $NF}' | grep -oP 'pid=\K[0-9]+' | head -1)
    [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null
  done
  sleep 2
  # 3) 按模块名清孤儿（含未绑端口的），顽固不走的 SIGKILL
  for mod in $_MODS; do
    for pid in $(pgrep -f "uvicorn src.api.$mod:app" 2>/dev/null); do kill "$pid" 2>/dev/null; done
  done
  sleep 2
  for mod in $_MODS; do
    for pid in $(pgrep -f "uvicorn src.api.$mod:app" 2>/dev/null); do kill -9 "$pid" 2>/dev/null; done
  done
  sleep 1
  # 4) 断言端口已释放，否则后续启动必然撞 address already in use
  for port in 38081 38082 38083 38084; do
    ss -tln 2>/dev/null | grep -q ":$port " && echo "[3db] WARN: port $port still bound after stop"
  done
}

start_one() {
  role=$1; module=$2; port=$3; mport=$4; dsn=$5
  # 主库也指向该角色的 stage 库（三库模式下进程主库=本域库，*_any 原语再走各自 DSN）
  _c=${dsn#mysql://}; _u=${_c%%:*}; _r=${_c#*:}; _p=${_r%%@*}; _hp=${_r#*@}
  _h=${_hp%%:*}; _pp=${_hp#*:}; _pp=${_pp%%/*}; _d=${_hp#*/}
  DEVOPS_DB_HOST="$_h" DEVOPS_DB_PORT="$_pp" DEVOPS_DB_USER="$_u" \
  DEVOPS_DB_PASSWORD="$_p" DEVOPS_DB_NAME="$_d" \
  METRICS_ADDR="127.0.0.1:$mport" GRAPE_SERVICE_NAME="$role" \
  GRAPE_OTLP_ENDPOINT="http://127.0.0.1:4317" GRAPE_TRACE_SERVICE="grape-$role" \
  GRAPE_TRACE_RATIO="0.5" \
  nohup "$PY" -m uvicorn "$module:app" --host 0.0.0.0 --port "$port" --workers 1 \
    >> "$LOGS/$role.log" 2>&1 &
  echo $! > "/home/grape/$role.pid"
}

stop_ports
GRAPE_GSB_SELFTEST_INTERVAL=60 \
start_one mgmt    src.api.grape_server         38081 39081 "$GRAPE_DSN_MGMT"
start_one event   src.api.grape_event_server   38082 39082 "$GRAPE_DSN_RUNTIME"
start_one worker  src.api.grape_worker_server  38083 39083 "$GRAPE_DSN_RUNTIME"
start_one insight src.api.grape_insight_server 38084 39084 "$GRAPE_DSN_INSIGHT"

echo "[3db] DOMAIN_LIB=$GRAPE_DOMAIN_LIB  mgmt=$GRAPE_DSN_MGMT" | sed 's#//[^@]*@#//***@#g'
for _ in $(seq 1 40); do
  up=0
  for p in 38081 38082 38083 38084; do
    path=/api/health; case $p in 38082|38083) path=/health ;; esac
    curl -s -m 2 "http://127.0.0.1:$p$path" >/dev/null 2>&1 && up=$((up+1))
  done
  [ "$up" -eq 4 ] && { echo "[3db] all 4 processes healthy"; break; }
  sleep 2
done
echo "[3db] status:"
for p in 38081 38082 38083 38084; do
  path=/api/health; case $p in 38082|38083) path=/health ;; esac
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p$path")
  echo "  127.0.0.1:$p$path -> $code"
done
# 每个模块必须恰好一个进程：多实例会抢同一端口，只有一个绑得上，另一个静默空转
for mod in $_MODS; do
  n=$(pgrep -cf "uvicorn src.api.$mod:app" 2>/dev/null || echo 0)
  [ "${n:-0}" -ne 1 ] && echo "[3db] WARN: $mod 进程数=$n（期望 1）"
done
# agent 执行进程必须来自本部署目录：旧代码目录残留的 agent 会抢到任务、却跑旧逻辑
# （不建引擎会话、零消息），事件层随后判 failed —— 2026-11-15 在 116 上复现过。
for pid in $(pgrep -f "python.*-m src.domains.runtime.engine.agent" 2>/dev/null); do
  _argv0=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -1)
  case "$_argv0" in
    "$REPO"/venv/*|"$REPO"/.venv/*) : ;;
    *) echo "[3db] WARN: agent pid=$pid 解释器不在部署目录：$_argv0（旧代码残留，会抢任务但不执行）" ;;
  esac
done