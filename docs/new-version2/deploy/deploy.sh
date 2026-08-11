#!/usr/bin/env bash
# =============================================================================
# new-version2 一键部署脚本（服务端执行：备份 → 拉代码 → 重启 → 状态检查）
#
# 用法：
#   bash deploy.sh [--mode direct|queue_local|queue_remote] [--no-restart]
#
# 注意：
#   - 在 /opt/grape 目录下执行
#   - 先合并发布分支到 main 再执行（或直接切到发布分支）
#   - 本脚本不含任何密码；SSH 交互由你手动完成
# =============================================================================
set -euo pipefail

APP_DIR="/opt/grape"
MODE="direct"          # 默认直连（行为与旧版一致），发布后手动灰度切换
RESTART="yes"

for arg in "$@"; do
  case "$arg" in
    --mode=*) MODE="${arg#*=}" ;;
    --no-restart) RESTART="no" ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done

cd "$APP_DIR"
TS="$(date +%F_%H%M%S)"
BACKUP_DIR="/root/backups"

echo "=== [1/6] 前置检查 ==="
if [ -n "$(git status --porcelain)" ]; then
  echo "⚠️  工作区有未提交改动（configs/*.json 等 gitignore 文件不影响）："
  git status --short
  echo "继续前请确认这些改动不是误操作。"
fi
echo "当前分支: $(git branch --show-current)"
echo "当前提交: $(git log --oneline -1)"

echo "=== [2/6] 备份数据库 ==="
mkdir -p "$BACKUP_DIR"
if [ -f "$APP_DIR/data/sessions.db" ]; then
  cp -a "$APP_DIR/data/sessions.db" "$BACKUP_DIR/sessions-${TS}.db"
  echo "已备份 -> $BACKUP_DIR/sessions-${TS}.db"
  sqlite3 "$APP_DIR/data/sessions.db" \
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;" \
    > "$BACKUP_DIR/tables-before-${TS}.txt" || true
else
  echo "未找到 data/sessions.db（首次部署？）"
fi

echo "=== [3/6] 拉取代码 ==="
git pull origin "$(git branch --show-current)"

echo "=== [4/6] 设置执行模式（写入 systemd 环境） ==="
# 用 drop-in 覆盖默认 direct；如已有则更新
DROP="/etc/systemd/system/grape.service.d"
mkdir -p "$DROP"
cat > "$DROP/execmode.conf" <<EOF
[Service]
Environment=DEVOPS_EXECUTION_MODE=$MODE
EOF
systemctl daemon-reload

if [ "$RESTART" = "yes" ]; then
  echo "=== [5/6] 重启 grape 服务 ==="
  systemctl restart grape
  sleep 3
else
  echo "=== [5/6] 跳过重启（--no-restart） ==="
fi

echo "=== [6/6] 状态检查 ==="
systemctl is-active grape || echo "⚠️ 服务未 active，请查 journalctl -u grape"
echo "--- 主 API 健康 ---"
curl -sf http://127.0.0.1:3000/health || echo "⚠️ 3000 不可达"
echo ""
echo "--- webhook 健康 ---"
curl -sf http://127.0.0.1:8080/health || echo "⚠️ 8080 不可达"
echo ""
echo "--- 迁移表检查 ---"
sqlite3 "$APP_DIR/data/sessions.db" \
  "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('agent_runtimes','agent_tasks','runtime_credentials') ORDER BY name;" 2>/dev/null || true

echo ""
echo "=== 部署完成（execution_mode=$MODE） ==="
echo "确认无回归后，按 runbook §五 灰度切换 queue_local / queue_remote。"
