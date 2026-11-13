#!/usr/bin/env bash
# 部署放行守卫：迁移 → 跨库视图修复 → 复核 → 才允许重启进程。
#
# 为什么需要这个顺序：迁移由进程 import 期执行（init_database），
# 若先在启动前修视图，看到的是**上一版 schema**，随本次发布新增的列/表仍会 1054/1146。
#
# 用法（fail-closed，任何非 0 都不得重启）：
#   DEVOPS_DB_HOST=... DEVOPS_DB_USER=... DEVOPS_DB_PASSWORD=... \
#   DEVOPS_DB_NAME=grape_mgmt_db GRAPE_DOMAIN_DATABASES=... \  # 或 DEVOPS_DB_NAME 足够
#     bash scripts/deploy-after-migrate.sh [--dry-run] [--skip-migrate]
#
# 可选环境变量：
#   GRAPE_PYTHON          指定解释器（需能 import pymysql / fastapi）
#   GRAPE_MIGRATE_ONLY=1  只跑迁移（调试用）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

DRY=""
SKIP_MIGRATE=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY="--dry-run" ;;
    --skip-migrate) SKIP_MIGRATE="1" ;;
  esac
done

PY="${GRAPE_PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in "./.venv/bin/python" "$HOME/grape/.venv/bin/python" "python3"; do
    if "$cand" -c "import pymysql" >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "[FATAL] 找不到能 import pymysql 的 Python（请设置 GRAPE_PYTHON=/path/to/python）" >&2
  exit 1
fi

if [ -z "${DEVOPS_DB_HOST:-}" ]; then
  echo "[FAIL] 数据库不可用（DEVOPS_DB_HOST 未设置）—— 未做任何修复，**不要重启四进程**" >&2
  exit 3
fi

KNOWN_DUPES="scripts/view_owner_known_duplicates.txt"
CREATE_MISSING="scripts/view_create_missing_allowlist.txt"
NARROWED="scripts/view_narrowed_allowlist.txt"

echo "=== [1/3] 执行数据库迁移（幂等；与本脚本后续修复的顺序不可颠倒）==="
if [ -n "$SKIP_MIGRATE" ] || [ -n "$DRY" ]; then
  echo "  跳过（--skip-migrate / --dry-run）"
else
  "$PY" - <<'PYCODE'
import sys
try:
    from src.api.db_setup import init_database
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] 迁移入口不可用: {exc}", file=sys.stderr)
    sys.exit(3)
init_database()
print("  迁移完成")
PYCODE
  rc=$?
  if [ "$rc" != "0" ]; then
    echo "[FAIL] 迁移失败（rc=$rc）—— **不要重启四进程**" >&2
    exit "$rc"
  fi
fi

echo
echo "=== [2/3] 修复跨库视图（缺列/多余列/类型漂移；缺失视图仅对清单内表补建）${DRY:+（dry-run）} ==="
ARGS=(--repair --strict --strict-owner)
[ -f "$KNOWN_DUPES" ] && ARGS+=(--known-duplicates "$KNOWN_DUPES")
[ -f "$CREATE_MISSING" ] && ARGS+=(--create-missing-tables "$CREATE_MISSING")
[ -f "$NARROWED" ] && ARGS+=(--narrowed-tables "$NARROWED")
"$PY" scripts/domain_view_freshness.py "${ARGS[@]}" ${DRY}
rc=$?

if [ "$DRY" = "--dry-run" ]; then
  echo "（dry-run 结束，未修改任何视图；rc=$rc）"
  exit "$rc"
fi

if [ "$rc" != "0" ]; then
  case "$rc" in
    3) echo "[FAIL] 数据库不可用 / 库族不确定 / 缺 CREATE VIEW 权限 —— **不要重启四进程**" >&2 ;;
    2) echo "[FAIL] 存在未登记的同名多份基表 —— 先定 owner（谁拥有谁写）再部署" >&2 ;;
    *) echo "[FAIL] 仍有视图漂移或孤儿视图（见上）—— **不要重启四进程**" >&2 ;;
  esac
  exit "$rc"
fi

echo
echo "=== [3/3] 视图就绪（exit 0），可以重启四进程 ==="
exit 0
