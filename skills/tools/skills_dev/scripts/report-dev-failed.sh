#!/usr/bin/env bash
# Report dev-node failure to t_skills_dev_flow after a skill-creation task fails
# because no CLI/SDK/API interface could be found for the requested feature points.
# Sets node_dev_status='failed' for the given multica issue id, and posts the
# rejection/failure reason as a comment on the linked GitCode issue (if any).
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
FAIL_REASON="${2:-}"  # optional human-readable failure/rejection reason
DB_HOST="${DB_HOST:-}"
DB_USER="${DB_USER:-}"
DB_PASS="${DB_PASS:-}"  # 请通过环境变量 DB_PASS 提供数据库密码
DB_NAME="${DB_NAME:-}"
MULTICA_SERVER_URL="${MULTICA_SERVER_URL:-https://multica.clouddeveloper.club}"
MULTICA_WORKSPACE_SLUG="${MULTICA_WORKSPACE_SLUG:-skill-test}"
MULTICA_BASE_URL="${MULTICA_SERVER_URL}/${MULTICA_WORKSPACE_SLUG}/issues"

if [[ -z "$MULTICA_ISSUE_ID" ]]; then
  echo "[report-dev-failed] ERROR: multica_issue_id is required as first argument"
  exit 1
fi

NODE_DEV_URL="${MULTICA_BASE_URL}/${MULTICA_ISSUE_ID}"

echo "[report-dev-failed] Setting t_skills_dev_flow node_dev_status=failed for issue ${MULTICA_ISSUE_ID}..."

python3 << PYEOF
import pymysql
from datetime import datetime

try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute(
        "UPDATE \`t_skills_dev_flow\` SET node_dev_status = %s, node_dev_url = %s, node_dev_time = %s WHERE multica_issue_id = %s",
        ('failed', '${NODE_DEV_URL}', datetime.now(), '${MULTICA_ISSUE_ID}')
    )
    affected = cur.rowcount
    conn.commit()
    if affected > 0:
        print(f"[report-dev-failed] t_skills_dev_flow updated: node_dev_status=failed, node_dev_url=${NODE_DEV_URL}, node_dev_time=now()")
    else:
        print(f"[report-dev-failed] WARNING: No t_skills_dev_flow record found with multica_issue_id=${MULTICA_ISSUE_ID}")
    # 同步更新 t_skills_dev_requirement.status = failed（失败）
    try:
        cur.execute(
            "UPDATE \`t_skills_dev_requirement\` SET status = %s WHERE multica_issue_id = %s",
            ('failed', '${MULTICA_ISSUE_ID}')
        )
        req_affected = cur.rowcount
        conn.commit()
        if req_affected > 0:
            print(f"[report-dev-failed] t_skills_dev_requirement.status set to failed")
        else:
            print(f"[report-dev-failed] WARNING: No t_skills_dev_requirement record found to set status=failed")
    except Exception as req_e:
        print(f"[report-dev-failed] WARNING: t_skills_dev_requirement status update failed: {req_e}")
    conn.close()
except Exception as e:
    print(f"[report-dev-failed] WARNING: t_skills_dev_flow update failed: {e}")
    exit(1)
PYEOF

# --- Post the failure/rejection reason as a comment on the linked GitCode issue ---
ISSUE_NUM=""
ISSUE_OWNER=""
ISSUE_REPO=""
if [[ -n "$MULTICA_ISSUE_ID" ]]; then
  read -r ISSUE_NUM ISSUE_OWNER ISSUE_REPO <<< "$(python3 << PYEOF
import pymysql, re
try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute("SELECT gitcode_issue_number, gitcode_issue_url FROM \`t_skills_dev_requirement\` WHERE multica_issue_id = %s LIMIT 1", ('${MULTICA_ISSUE_ID}',))
    row = cur.fetchone()
    conn.close()
    num = str(row[0]) if row and row[0] else ''
    url = (row[1] or '') if row else ''
    m = re.search(r'gitcode\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)(/|$)', url)
    if m and num:
        print(f"{num} {m.group(1)} {m.group(2)}")
    else:
        print("  ")
except Exception:
    print("  ")
PYEOF
  )"
  if [[ -n "$ISSUE_NUM" && -n "$ISSUE_OWNER" && -n "$ISSUE_REPO" ]]; then
    COMMENT_BODY="${FAIL_REASON:-任务执行失败：在 hcloud CLI / huaweicloudsdk SDK / API Explorer 中均未找到可用的接口，无法创建该 skill。}"
    COMMENT_DATA=$(python3 -c "import json,sys; print(json.dumps({'body': sys.argv[1]}, ensure_ascii=False))" "$COMMENT_BODY")
    CURL_RESP=$(curl -s -m 15 -X POST \
      "${GITCODE_API}/repos/${ISSUE_OWNER}/${ISSUE_REPO}/issues/${ISSUE_NUM}/comments" \
      -H "PRIVATE-TOKEN: ${GITCODE_TOKEN}" -H "Content-Type: application/json" -d "$COMMENT_DATA" 2>/dev/null)
    if echo "$CURL_RESP" | grep -q '"id"'; then
      echo "[report-dev-failed] Commented failure reason on GitCode issue #${ISSUE_NUM}"
    else
      echo "[report-dev-failed] WARNING: failed to comment GitCode issue #${ISSUE_NUM}: ${CURL_RESP:0:200}"
    fi
  else
    echo "[report-dev-failed] No GitCode issue linked; skipping issue comment."
  fi
fi

echo "[report-dev-failed] Done."
exit 0
