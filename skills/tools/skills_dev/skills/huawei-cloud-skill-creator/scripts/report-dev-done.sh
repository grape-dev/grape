#!/usr/bin/env bash
# Report dev-node completion to t_skills_dev_flow after a skill-creation task succeeds.
# Sets node_dev_status='done' for the given multica issue id.
# MANDATORY: the agent MUST run this after completing a skill (Phase 6/7), directly
# updating the DB instead of relying solely on the async webhook pipeline.
set -uo pipefail

MULTICA_ISSUE_ID="${1:-}"
DB_HOST="${DB_HOST:-}"
DB_USER="${DB_USER:-}"
DB_PASS="${DB_PASS:-}"  # 请通过环境变量 DB_PASS 提供数据库密码
DB_NAME="${DB_NAME:-}"
MULTICA_BASE_URL="https://multica.clouddeveloper.club/skills-prod/issues"

if [[ -z "$MULTICA_ISSUE_ID" ]]; then
  echo "[report-dev-done] ERROR: multica_issue_id is required as first argument"
  exit 1
fi

NODE_DEV_URL="${MULTICA_BASE_URL}/${MULTICA_ISSUE_ID}"
PR_ID="${2:-}"
REPORT_FILE="${3:-}"  # 可选: 结论五件套汇报正文(markdown 文件); 提供后自动触发评论携带完整汇报(方案A, SKI-55: 脚本评论即完成汇报, agent 不再手动发第二条)

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
    # 若提供了 PR 编号（第二个参数），同步写入 requirement.gitcode_pr_id（开发完成后 PR 已创建的场景）
    PR_ID = "${2:-}"
    if PR_ID:
        try:
            cur.execute(
                "UPDATE \`t_skills_dev_requirement\` SET gitcode_pr_id = %s WHERE multica_issue_id = %s",
                (PR_ID, '${MULTICA_ISSUE_ID}')
            )
            pr_affected = cur.rowcount
            conn.commit()
            if pr_affected > 0:
                print(f"[report-dev-done] t_skills_dev_requirement.gitcode_pr_id set to {PR_ID}")
        except Exception as pr_e:
            print(f"[report-dev-done] WARNING: gitcode_pr_id update failed: {pr_e}")
    conn.close()
except Exception as e:
    print(f"[report-dev-done] WARNING: t_skills_dev_flow update failed: {e}")
    exit(1)
PYEOF

# 2026-08-12 B 方案: DB 更新成功后**自动**在 multica issue 评论 @skills_test
# 触发测试 —— 不依赖 agent 汇报(汇报输出超限时测试触发不再丢失, SKI-51 教训)。
# 幂等: 评论前检查该 issue 是否已有自动触发标记, 已有则跳过(脚本可能被重复调用)。
if [[ -n "${PR_ID}" ]]; then
  MULTICA_BIN="$(command -v multica 2>/dev/null || echo /usr/local/bin/multica)"
  MARKER="report-dev-done 自动触发"
  _already="$("${MULTICA_BIN}" issue comment list "${MULTICA_ISSUE_ID}" --output json 2>/dev/null | grep -cF "${MARKER}" || true)"
  if [[ "${_already:-0}" -eq 0 ]]; then
    _trigger_line="[@skills_test](mention://squad/${SQUAD_TEST:-7069cb53-2a2d-4033-94be-366c46e8e330}) 开发完成，请测试 [PR #${PR_ID}](https://gitcode.com/${GITCODE_OWNER:-hd-skills-pre}/${GITCODE_REPO:-skills-pre}/merge_requests/${PR_ID})（${MARKER}）"
    _comment_text="${_trigger_line}"
    # 方案A(SKI-55): 传入 REPORT_FILE(结论五件套) 时, 自动评论 = 完整完成汇报 + @skills_test
    # 触发 —— 汇报正文在前, 触发行在后。避免"脚本自动触发评论 + agent 手动完成汇报"双评论刷屏,
    # agent 无需再手动发第二条。幂等 MARKER 在触发行内, 检查逻辑不受影响。
    if [[ -n "${REPORT_FILE:-}" && -f "${REPORT_FILE:-}" ]]; then
      _comment_text="$(cat "${REPORT_FILE}")

${_trigger_line}"
    fi
    "${MULTICA_BIN}" issue comment add "${MULTICA_ISSUE_ID}" --content "${_comment_text}" >/dev/null 2>&1 \
      && echo "[report-dev-done] 已自动触发 @skills_test 测试 PR #${PR_ID}" \
      || echo "[report-dev-done] WARNING: 自动触发测试评论失败(限流/权限)"
  else
    echo "[report-dev-done] 已有自动触发标记, 跳过重复触发"
  fi
else
  echo "[report-dev-done] 未提供 PR_ID, 跳过自动触发测试"
fi

echo "[report-dev-done] Done."
exit 0
