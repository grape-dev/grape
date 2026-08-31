#!/usr/bin/env bash
# Report dev-node completion to t_skills_dev_flow after a skill-creation task succeeds.
# Sets node_dev_status='done' for the given multica issue id.
# MANDATORY: the agent MUST run this after completing a skill (Phase 6/7), directly
# updating the DB instead of relying solely on the async webhook pipeline.
set -uo pipefail

# 加载工具链 .env(若存在); 空值回填默认值(2026-08-04 SKI-133/136 事故修复:
# multica daemon 拉起的 agent 不 source ~/.bashrc, 且 .env 里 export VAR=""
# 会覆盖脚本默认值导致 MySQL 连不上 / token 为空)。
_ENV_FILE="${SKILLS_BASE_DIR:-${HOME}/.agents/skills}/.env"
if [ -f "$_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$_ENV_FILE"; set +a
fi
[ -z "${DB_HOST:-}" ] && { echo "[ERROR] DB_HOST 未设置, 请在 ~/.agents/skills/.env 配置" >&2; exit 1; }
[ -z "${DB_USER:-}" ] && DB_USER="root"
[ -z "${DB_PASS:-}" ] && { echo "[ERROR] DB_PASS 未设置, 请在 ~/.agents/skills/.env 配置" >&2; exit 1; }
[ -z "${DB_NAME:-}" ] && DB_NAME="rds-hd-skillsopr-db"
[ -z "${GITCODE_TOKEN:-}" ] && { echo "[ERROR] GITCODE_TOKEN 未设置, 请在 ~/.agents/skills/.env 配置" >&2; exit 1; }

MULTICA_ISSUE_ID="${1:-}"
DB_HOST="${DB_HOST:-}"
DB_USER="${DB_USER:-}"
DB_PASS="${DB_PASS:-}"  # 请通过环境变量 DB_PASS 提供数据库密码
DB_NAME="${DB_NAME:-}"
MULTICA_SERVER_URL="${MULTICA_SERVER_URL:-https://multica.clouddeveloper.club}"
MULTICA_WORKSPACE_SLUG="${MULTICA_WORKSPACE_SLUG:-skill-test}"
MULTICA_BASE_URL="${MULTICA_SERVER_URL}/${MULTICA_WORKSPACE_SLUG}/issues"

if [[ -z "$MULTICA_ISSUE_ID" ]]; then
  echo "[report-dev-done] ERROR: multica_issue_id is required as first argument"
  exit 1
fi

NODE_DEV_URL="${MULTICA_BASE_URL}/${MULTICA_ISSUE_ID}"

echo "[report-dev-done] Setting t_skills_dev_flow node_dev_status=done for issue ${MULTICA_ISSUE_ID}..."

python3 << PYEOF
import pymysql
from datetime import datetime

try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute(
        "UPDATE \`t_skills_dev_flow\` SET node_dev_status = %s, node_dev_url = %s, node_dev_time = %s WHERE multica_issue_id = %s",
        ('done', '${NODE_DEV_URL}', datetime.now(), '${MULTICA_ISSUE_ID}')
    )
    affected = cur.rowcount
    conn.commit()
    if affected > 0:
        print(f"[report-dev-done] t_skills_dev_flow updated: node_dev_status=done, node_dev_url=${NODE_DEV_URL}, node_dev_time=now()")
    else:
        print(f"[report-dev-done] WARNING: No t_skills_dev_flow record found with multica_issue_id=${MULTICA_ISSUE_ID}")
    # 同步更新 t_skills_dev_requirement.status = developed（已开发完成）
    try:
        cur.execute(
            "UPDATE \`t_skills_dev_requirement\` SET status = %s WHERE multica_issue_id = %s",
            ('developed', '${MULTICA_ISSUE_ID}')
        )
        req_affected = cur.rowcount
        conn.commit()
        if req_affected > 0:
            print(f"[report-dev-done] t_skills_dev_requirement.status set to developed")
        else:
            print(f"[report-dev-done] WARNING: No t_skills_dev_requirement record found to set status=developed")
    except Exception as req_e:
        print(f"[report-dev-done] WARNING: t_skills_dev_requirement status update failed: {req_e}")
    conn.close()
except Exception as e:
    print(f"[report-dev-done] WARNING: t_skills_dev_flow update failed: {e}")
    exit(1)
PYEOF

echo "[report-dev-done] Done."
exit 0
