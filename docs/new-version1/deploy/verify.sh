#!/usr/bin/env bash
# =============================================================================
# new-version2 部署后校验脚本（服务端执行）
# 用法：bash verify.sh
# 通过条件见 runbook §八 验收检查单
# =============================================================================
set -uo pipefail

APP_DIR="/opt/grape"
DB="$APP_DIR/data/sessions.db"
PASS=0
FAIL=0

ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "=== 1. 服务健康 ==="
if systemctl is-active --quiet grape; then ok "grape.service active"; else bad "grape.service 未 active"; fi
if curl -sf http://127.0.0.1:3000/health >/dev/null; then ok "主 API :3000 健康"; else bad "主 API :3000 不可达"; fi
if curl -sf http://127.0.0.1:8080/health >/dev/null; then ok "webhook :8080 健康"; else bad "webhook :8080 不可达"; fi

echo "=== 2. SQLite WAL ==="
MODE="$(sqlite3 "$DB" "PRAGMA journal_mode;" 2>/dev/null)"
if [ "$MODE" = "wal" ]; then ok "WAL 模式 ($MODE)"; else bad "WAL 未生效 ($MODE)"; fi

echo "=== 3. 表迁移 ==="
TABLES="$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table';" 2>/dev/null)"
for t in agent_runtimes agent_tasks runtime_credentials; do
  if echo "$TABLES" | grep -qx "$t"; then ok "表 $t 存在"; else bad "表 $t 缺失"; fi
done
if echo "$TABLES" | grep -q "backend_agents"; then bad "旧表 backend_agents 仍存在"; else ok "旧表 backend_agents 已移除"; fi

echo "=== 4. 数据保全 ==="
CNT="$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runtimes;" 2>/dev/null || echo "err")"
if [ "$CNT" != "err" ] && [ "$CNT" -ge 1 ]; then ok "agent_runtimes 数据保全 ($CNT 行)"; else bad "agent_runtimes 数据为空/异常 ($CNT)"; fi

echo "=== 5. 列改名 ==="
SESS_COLS="$(sqlite3 "$DB" "PRAGMA table_info(devops_agent_session);" 2>/dev/null | cut -d'|' -f2)"
if echo "$SESS_COLS" | grep -q "runtime_id"; then ok "devops_agent_session.runtime_id 存在"; else bad "runtime_id 缺失"; fi
if echo "$SESS_COLS" | grep -q "backend_id"; then bad "backend_id 仍存在"; else ok "backend_id 已移除"; fi
if echo "$SESS_COLS" | grep -q "runtime_session_id"; then ok "runtime_session_id 存在"; else bad "runtime_session_id 缺失"; fi

echo "=== 6. 自动备份 ==="
if ls "$APP_DIR/data/backups/"*.bak >/dev/null 2>&1; then
  ok "迁移前自动备份存在"
else
  echo "  ⚠️ 无 data/backups/*.bak（副本无旧结构时可能不触发，属正常）"
fi

echo "=== 7. 执行模式 ==="
CUR="$(sqlite3 "$DB" "SELECT value FROM meta WHERE key='execution_mode';" 2>/dev/null || true)"
echo "  当前 execution_mode (meta): ${CUR:-（未设置，默认 direct）}"

echo ""
echo "===== 结果: PASS=$PASS FAIL=$FAIL ====="
if [ "$FAIL" -gt 0 ]; then
  echo "存在失败项，请查 journalctl -u grape 与 runbook §九 FAQ。"
  exit 1
else
  echo "全部通过，可进入灰度切换（runbook §五）。"
fi
