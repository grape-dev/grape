#!/usr/bin/env bash
# =============================================================================
# new-version2 回滚脚本（服务端执行）
#
# 用法：
#   bash rollback.sh code <commit>            # 代码回滚到指定提交
#   bash rollback.sh db   <backup-file.db>    # 数据库回滚到指定备份
#   bash rollback.sh db-list                  # 列出可用数据库备份
#   bash rollback.sh mode  direct|queue_local # 仅切回执行模式
#
# 注意：
#   - 代码回滚前若已跑远端 worker，先停 grape-runtime，避免新旧协议混跑
#   - 数据库回滚会丢失备份后的新数据，谨慎执行
# =============================================================================
set -euo pipefail

APP_DIR="/opt/grape"
DB="$APP_DIR/data/sessions.db"
BACKUP_DIR="/root/backups"

CMD="${1:-}"
ARG="${2:-}"

if [ -z "$CMD" ]; then
  echo "用法: $0 code <commit> | db <backup.db> | db-list | mode <direct|queue_local|queue_remote>"
  exit 1
fi

case "$CMD" in
  db-list)
    echo "=== 可用数据库备份 ==="
    ls -lh "$BACKUP_DIR"/sessions-*.db 2>/dev/null || echo "（无备份）"
    ;;

  db)
    if [ -z "$ARG" ] || [ ! -f "$ARG" ]; then
      echo "❌ 备份文件不存在: $ARG"; exit 1
    fi
    echo "=== 数据库回滚 ==="
    # 先备份当前库（回滚前的现场）
    TS="$(date +%F_%H%M%S)"
    cp -a "$DB" "$BACKUP_DIR/sessions-pre-rollback-${TS}.db"
    echo "已保存当前现场 -> $BACKUP_DIR/sessions-pre-rollback-${TS}.db"
    # 停服还原
    systemctl stop grape || true
    cp -a "$ARG" "$DB"
    rm -f "$DB-wal" "$DB-shm"
    echo "已还原: $ARG"
    systemctl start grape
    echo "服务已重启，请用 verify.sh 校验。"
    ;;

  code)
    if [ -z "$ARG" ]; then echo "❌ 缺少 commit"; exit 1; fi
    echo "=== 代码回滚到 $ARG ==="
    systemctl stop grape || true
    cd "$APP_DIR"
    git fetch origin
    git checkout "$ARG"
    systemctl start grape
    echo "服务已重启（$ARG）。"
    ;;

  mode)
    if [ -z "$ARG" ]; then echo "❌ 缺少 mode"; exit 1; fi
    if [ "$ARG" != "direct" ] && [ "$ARG" != "queue_local" ] && [ "$ARG" != "queue_remote" ]; then
      echo "❌ mode 必须是 direct/queue_local/queue_remote"; exit 1
    fi
    echo "=== 切换执行模式 -> $ARG ==="
    DROP="/etc/systemd/system/grape.service.d"
    mkdir -p "$DROP"
    cat > "$DROP/execmode.conf" <<EOF
[Service]
Environment=DEVOPS_EXECUTION_MODE=$ARG
EOF
    systemctl daemon-reload
    systemctl restart grape
    echo "已切到 $ARG。"
    ;;

  *)
    echo "未知命令: $CMD"; exit 1 ;;
esac
