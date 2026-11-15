#!/usr/bin/env bash
# 把 Grape 四进程从「nohup 手工拉起（deploy/deploy3db.sh）」切换到 systemd 托管。
#
# 为什么必须托管：nohup 拉起的进程 PPID=1、无人监管，崩溃/重启后不会自愈；而
# grape@.service 带 Restart=always + RestartSec=5 + KillSignal=SIGINT，是最小改动就能
# 拿到自愈能力的路径（也是 .env / drop-in / 日志路径的既有约定）。
#
# 本脚本做的事（幂等，可重复执行）：
#   1. 落一份三库 DSN 环境文件到 /etc/grape/grape-3db.env（root:grape 0640，不写进 .env）；
#   2. 给四个角色写 drop-in：GRAPE_3DB_ENV（三库模式）+ 按角色的 GRAPE_GSB_BASE_URL；
#   3. 停掉 nohup 遗留进程（pid 文件 + 端口兜底），避免和 Restart=always 抢端口；
#   4. systemctl daemon-reload + enable --now grape@{mgmt,event,worker,insight}；
#   5. 等 4/4 健康，打印 PID / MainPID 对照表（MainPID == /health 的 pid 才算真托管）。
#
# 用法（需要 root）：
#   sudo scripts/install-grape-systemd.sh --3db-env /home/grape/.grape-rds-stage.env
#   sudo scripts/install-grape-systemd.sh --3db-env /etc/grape/grape-3db.env --node-ip 10.0.0.11
#   sudo scripts/install-grape-systemd.sh --dry-run
#
# 回滚（回到 nohup 方式）：
#   sudo systemctl disable --now grape@mgmt grape@event grape@worker grape@insight
#   rm -f /etc/systemd/system/grape@*.service.d/20-3db.conf && sudo systemctl daemon-reload
#   bash deploy/deploy3db.sh
set -euo pipefail

ROLES="mgmt event worker insight"
UNIT_DIR=/etc/systemd/system
ENV_DIR=/etc/grape
ENV_FILE="$ENV_DIR/grape-3db.env"
DEFAULT_STAGE_ENV=/home/grape/.grape-rds-stage.env
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_IP=""
DRY_RUN=0
SRC_ENV=""

while [ $# -gt 0 ]; do
    case "$1" in
        --3db-env) SRC_ENV="${2:-}"; shift 2 ;;
        --repo)    REPO="${2:-}"; shift 2 ;;
        --node-ip) NODE_IP="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

say() { echo "[supervise] $*"; }
run() {
    if [ "$DRY_RUN" = "1" ]; then echo "[dry-run] $*"; else eval "$@"; fi
}

# ── 0. 前置检查 ───────────────────────────────────────────────────────────────
if [ "$(id -u)" != "0" ] && [ "$DRY_RUN" != "1" ]; then
    echo "需要 root：sudo $0 $*（只做体检不看改动可加 --dry-run）" >&2
    exit 1
fi
[ -x "$REPO/venv/bin/python" ] || { echo "找不到 venv：$REPO/venv/bin/python" >&2; exit 1; }
[ -f "$UNIT_DIR/grape@.service" ] || { echo "缺少 $UNIT_DIR/grape@.service（先安装 unit 模板）" >&2; exit 1; }

# 三库 DSN 源文件：优先 --3db-env，其次本机既有的 stage 环境文件
if [ -z "$SRC_ENV" ] && [ -r "$DEFAULT_STAGE_ENV" ]; then
    SRC_ENV="$DEFAULT_STAGE_ENV"
fi
USE_3DB=0
if [ -n "$SRC_ENV" ]; then
    [ -r "$SRC_ENV" ] || { echo "三库 DSN 文件不可读：$SRC_ENV" >&2; exit 1; }
    for _v in GRAPE_DSN_MGMT GRAPE_DSN_RUNTIME GRAPE_DSN_INSIGHT; do
        grep -q "^${_v}=mysql://" "$SRC_ENV" || {
            echo "$SRC_ENV 缺少 $_v=mysql://...（三库模式需要三条 DSN）" >&2; exit 1; }
    done
    USE_3DB=1
else
    say "未提供三库 DSN 文件 → 按单库/领域库模式托管（等价于 grape-systemd-run.sh 原行为）"
fi

if [ -z "$NODE_IP" ]; then
    NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
[ -n "$NODE_IP" ] || { echo "拿不到本机私网 IP，请用 --node-ip 指定（GSB 注册地址必须跨节点可达）" >&2; exit 1; }

say "repo=$REPO node_ip=$NODE_IP 3db=$USE_3DB 源DSN=${SRC_ENV:-<none>}"

# ── 1. 三库 DSN 环境文件 ─────────────────────────────────────────────────────
if [ "$USE_3DB" = "1" ]; then
    if [ "$(readlink -f "$SRC_ENV")" != "$(readlink -f "$ENV_FILE" 2>/dev/null || echo "$ENV_FILE")" ]; then
        run "install -d -m 750 $ENV_DIR"
        run "install -m 640 -g grape '$SRC_ENV' '$ENV_FILE'"
        say "DSN 文件 → $ENV_FILE (root:grape 640)"
    else
        say "DSN 文件已在 $ENV_FILE（原地使用）"
    fi
    # 服务以 User=grape 运行，必须能读
    if [ "$DRY_RUN" = "0" ]; then
        runuser -u grape -- test -r "$ENV_FILE" || { echo "grape 用户读不到 $ENV_FILE" >&2; exit 1; }
    fi
fi

# ── 2. 每角色 drop-in ────────────────────────────────────────────────────────
declare -A PORT=( [mgmt]=38081 [event]=38082 [worker]=38083 [insight]=38084 )
for role in $ROLES; do
    dropin_dir="$UNIT_DIR/grape@$role.service.d"
    dropin="$dropin_dir/20-3db.conf"
    run "install -d -m 755 $dropin_dir"
    body="[Service]\n# 由 scripts/install-grape-systemd.sh 生成：systemd 等价于 deploy/deploy3db.sh\nEnvironment=GRAPE_GSB_BASE_URL=http://${NODE_IP}:${PORT[$role]}\n"
    if [ "$USE_3DB" = "1" ]; then
        body="${body}Environment=GRAPE_3DB_ENV=${ENV_FILE}\n"
    fi
    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] 写 $dropin:"; printf "%b" "$body" | sed 's/^/    /'
    else
        printf "%b" "$body" > "$dropin"
        say "drop-in → $dropin"
    fi
done

# ── 3. 停掉 nohup 遗留进程（否则会和 Restart=always 抢同一端口）──────────────
stop_nohup() {
    local stopped=0 role f pid port unit_pid
    # 1) 按 deploy3db.sh 的 pid 文件停（有 pid 文件 = 一定是 nohup 拉起的）
    for role in $ROLES; do
        f="/home/grape/3db-$role.pid"
        [ -f "$f" ] || continue
        pid="$(cat "$f" 2>/dev/null || true)"
        unit_pid="$(systemctl show -p MainPID --value "grape@$role.service" 2>/dev/null || echo 0)"
        if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null && [ "$pid" != "${unit_pid:-0}" ]; then
            say "停止 nohup 进程 $role pid=$pid"
            kill "$pid" 2>/dev/null || true
            stopped=1
        fi
        rm -f "$f"
    done
    [ "$stopped" = "1" ] && sleep 3
    # 2) 端口兜底：占着端口、又不是该单元 MainPID 的进程（会产生
    #    address already in use → 新单元起不来）
    for role in $ROLES; do
        port="${PORT[$role]}"
        pid="$(ss -tlnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $NF}' \
            | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
        [ -z "${pid:-}" ] && continue
        unit_pid="$(systemctl show -p MainPID --value "grape@$role.service" 2>/dev/null || echo 0)"
        if [ "$pid" != "${unit_pid:-0}" ]; then
            say "释放端口 $port（残留 pid=$pid 非 systemd MainPID）"
            kill "$pid" 2>/dev/null || true
        fi
    done
    sleep 1
}
[ "$DRY_RUN" = "1" ] && echo "[dry-run] stop_nohup()" || stop_nohup

# ── 4. enable + start ───────────────────────────────────────────────────────
run "systemctl daemon-reload"
units=""
for role in $ROLES; do units="$units grape@$role"; done
run "systemctl enable --now $units" || { echo "enable/start 失败，查 journalctl -u grape@mgmt -n 50" >&2; exit 1; }
if [ "$DRY_RUN" = "1" ]; then
    say "dry-run 结束：以上为将要执行的改动，未做 enable / 健康核对"
    exit 0
fi

# ── 5. 健康核对 ─────────────────────────────────────────────────────────────
health_path() { case "$1" in mgmt|insight) echo /api/health ;; *) echo /health ;; esac; }

ok=0
for _ in $(seq 1 30); do
    up=0
    for role in $ROLES; do
        curl -s -m 2 "http://127.0.0.1:${PORT[$role]}$(health_path "$role")" >/dev/null 2>&1 && up=$((up+1))
    done
    [ "$up" -eq 4 ] && { ok=1; break; }
    sleep 2
done

printf '\n%-8s %-7s %-9s %-9s %-8s %s\n' ROLE PORT UNIT_PID HEALTH_PID STATE HTTP
for role in $ROLES; do
    port="${PORT[$role]}"
    unit_pid="$(systemctl show -p MainPID --value "grape@$role.service" 2>/dev/null || echo 0)"
    health_pid="$(curl -s -m 3 "http://127.0.0.1:$port$(health_path "$role")" 2>/dev/null \
        | python3 -c 'import json,sys;print(json.load(sys.stdin).get("pid","-"))' 2>/dev/null || echo -)"
    state="$(systemctl is-active "grape@$role.service" 2>/dev/null || true)"
    code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port$(health_path "$role")" 2>/dev/null || echo 000)"
    printf '%-8s %-7s %-9s %-9s %-8s %s\n' "$role" "$port" "$unit_pid" "$health_pid" "$state" "$code"
done

echo
if [ "$ok" = "1" ]; then
    say "四进程已由 systemd 托管并通过健康检查（Restart=always 生效，MainPID 应为 systemctl 所有）"
else
    say "WARN: 未达到 4/4 健康，查 journalctl -u grape@mgmt -n 80 / /home/grape/grape-logs/*.log" >&2
fi
cat <<'NEXT'

后续：
  重启全部角色   systemctl restart grape@mgmt grape@event grape@worker grape@insight
  单角色重启     systemctl restart grape@worker
  查状态         systemctl status grape@mgmt --no-pager -n 0
  验证自愈       kill -9 $(systemctl show -p MainPID --value grape@worker)；约 5s 后自动拉起
  回滚到 nohup   systemctl disable --now grape@{mgmt,event,worker,insight} && bash deploy/deploy3db.sh

注意：启用托管后 deploy/deploy3db.sh 会拒绝执行（会与 Restart=always 抢端口）。
NEXT