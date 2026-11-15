#!/usr/bin/env bash
# P4/P5 彩排循环：确保三实例在线 → 建库灌数据 → 环境验收(A1/A2/A6) → 全 API 冒烟 → 余量报告
# 任一步红就再来一轮（库/数据会重建，天然幂等）；全绿则退出 0。
#
# 凭据一律走环境变量（不写进仓库）：
#   SRC_HOST/SRC_USER/SRC_PWD        参照库（数据来源）
#   REMOTE_HOST/REMOTE_PORT/REMOTE_USER/REMOTE_PWD   兄弟节点实例（runtime）
#   REMOTE_SSH_USER/REMOTE_SSH_PWD   兄弟节点重启用的 SSH（可选，仅在实例掉线时用）
#   LOOP_MAX                         最多几轮（默认 3）
#   STAGE_ENV                        生成的 stage DSN 文件（默认 ~/.grape-rds-stage.env）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
PY=${PY:-venv/bin/python}
LOOP_MAX=${LOOP_MAX:-3}
STAGE_ENV=${STAGE_ENV:-$HOME/.grape-rds-stage.env}
STAGE_DB_USER=${STAGE_DB_USER:-root}
: "${STAGE_DB_PWD:?STAGE_DB_PWD 未设置：请通过环境变量或 ~/.grape-rds-stage.env 提供 stage 口令（仓库内不得有默认口令）}"
LOCAL_MGMT_PORT=${LOCAL_MGMT_PORT:-3311}
LOCAL_INSIGHT_PORT=${LOCAL_INSIGHT_PORT:-3312}

say() { echo -e "\n=== $* ==="; }

ensure_local() {  # $1=port  $2=datadir  $3=socket
  ss -ltn 2>/dev/null | grep -q ":$1 " && return 0
  echo "[ensure] 启动本机实例 :$1"
  setsid "$HOME/bin/mysqld8" --no-defaults --user="$(id -un)" --datadir="$2" --port="$1" \
    --bind-address=0.0.0.0 --socket="$3" --mysqlx=0 --pid-file="$2/mysqld.pid" \
    --log-error="$2/error.log" --secure-file-priv="$2/files" \
    --innodb-buffer-pool-size=256M --skip-name-resolve \
    --max-connections="${RDS_MAX_CONNECTIONS:-500}" </dev/null >/dev/null 2>&1 &
  for _ in $(seq 1 20); do sleep 1; ss -ltn 2>/dev/null | grep -q ":$1 " && return 0; done
  echo "[ensure] :$1 未起来（看 $2/error.log）" >&2; return 1
}

ensure_remote() {
  ss -ltn 2>/dev/null | grep -q ":$REMOTE_PORT .*0.0.0.0" && return 0
  nc -z -w3 "${REMOTE_HOST}" "${REMOTE_PORT}" 2>/dev/null && return 0
  echo "[ensure] 兄弟节点实例 ${REMOTE_HOST}:${REMOTE_PORT} 不在线，尝试重启（需 REMOTE_SSH_*）"
  : "${REMOTE_SSH_USER:?需要 REMOTE_SSH_USER}" "${REMOTE_SSH_PWD:?需要 REMOTE_SSH_PWD}"
  : "${REMOTE_RDS_HOME:?需要 REMOTE_RDS_HOME（远端 MySQL 实例目录，不含仓库内默认值）}"
  "$PY" - "$REMOTE_HOST" "$REMOTE_SSH_USER" "$REMOTE_SSH_PWD" "$REMOTE_PORT" "$REMOTE_RDS_HOME" <<'PYEOF'
import os, sys, paramiko
host, user, pwd, port, rds_home = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=pwd, timeout=15)
cmd = (f"setsid {rds_home}/bin/mysqld8 --no-defaults --user=root --datadir={rds_home}/runtime "
       f"--port={port} --bind-address=0.0.0.0 --socket=/tmp/mysql-runtime.sock --mysqlx=0 "
       f"--pid-file={rds_home}/runtime/mysqld.pid --log-error={rds_home}/runtime/error.log "
       f"--secure-file-priv={rds_home}/runtime/files --innodb-buffer-pool-size=512M "
       f"--skip-name-resolve --max-connections={os.environ.get('RDS_MAX_CONNECTIONS', '500')} "
       f"</dev/null >/dev/null 2>&1 & sleep 8; ss -ltn | grep {port} || true")
c.exec_command(cmd, timeout=60)
c.close()
PYEOF
}

for i in $(seq 1 "$LOOP_MAX"); do
  say "第 $i/$LOOP_MAX 轮 · 实例就绪检查"
  ensure_local "$LOCAL_MGMT_PORT" "$HOME/rds-local/mgmt" /tmp/mysql-mgmt.sock || true
  ensure_local "$LOCAL_INSIGHT_PORT" "$HOME/rds-local/insight" /tmp/mysql-insight.sock || true
  ensure_remote || true

  say "P4：建库 + 搬基表（视图不复制，账号只授本库）"
  "$PY" scripts/rds_p4_provision.py \
    --source-host "${SRC_HOST:?}" --source-user "${SRC_USER:-root}" --source-password "${SRC_PWD:?}" \
    --remote-host "${REMOTE_HOST:?}" --remote-port "${REMOTE_PORT:-3307}" \
    --remote-user "${REMOTE_USER:-rds_admin}" --remote-password "${REMOTE_PWD:?}" || { rc_p4=1; }

  say "P5：环境验收（A1 视图 0 / A2 授权仅本库 / A6 队列）"
  # shellcheck disable=SC1090
  set -a; . "$STAGE_ENV"; set +a
  "$PY" scripts/verify_domain_isolation.py --env-only \
    --dsn-mgmt "$GRAPE_DSN_MGMT" --dsn-runtime "$GRAPE_DSN_RUNTIME" --dsn-insight "$GRAPE_DSN_INSIGHT" \
    --account "mgmt=$(echo "$GRAPE_DSN_MGMT" | sed -E 's#mysql://([^:]+):.*#\1#')" \
    --account "runtime=$(echo "$GRAPE_DSN_RUNTIME" | sed -E 's#mysql://([^:]+):.*#\1#')" \
    --account "insight=$(echo "$GRAPE_DSN_INSIGHT" | sed -E 's#mysql://([^:]+):.*#\1#')" || { rc_p5=1; }

  say "全 API 冒烟（169 读 + 3 写场景 + 46 写端点探测）"
  "$PY" scripts/stage_api_smoke.py --with-ids --write --write-all \
    --db-user "$STAGE_DB_USER" --db-password "$STAGE_DB_PWD" || { rc_smoke=1; }

  say "余量报告"
  STAGE_DB_USER="$STAGE_DB_USER" STAGE_DB_PWD="$STAGE_DB_PWD" "$PY" scripts/rds_burndown.py --smoke || true

  if [ "${rc_p4:-0}${rc_p5:-0}${rc_smoke:-0}" = "000" ]; then
    echo; echo "RESULT: 环境侧全绿（P4 落库 + P5 A1/A2/A6 + 冒烟）；代码侧余量见上（A3/静态引用）"
    exit 0
  fi
  echo; echo "RESULT: 第 $i 轮未全绿（p4=${rc_p4:-0} p5=${rc_p5:-0} smoke=${rc_smoke:-0}），继续下一轮"
  unset rc_p4 rc_p5 rc_smoke
  sleep 5
done
echo "RESULT: $LOOP_MAX 轮后仍未全绿，见上面各步输出"
exit 1