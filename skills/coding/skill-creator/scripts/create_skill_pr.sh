#!/usr/bin/env bash
# ============================================================================
# create_skill_pr.sh — Coding Agent 创建 GitCode PR 的脚本
#
# 职责：git commit + push → 调 GitCode API 创建 PR → 设置审查人 → 输出 PR URL
# 复用 worktree 已有分支 feat/issue-{N}，不另建分支。
#
# 用法：
#   create_skill_pr.sh <skill_name>
#
# 环境变量（已由系统注入，不用查 env）：
#   BOT_GITCODE_TOKEN  仓库 bot token（必填，所有 agent 已注入）
#   REPO_FULL_NAME     仓库全名 owner/repo（必填）
#   ISSUE_NUMBER       Issue 编号（必填）
#   BOT_USERNAME       审查人用户名（可选，未注入时兜底 owner）
#
# 退出码：0=成功（创建或复用 PR）；1=参数/环境错误；2=API 调用失败
# ============================================================================
set -uo pipefail

SKILL_NAME="${1:-}"
if [[ -z "$SKILL_NAME" ]]; then
  echo "[create_skill_pr] ERROR: skill_name 必填"
  exit 1
fi

TOKEN="${BOT_GITCODE_TOKEN:-}"
REPO_FULL="${REPO_FULL_NAME:-}"
ISSUE_NUM="${ISSUE_NUMBER:-}"

if [[ -z "$TOKEN" ]]; then
  echo "[create_skill_pr] ERROR: BOT_GITCODE_TOKEN 未设置"
  exit 1
fi
if [[ -z "$REPO_FULL" ]]; then
  echo "[create_skill_pr] ERROR: REPO_FULL_NAME 未设置"
  exit 1
fi
if [[ -z "$ISSUE_NUM" ]]; then
  echo "[create_skill_pr] ERROR: ISSUE_NUMBER 未设置"
  exit 1
fi

# 解析 owner/repo
OWNER="${REPO_FULL%%/*}"
REPO="${REPO_FULL#*/}"
API_BASE="https://api.gitcode.com/api/v5"
BRANCH="feat/issue-${ISSUE_NUM}"

log() { echo "[create_skill_pr] $*"; }

# ============================================================================
# 0. 获取仓库默认分支（不硬编码 main——目标仓库默认分支可能是 master）
# ============================================================================
log "获取仓库默认分支: GET /repos/${OWNER}/${REPO}"
REPO_RESP=$(curl -s --max-time 30 \
  -H "PRIVATE-TOKEN: ${TOKEN}" \
  "${API_BASE}/repos/${OWNER}/${REPO}" 2>/dev/null)

DEFAULT_BRANCH=$(echo "$REPO_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('default_branch',''))" 2>/dev/null || true)
if [[ -z "$DEFAULT_BRANCH" ]]; then
  log "WARNING: 无法获取默认分支，回退 main"
  BASE_BRANCH="main"
else
  BASE_BRANCH="$DEFAULT_BRANCH"
fi
log "默认分支: ${BASE_BRANCH}"

# ============================================================================
# 1. git commit + push（在当前 worktree 执行，分支已是 feat/issue-{N}）
# ============================================================================
log "git add skills/${SKILL_NAME}/"
git add "skills/${SKILL_NAME}/" 2>/dev/null || {
  log "WARNING: git add skills/${SKILL_NAME}/ 没有变更（可能已提交）"
}

# 检查是否有待提交的变更
if git diff --cached --quiet 2>/dev/null; then
  log "无新变更待提交（可能已 commit 过），跳过 commit"
else
  log "git commit"
  git commit -m "feat(skill): 新增 ${SKILL_NAME}" || {
    log "ERROR: git commit 失败"
    exit 2
  }
fi

log "git push origin ${BRANCH}"
git push origin "$BRANCH" 2>&1 | sed 's/^/[create_skill_pr] /' || {
  log "ERROR: git push 失败"
  exit 2
}

# ============================================================================
# 2. 幂等查重：同分支已有 open PR → 复用
# ============================================================================
log "查重: GET /repos/${OWNER}/${REPO}/pulls?state=open"
PRS_RESP=$(curl -s --max-time 30 \
  -H "PRIVATE-TOKEN: ${TOKEN}" \
  "${API_BASE}/repos/${OWNER}/${REPO}/pulls?state=open&per_page=100" 2>/dev/null)

PR_NUMBER=$(echo "$PRS_RESP" | python3 -c "
import json, sys
try:
    for pr in json.load(sys.stdin):
        head = pr.get('head') or {}
        ref = head.get('ref', '') if isinstance(head, dict) else ''
        if ref == '${BRANCH}':
            print(pr.get('number', ''))
            break
except Exception:
    pass
" 2>/dev/null)

if [[ -n "$PR_NUMBER" ]]; then
  PR_URL="https://gitcode.com/${OWNER}/${REPO}/merge_requests/${PR_NUMBER}"
  log "复用已有 open PR #${PR_NUMBER} (${PR_URL})"
  echo "PR_URL=${PR_URL}"
  echo "PR_NUMBER=${PR_NUMBER}"
  exit 0
fi

# ============================================================================
# 3. 创建 PR
# ============================================================================
PR_BODY=$(python3 -c "
import json
body = '## 新增 Skill: ${SKILL_NAME}\n\n由 skill-creator 自动创建\n\nFixes #${ISSUE_NUM}'
print(json.dumps({
    'head': '${BRANCH}',
    'base': '${BASE_BRANCH}',
    'title': 'feat(skill): 新增 ${SKILL_NAME}',
    'body': body
}))
" 2>/dev/null)

log "创建 PR: POST /repos/${OWNER}/${REPO}/pulls  base=${BASE_BRANCH} head=${BRANCH}"
CREATE_RESP=$(curl -s -w "\n%{http_code}" --max-time 30 \
  -X POST \
  -H "PRIVATE-TOKEN: ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$PR_BODY" \
  "${API_BASE}/repos/${OWNER}/${REPO}/pulls" 2>/dev/null)

HTTP_CODE=$(echo "$CREATE_RESP" | tail -n 1)
BODY_ONLY=$(echo "$CREATE_RESP" | head -n -1)

if [[ ! "$HTTP_CODE" =~ ^2 ]]; then
  log "ERROR: 创建 PR 失败 HTTP ${HTTP_CODE}"
  echo "$BODY_ONLY" | head -c 500
  echo ""
  exit 2
fi

PR_NUMBER=$(echo "$BODY_ONLY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('number',''))" 2>/dev/null || true)
if [[ -z "$PR_NUMBER" ]]; then
  log "ERROR: 创建响应无 PR number"
  exit 2
fi

PR_URL="https://gitcode.com/${OWNER}/${REPO}/merge_requests/${PR_NUMBER}"
log "PR 已创建 #${PR_NUMBER} (${PR_URL})"

# ============================================================================
# 4. base 校验（防 base 写错）
# ============================================================================
log "base 校验: GET /repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}"
DETAIL=$(curl -s --max-time 30 \
  -H "PRIVATE-TOKEN: ${TOKEN}" \
  "${API_BASE}/repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}" 2>/dev/null)

ACTUAL_BASE=$(echo "$DETAIL" | python3 -c "import json,sys; d=json.load(sys.stdin); print((d.get('base') or {}).get('ref',''))" 2>/dev/null || true)
if [[ "$ACTUAL_BASE" != "$BASE_BRANCH" ]]; then
  log "ERROR: PR base 校验不过: 期望 ${BASE_BRANCH}, 实际 ${ACTUAL_BASE:-未知}"
  exit 2
fi
log "base 校验通过: base=${ACTUAL_BASE}"

# ============================================================================
# 5. 设置审查人（best-effort，失败不阻塞）
# ============================================================================
REVIEWER="${BOT_USERNAME:-}"
if [[ -z "$REVIEWER" ]]; then
  REVIEWER="$OWNER"
  log "BOT_USERNAME 未注入，兜底审查人: ${REVIEWER}"
fi

log "设置审查人: ${REVIEWER}"
ASSIGN_RESP=$(curl -s -m 10 \
  -X POST \
  -H "PRIVATE-TOKEN: ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"assignees\": \"${REVIEWER}\"}" \
  "${API_BASE}/repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/assignees" 2>/dev/null)
log "审查人设置响应: ${ASSIGN_RESP:-empty}"

# ============================================================================
# 6. 输出
# ============================================================================
echo ""
echo "PR_URL=${PR_URL}"
echo "PR_NUMBER=${PR_NUMBER}"
log "完成: ${PR_URL}  (base=${BASE_BRANCH}, 分支=${BRANCH})"
exit 0
