#!/usr/bin/env bash
# 记录部署起点：用于事后校验每个进程确实是在本次部署之后启动的。
# 只看端口 200 会被「脚本没重启、旧进程还在应答」骗过去 —— 2026-11-15 就是这样
# 连续多轮以为部署成功（116 上 systemd 托管后本脚本直接退出，旧代码一直在跑）。
_DEPLOY_T0=$(date +%s)
# 三库独立模式：一套独立实例（38081 前端+API / 38082 event / 38083 worker / 38084 insight）
# 库：stage 三库（grape_stage_mgmt / grape_stage_runtime / grape_stage_insight），
#     由 GRAPE_DOMAIN_LIB=1 + GRAPE_DSN_* 指定，互不干扰生产（38081-38084）。
set -u

# 已被 systemd 托管时禁止执行本脚本：nohup 进程杀不干净 / Restart=always 会再拉一遍，
# 两边抢同一端口 → address already in use，只有一个能绑上，另一个静默空转。
# 切换/回滚方法见 scripts/install-grape-systemd.sh。
# 只有「单元真的在运行」才与本脚本冲突（两边抢同一端口）。
# 仅 enabled、但当前 inactive 的单元不该阻止本脚本 —— 那只是「开机自启已登记」，
# 本机曾因此被误判，脚本静默退出、旧进程继续应答，看起来部署成功其实没重启。
_units_on=0
_units_enabled=""
for _r in mgmt event worker insight; do
  if systemctl is-active -q "grape@$_r.service" 2>/dev/null; then
    _units_on=1
  elif [ -L "/etc/systemd/system/multi-user.target.wants/grape@$_r.service" ]; then
    _units_enabled="$_units_enabled $_r"
  fi
done
if [ -n "$_units_enabled" ]; then
  echo "[3db] 提示: 这些角色已 enable 但当前未运行:$_units_enabled" >&2
  echo "[3db]       若要用 systemd 托管: systemctl start grape@{mgmt,event,worker,insight}" >&2
fi
# 运行中的 systemd 单元 → 本脚本作为统一部署入口，内部改用 systemctl restart。
# 不再「检测到就退出」：那样脚本静默空跑、旧进程继续应答，看起来部署成功其实没重启。
_SYSTEMD_MODE=0
if [ "$_units_on" = "1" ]; then
  _SYSTEMD_MODE=1
  echo "[3db] systemd 模式：将通过 systemctl restart grape@{mgmt,event,worker,insight} 部署"
  echo "[3db] （回到 nohup 方式: systemctl disable --now grape@{mgmt,event,worker,insight}）"
fi
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

if [ "$_SYSTEMD_MODE" = "1" ]; then
  # 单元自己负责拉起进程（Restart=always / 启动顺序），这里只触发重启并等健康。
  # 三库 DSN 已在文件头 source 过（.grape-rds-stage.env），unit 侧另有 GRAPE_3DB_ENV drop-in。
  systemctl restart grape@mgmt grape@event grape@worker grape@insight \
    || { echo "[3db] FAIL: systemctl restart 失败" >&2; exit 1; }
else
  stop_ports
  GRAPE_GSB_SELFTEST_INTERVAL=60 \
  start_one mgmt    src.api.grape_server         38081 39081 "$GRAPE_DSN_MGMT"
  start_one event   src.api.grape_event_server   38082 39082 "$GRAPE_DSN_RUNTIME"
  start_one worker  src.api.grape_worker_server  38083 39083 "$GRAPE_DSN_RUNTIME"
  start_one insight src.api.grape_insight_server 38084 39084 "$GRAPE_DSN_INSIGHT"
fi

echo "[3db] DOMAIN_LIB=$GRAPE_DOMAIN_LIB  mgmt=$GRAPE_DSN_MGMT" | sed 's#//[^@]*@#//***@#g'
# 启动瞬态：进程可能仍在迁移/建连（2026-11-15 116 的 insight 卡在连接池初始化之后
# 两分钟不监听端口，重启一次即好）。先等一轮，仍不齐就对没起来的角色单独重启一次再等一轮。
_healthy_pids() {
  local up=0 p
  for p in 38081 38082 38083 38084; do
    path=/api/health; case $p in 38082|38083) path=/health ;; esac
    curl -s -m 2 "http://127.0.0.1:$p$path" >/dev/null 2>&1 && up=$((up+1))
  done
  echo "$up"
}
_wait_healthy() {
  local i
  for i in $(seq 1 30); do
    [ "$(_healthy_pids)" -eq 4 ] && return 0
    sleep 2
  done
  return 1
}
if _wait_healthy; then
  echo "[3db] all 4 processes healthy"
else
  _stuck=""
  for pair in "38081:mgmt" "38082:event" "38083:worker" "38084:insight"; do
    _p=${pair%%:*}; _role=${pair##*:}
    path=/api/health; case $_p in 38082|38083) path=/health ;; esac
    code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$_p$path" 2>/dev/null)
    [ "$code" != "200" ] && _stuck="$_stuck $_role"
  done
  if [ -n "$_stuck" ]; then
    echo "[3db] WARN: 这些角色未就绪:$_stuck —— 判定为启动瞬态，单独重启一次再等一轮"
    for _role in $_stuck; do
      if [ "$_SYSTEMD_MODE" = "1" ]; then
        systemctl restart "grape@$_role" 2>/dev/null || true
      fi
    done
    if _wait_healthy; then
      echo "[3db] all 4 processes healthy (after one restart)"
    else
      echo "[3db] FAIL: 重启一次后仍未全部就绪（见下方 status）" >&2
      _unhealthy=1
    fi
  fi
fi
echo "[3db] status:"
for p in 38081 38082 38083 38084; do
  path=/api/health; case $p in 38082|38083) path=/health ;; esac
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p$path")
  echo "  127.0.0.1:$p$path -> $code"
done
# 每个模块必须恰好一个进程：多实例会抢同一端口，只有一个绑得上，另一个静默空转
for mod in $_MODS; do
  # pgrep -c 在没有匹配时自己就输出 0 并以 0 退出，不能再 `|| echo 0`（会变成两行）
  n=$(pgrep -cf "uvicorn src.api.$mod:app" 2>/dev/null); [ -z "$n" ] && n=0
  [ "$n" -ne 1 ] && echo "[3db] WARN: $mod 进程数=$n（期望 1）"
done
# 启动时间校验：进程必须是本次部署之后才起来的，否则说明它根本没被重启（还在跑旧代码）。
# 端口 200 不能证明代码是新的 —— 旧进程也 200。
_stale_procs=0
_btime=$(awk '/^btime/{print $2}' /proc/stat 2>/dev/null || echo 0)
for pair in "mgmt:grape_server" "event:grape_event_server" "worker:grape_worker_server" "insight:grape_insight_server"; do
  _role=${pair%%:*}; _mod=${pair##*:}
  _pid=$(pgrep -f "uvicorn src.api.$_mod:app" 2>/dev/null | head -1)
  [ -z "$_pid" ] && continue
  _ticks=$(awk '{print $22}' "/proc/$_pid/stat" 2>/dev/null || echo "")
  [ -z "$_ticks" ] && continue
  _started=$(( _btime + _ticks / 100 ))
  if [ "$_started" -lt "$((_DEPLOY_T0 - 5))" ]; then
    echo "[3db] ERROR: $_role pid=$_pid 启动于 $(date -d @$_started '+%H:%M:%S')，早于本次部署起点 $(date -d @$_DEPLOY_T0 '+%H:%M:%S') —— 进程没被重启，仍在跑旧代码"
    _stale_procs=1
  fi
done
if [ "$_stale_procs" -ne 0 ]; then
  echo "[3db] FAIL: 有进程未随本次部署重启（见上），代码未生效"
  exit 1
fi
if [ "${_unhealthy:-0}" -ne 0 ]; then
  echo "[3db] FAIL: 有角色重启一次后仍未就绪（见上方 status）" >&2
  exit 1
fi

# agent 执行进程必须来自本部署目录：旧代码目录残留的 agent 会抢到任务、却跑旧逻辑
# （不建引擎会话、零消息），事件层随后判 failed —— 2026-11-15 在 116 上复现过。
for pid in $(pgrep -f "python.*-m src.domains.runtime.engine.agent" 2>/dev/null); do
  # 判据用 working directory 而不是解释器路径：venv 允许复用（orgtest 就是复用旧 venv
  # 跑新代码），真正决定跑哪份代码的是进程的 cwd。
  _cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || echo "")
  _argv0=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -1)
  case "$_cwd" in
    "$REPO"|"$REPO"/*|*grape-new-version2*) : ;;
    *)
      echo "[3db] WARN: agent pid=$pid 工作目录不在部署目录：$_cwd（旧代码残留，会抢任务但不执行）"
      ;;
  esac
done