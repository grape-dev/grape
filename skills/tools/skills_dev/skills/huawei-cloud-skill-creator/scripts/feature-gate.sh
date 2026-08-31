#!/usr/bin/env bash
# feature-gate.sh — Accept any dispatched dev task: as long as a GitCode issue is linked
# in the DB, the task proceeds to development (no feature/bug label gate).
# Also reports node_dev_status=processing (开发中) as soon as the task is received.
#
# Usage:
#   bash feature-gate.sh <multica_issue_id>
#
# Behavior:
#   1. Sets t_skills_dev_flow.node_dev_status = processing (task received -> developing)
#   2. Looks up the GitCode issue (via DB gitcode_issue_url / gitcode_issue_number)
#   3. Dispatched tasks are always accepted — no label filtering.
#   4. Prints the resolved repo + gitcode issue info for the caller.
set -uo pipefail

MULTICA_ISSUE_ID="${1:-}"
DB_HOST="${DB_HOST:-}"  # 必填: 请在 ~/.agents/skills/.env 配置
DB_USER="${DB_USER:-root}"
DB_PASS="${DB_PASS:-}"  # 请通过环境变量 DB_PASS 提供数据库密码
DB_NAME="${DB_NAME:-rds-hd-skillsopr-db}"
GITCODE_API="${GITCODE_API:-https://api.gitcode.com/api/v5}"
GITCODE_TOKEN="${GITCODE_TOKEN:-}"  # 请通过环境变量 GITCODE_TOKEN 提供

if [[ -z "$MULTICA_ISSUE_ID" ]]; then
  echo "[feature-gate] ERROR: multica_issue_id is required as first argument"
  exit 1
fi

echo "[feature-gate] Gating task for multica issue ${MULTICA_ISSUE_ID}..."

# --- Resolve GitCode issue info from DB ---
read -r GITCODE_ISSUE_NUM GITCODE_ISSUE_URL GITCODE_OWNER GITCODE_REPO <<< "$(python3 << PYEOF
import pymysql, re, sys
try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()
    cur.execute("SELECT gitcode_issue_number, gitcode_issue_url FROM \`t_skills_dev_requirement\` WHERE multica_issue_id = %s LIMIT 1", ('${MULTICA_ISSUE_ID}',))
    row = cur.fetchone()
    conn.close()
    num = url = owner = repo = ''
    if row:
        num = row[0] or ''
        url = row[1] or ''
        m = re.search(r'gitcode\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)(/|$)', url)
        if m:
            owner, repo = m.group(1).split('/', 1)
    print(f"{num} {url} {owner} {repo}")
except Exception:
    print("  ")
PYEOF
)"

echo "[feature-gate] GitCode issue: #${GITCODE_ISSUE_NUM:-?} (${GITCODE_OWNER}/${GITCODE_REPO})"

# --- If we cannot resolve a GitCode issue, do not gate (may be a direct multica task) ---
if [[ -z "${GITCODE_ISSUE_NUM:-}" || -z "${GITCODE_OWNER:-}" || -z "${GITCODE_REPO:-}" ]]; then
  echo "[feature-gate] No GitCode issue linked in DB; skipping feature gate."
  exit 0
fi

# 分发即开发：不再校验 GitCode issue 的 feature/bug 标签，只要关联到分发任务即可开始工作。
echo "[feature-gate] PASS: issue #${GITCODE_ISSUE_NUM} accepted for development (dispatch-gated)."

# ====================================================================
# 门禁通过后自动上报 processing（开发中）——不依赖 agent 自觉执行 pre-create-hook。
# 一次调用同时完成：flow.dev=processing + requirement.status=developing。
# 幂等：flow 记录不存在则 INSERT，已存在则 UPDATE；重复调用不重复插入。
# ====================================================================
echo "[feature-gate] Marking t_skills_dev_flow.node_dev_status=processing & requirement.status=developing..."
python3 << PYEOF
import pymysql
from datetime import datetime

MULTICA_ISSUE_ID = "${MULTICA_ISSUE_ID}"
now = datetime.now()

try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()

    # --- update flow.dev = processing (by multica_issue_id) ---
    cur.execute(
        "UPDATE \`t_skills_dev_flow\` SET node_dev_status = %s, node_dev_time = %s WHERE multica_issue_id = %s",
        ('processing', now, MULTICA_ISSUE_ID)
    )
    affected = cur.rowcount

    if affected == 0:
        # fallback: match by gitcode_issue_id via requirement, then backfill multica_issue_id
        cur.execute(
            "SELECT gitcode_issue_id FROM \`t_skills_dev_requirement\` WHERE multica_issue_id = %s LIMIT 1",
            (MULTICA_ISSUE_ID,)
        )
        row = cur.fetchone()
        git_issue_id = row[0] if row and row[0] else None
        if git_issue_id:
            cur.execute(
                "UPDATE \`t_skills_dev_flow\` SET node_dev_status = %s, node_dev_time = %s, multica_issue_id = %s WHERE gitcode_issue_id = %s",
                ('processing', now, MULTICA_ISSUE_ID, str(git_issue_id))
            )
            affected = cur.rowcount

    if affected > 0:
        conn.commit()
        print(f"[feature-gate] t_skills_dev_flow.node_dev_status=processing")
    else:
        # insert a new flow record if none exists
        cur.execute("SELECT id FROM \`t_skills_dev_flow\` WHERE multica_issue_id = %s LIMIT 1", (MULTICA_ISSUE_ID,))
        if cur.fetchone() is None:
            cur.execute(
                "SELECT id, gitcode_issue_id FROM \`t_skills_dev_requirement\` WHERE multica_issue_id = %s LIMIT 1",
                (MULTICA_ISSUE_ID,)
            )
            req = cur.fetchone()
            req_id = req[0] if req else 0
            git_issue_id = req[1] if req else None
            cur.execute(
                """INSERT INTO \`t_skills_dev_flow\`
                   (requirement_id, gitcode_issue_id, multica_issue_id,
                    node_source_status, node_preprocess_status, node_dev_status,
                    node_dev_time, created_at, updated_at)
                   VALUES (%s, %s, %s, 'done', 'done', 'processing', %s, %s, %s)""",
                (req_id, str(git_issue_id) if git_issue_id else None, MULTICA_ISSUE_ID,
                 now, now, now)
            )
            conn.commit()
            print(f"[feature-gate] INSERTED flow record with node_dev_status=processing")
        else:
            print(f"[feature-gate] WARNING: flow record exists but not matched; multica_issue_id=${MULTICA_ISSUE_ID}")

    # --- sync requirement.status = developing ---
    try:
        cur.execute(
            "UPDATE \`t_skills_dev_requirement\` SET status = %s WHERE multica_issue_id = %s",
            ('developing', MULTICA_ISSUE_ID)
        )
        req_affected = cur.rowcount
        conn.commit()
        if req_affected > 0:
            print(f"[feature-gate] t_skills_dev_requirement.status set to developing")
        else:
            print(f"[feature-gate] WARNING: No t_skills_dev_requirement record found to set status=developing")
    except Exception as req_e:
        print(f"[feature-gate] WARNING: t_skills_dev_requirement status update failed: {req_e}")

    conn.close()
except Exception as e:
    print(f"[feature-gate] WARNING: processing status update failed: {e}")
PYEOF

echo "GITCODE_OWNER=${GITCODE_OWNER}"
echo "GITCODE_REPO=${GITCODE_REPO}"
echo "GITCODE_ISSUE_NUMBER=${GITCODE_ISSUE_NUM}"
exit 0
