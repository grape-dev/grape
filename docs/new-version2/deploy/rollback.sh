#!/usr/bin/env bash
# =============================================================================
# new-version2 回滚脚本（服务端执行）
#
# 用法：
#   bash rollback.sh code <commit|branch>   # 代码回滚到指定提交/分支（本地已存在即可，无需 fetch）
#   bash rollback.sh db   <backup-file.db>  # 仅数据库回滚（旧结构备份，见注意事项）
#   bash rollback.sh db-list                # 列出可用数据库备份
#   bash rollback.sh all  <backup-file.db> [commit|branch]
#                                           # 一键回滚：数据库 + 代码 同时还原（推荐）
#   bash rollback.sh mode  direct|queue_remote
#                                           # 仅切回执行模式（不碰代码/数据库）
#
# ⚠️ 重要注意事项：
#   1. 数据库与代码必须【同时】回滚（用 `all`），否则：
#      - 只还原旧 DB + 新代码 → 服务启动时 init_database() 会再次迁移 → DB 回滚无效
#      - 只回退代码 + 新 DB   → 旧代码查 backend_agents 等旧表/旧列 → 运行报错
#   2. 本脚本不做 `git fetch`：生产机 origin 为 HTTPS 且无凭证（git fetch 会因
#      "could not read Username" 失败，见 2026-08-08 部署教训）。回滚目标必须是
#      本地已有的提交/分支（如 main）。若需拉取远端新提交，改用 deploy 流程注入 token。
#   3. 回滚前若已跑远端 worker，先停 grape-runtime，避免新旧协议混跑。
#   4. 数据库回滚会丢失备份之后的新数据，谨慎执行。
# =============================================================================
set -euo pipefail

APP_DIR="/opt/grape"
DB="$APP_DIR/data/sessions.db"
BACKUP_DIR="/root/backups"

CMD="${1:-}"
ARG="${2:-}"
ARG2="${3:-}"

if [ -z "$CMD" ]; then
  echo "用法: $0 code <commit|branch> | db <backup.db> | db-list | all <backup.db> [commit|branch] | mode <direct|queue_remote>"
  exit 1
fi

# ---- 工具函数 ----

# 校验备份文件存在
require_backup() {
  if [ -z "$ARG" ] || [ ! -f "$ARG" ]; then
    echo "❌ 备份文件不存在: ${ARG:-（空）}"; exit 1
  fi
}

# 校验回滚目标提交/分支在本地存在（不 fetch，见注意 2）
require_local_ref() {
  local ref="${1:-}"
  if [ -z "$ref" ]; then
    echo "❌ 缺少回滚目标（commit 或分支名）"; exit 1
  fi
  if ! git -C "$APP_DIR" cat-file -e "$ref^{commit}" 2>/dev/null; then
    echo "❌ 本地不存在该提交/分支: $ref（不执行 git fetch；若需远端提交请用 deploy 流程注入 token）"
    echo "   本地可用分支: $(git -C "$APP_DIR" branch --format='%(refname:short)' 2>/dev/null | tr '\n' ' ')"
    exit 1
  fi
}

# 停止服务（容忍失败）
stop_service() {
  systemctl stop grape 2>/dev/null || true
}

# 启动服务
start_service() {
  systemctl start grape
  echo "服务已重启。"
}

case "$CMD" in
  db-list)
    echo "=== 可用数据库备份 ==="
    ls -lh "$BACKUP_DIR"/sessions-*.db 2>/dev/null || echo "（无备份）"
    echo "=== 迁移前自动备份（data/backups）==="
    ls -lh "$APP_DIR"/data/backups/*.bak 2>/dev/null || echo "（无自动备份）"
    ;;

  db)
    require_backup
    echo "=== 数据库回滚（仅 DB，代码不变）==="
    echo "⚠️ 注意：代码仍是 new-version2 时，服务启动会再次迁移使此回滚失效。建议改用 all。"
    TS="$(date +%F_%H%M%S)"
    cp -a "$DB" "$BACKUP_DIR/sessions-pre-rollback-${TS}.db"
    echo "已保存当前现场 -> $BACKUP_DIR/sessions-pre-rollback-${TS}.db"
    stop_service
    cp -a "$ARG" "$DB"
    rm -f "$DB-wal" "$DB-shm"
    echo "已还原: $ARG"
    start_service
    echo "请用 verify.sh 校验。"
    ;;

  code)
    require_local_ref "$ARG"
    echo "=== 代码回滚到 $ARG ==="
    stop_service
    git -C "$APP_DIR" checkout "$ARG"
    start_service
    echo "当前分支/提交: $(git -C "$APP_DIR" log --oneline -1)"
    echo "⚠️ 若数据库已是 new-version2 新结构，需同时还原旧 DB（用 all 或 db 命令），否则旧代码可能报错。"
    ;;

  all)
    require_backup
    # 默认回滚到 main（部署前生产机所在分支）
    local_ref="${ARG2:-main}"
    require_local_ref "$local_ref"
    echo "=== 一键回滚：数据库 + 代码（$local_ref）==="
    TS="$(date +%F_%H%M%S)"
    cp -a "$DB" "$BACKUP_DIR/sessions-pre-rollback-${TS}.db"
    echo "已保存当前现场 -> $BACKUP_DIR/sessions-pre-rollback-${TS}.db"
    stop_service
    # 1) 还原数据库（旧结构备份）
    cp -a "$ARG" "$DB"
    rm -f "$DB-wal" "$DB-shm"
    echo "已还原数据库: $ARG"
    # 2) 回退代码
    git -C "$APP_DIR" checkout "$local_ref"
    echo "已回退代码: $(git -C "$APP_DIR" log --oneline -1)"
    start_service
    echo "请用 verify.sh 校验。"
    ;;

  mode)
    if [ -z "$ARG" ]; then echo "❌ 缺少 mode"; exit 1; fi
    if [ "$ARG" != "direct" ] && [ "$ARG" != "queue_remote" ]; then
      echo "❌ mode 必须是 direct/queue_remote（queue_local 已废除）"; exit 1
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
