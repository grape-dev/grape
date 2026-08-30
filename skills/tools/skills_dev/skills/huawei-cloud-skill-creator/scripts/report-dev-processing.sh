#!/usr/bin/env bash
# Report dev-node status to t_skills_dev_flow before executing the skill-creation task.
# Sets node_dev_status='processing' for the given multica issue id.
set -uo pipefail

MULTICA_ISSUE_ID="${1:-}"

# 加载工具链 .env（若存在）
SKILLS_BASE_DIR="${HOME}/.agents/skills"
ENV_FILE="${SKILLS_BASE_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

DB_HOST="${DB_HOST:-}"
DB_USER="${DB_USER:-}"
DB_PASS="${DB_PASS:-}"
DB_NAME="${DB_NAME:-}"

# 校验必填配置
missing_config=""
for var in DB_HOST DB_USER DB_PASS DB_NAME; do
  eval "val=\${$var:-}"
  if [ -z "$val" ]; then
    missing_config="$missing_config $var"
  fi
done
if [ -n "$missing_config" ]; then
  echo "[report-dev] ERROR: 缺少必填配置:$missing_config"
  echo "[report-dev] 请设置环境变量或在 ${SKILLS_BASE_DIR}/.env 中配置后重试"
  exit 1
fi

MULTICA_SERVER_URL="${MULTICA_SERVER_URL:-https://multica.clouddeveloper.club}"
MULTICA_WORKSPACE_SLUG="${MULTICA_WORKSPACE_SLUG:-skill-test}"
MULTICA_BASE_URL="${MULTICA_SERVER_URL}/${MULTICA_WORKSPACE_SLUG}/issues"

if [[ -z "$MULTICA_ISSUE_ID" ]]; then
  echo "[report-dev-processing] ERROR: multica_issue_id is required as first argument"
  exit 1
fi

NODE_DEV_URL="${MULTICA_BASE_URL}/${MULTICA_ISSUE_ID}"

echo "[report-dev-processing] Setting t_skills_dev_flow node_dev_status=processing for issue ${MULTICA_ISSUE_ID}..."

python3 << PYEOF
import pymysql
from datetime import datetime

try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute(
        "UPDATE \`t_skills_dev_flow\` SET node_dev_status = %s, node_dev_url = %s, node_dev_time = %s WHERE multica_issue_id = %s",
        ('processing', '${NODE_DEV_URL}', datetime.now(), '${MULTICA_ISSUE_ID}')
    )
    affected = cur.rowcount
    conn.commit()
    if affected > 0:
        print(f"[report-dev-processing] t_skills_dev_flow updated: node_dev_status=processing, node_dev_url=${NODE_DEV_URL}, node_dev_time=now()")
    else:
        print(f"[report-dev-processing] WARNING: No t_skills_dev_flow record found with multica_issue_id=${MULTICA_ISSUE_ID}")
    conn.close()
except Exception as e:
    print(f"[report-dev-processing] WARNING: t_skills_dev_flow update failed: {e}")
    exit(1)
PYEOF

echo "[report-dev-processing] Done."
exit 0
