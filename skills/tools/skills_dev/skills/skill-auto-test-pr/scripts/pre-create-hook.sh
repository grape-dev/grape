#!/usr/bin/env bash
# Pre-create hook for huawei-cloud-skill-creator.
# Called as soon as the agent receives a dev task, sets t_skills_dev_flow.node_dev_status
# to 'processing' so the platform shows "开发中" immediately instead of stuck at pending.
#
# 幂等逻辑（确保总能置为 processing）:
#   1. 按 multica_issue_id 匹配 → UPDATE processing
#   2. 无则从 t_skills_dev_requirement 反查 gitcode_issue_id 匹配 → UPDATE + 回填 multica_issue_id
#   3. 仍无 → INSERT 新 flow 记录（带 multica_issue_id 防重）
set -uo pipefail

MULTICA_ISSUE_ID="${1:-}"
DB_HOST="${DB_HOST:-192.168.1.18}"
DB_USER="${DB_USER:-root}"
DB_PASS="${DB_PASS:-}"
DB_NAME="${DB_NAME:-rds-hd-skillsopr-db}"

if [[ -z "$MULTICA_ISSUE_ID" ]]; then
  echo "[pre-create-hook] ERROR: multica_issue_id is required as first argument"
  exit 1
fi

echo "[pre-create-hook] Setting t_skills_dev_flow dev node to processing for issue ${MULTICA_ISSUE_ID}..."

python3 << PYEOF
import pymysql
from datetime import datetime

MULTICA_ISSUE_ID = "${MULTICA_ISSUE_ID}"
now = datetime.now()

try:
    conn = pymysql.connect(host='${DB_HOST}', user='${DB_USER}', password='${DB_PASS}', database='${DB_NAME}', charset='utf8mb4')
    cur = conn.cursor()

    # --- Tier 1: match by multica_issue_id ---
    cur.execute(
        "UPDATE \`t_skills_dev_flow\` SET node_dev_status = %s, node_dev_time = %s WHERE multica_issue_id = %s",
        ('processing', now, MULTICA_ISSUE_ID)
    )
    affected = cur.rowcount

    if affected > 0:
        print(f"[pre-create-hook] updated by multica_issue_id: node_dev_status=processing")
    else:
        # --- Tier 2: resolve gitcode_issue_id from requirement, match flow by it ---
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
                print(f"[pre-create-hook] updated by gitcode_issue_id={git_issue_id}: node_dev_status=processing (multica_issue_id backfilled)")

    if affected > 0:
        conn.commit()
    else:
        # --- Tier 3: insert a new flow record (idempotent on multica_issue_id) ---
        # find the matching requirement row to copy source-node defaults
        cur.execute(
            "SELECT id, gitcode_issue_id, gitcode_issue_number FROM \`t_skills_dev_requirement\` WHERE multica_issue_id = %s LIMIT 1",
            (MULTICA_ISSUE_ID,)
        )
        req = cur.fetchone()
        req_id = req[0] if req else None
        git_issue_id = req[1] if req else None
        git_issue_num = req[2] if req else None

        # guard: only insert if no record exists for this multica_issue_id (idempotent)
        cur.execute("SELECT id FROM \`t_skills_dev_flow\` WHERE multica_issue_id = %s LIMIT 1", (MULTICA_ISSUE_ID,))
        if cur.fetchone() is None:
            # requirement_id is NOT NULL + UNIQUE; use 0 as placeholder when no requirement row exists
            insert_req_id = req_id if req_id is not None else 0
            cur.execute(
                """INSERT INTO \`t_skills_dev_flow\`
                   (requirement_id, gitcode_issue_id, multica_issue_id,
                    node_source_status, node_preprocess_status, node_dev_status,
                    node_dev_time, created_at, updated_at)
                   VALUES (%s, %s, %s, 'done', 'done', 'processing', %s, %s, %s)""",
                (insert_req_id, str(git_issue_id) if git_issue_id else None, MULTICA_ISSUE_ID,
                 now, now, now)
            )
            conn.commit()
            print(f"[pre-create-hook] INSERTED new flow record (multica_issue_id={MULTICA_ISSUE_ID}) with node_dev_status=processing")
        else:
            print(f"[pre-create-hook] WARNING: flow record exists but not matched by tiers; multica_issue_id={MULTICA_ISSUE_ID}")

    # 同步更新 t_skills_dev_requirement.status = developing（开发中）
    try:
        cur.execute(
            "UPDATE \`t_skills_dev_requirement\` SET status = %s WHERE multica_issue_id = %s",
            ('developing', MULTICA_ISSUE_ID)
        )
        req_affected = cur.rowcount
        conn.commit()
        if req_affected > 0:
            print(f"[pre-create-hook] t_skills_dev_requirement.status set to developing")
        else:
            print(f"[pre-create-hook] WARNING: No t_skills_dev_requirement record found to set status=developing")
    except Exception as req_e:
        print(f"[pre-create-hook] WARNING: t_skills_dev_requirement status update failed: {req_e}")

    conn.close()
except Exception as e:
    print(f"[pre-create-hook] WARNING: t_skills_dev_flow update failed: {e}")

PYEOF

echo "[pre-create-hook] Done."
exit 0
