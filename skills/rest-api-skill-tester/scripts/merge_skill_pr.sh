#!/usr/bin/env bash
# ============================================================================
# merge_skill_pr.sh — QA Agent 测试通过后调用的合并脚本
#
# 职责：执行 GitCode merge API 自动合并 PR
# 测试通过后统一自动合并，不再区分只读/变更类。
#
# 用法：
#   merge_skill_pr.sh <pr_number>
#
# 环境变量（已由系统注入）：
#   BOT_GITCODE_TOKEN  仓库 bot token（必填）
#   REPO_FULL_NAME     仓库全名 owner/repo（必填）
#
# 退出码：0=合并成功；1=参数/环境错误；2=merge API 调用失败
# ============================================================================
set -uo pipefail

PR_NUMBER="${1:-}"

if [[ -z "$PR_NUMBER" ]]; then
  echo "[merge_skill_pr] ERROR: pr_number 必填"
  exit 1
fi

TOKEN="${BOT_GITCODE_TOKEN:-}"
REPO_FULL="${REPO_FULL_NAME:-}"

if [[ -z "$TOKEN" ]]; then
  echo "[merge_skill_pr] ERROR: BOT_GITCODE_TOKEN 未设置"
  exit 1
fi
if [[ -z "$REPO_FULL" ]]; then
  echo "[merge_skill_pr] ERROR: REPO_FULL_NAME 未设置"
  exit 1
fi

OWNER="${REPO_FULL%%/*}"
REPO="${REPO_FULL#*/}"
API_BASE="https://api.gitcode.com/api/v5"

log() { echo "[merge_skill_pr] $*"; }

# ============================================================================
# 调 GitCode merge API 自动合并
# ============================================================================
log "合并 PR #${PR_NUMBER}: PUT /repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/merge"
MERGE_RESP=$(curl -s -w "\n%{http_code}" --max-time 30 \
  -X PUT \
  -H "PRIVATE-TOKEN: ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"merge_method": "merge", "prune_branch": true}' \
  "${API_BASE}/repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/merge" 2>/dev/null)

HTTP_CODE=$(echo "$MERGE_RESP" | tail -n 1)
BODY_ONLY=$(echo "$MERGE_RESP" | head -n -1)

if [[ ! "$HTTP_CODE" =~ ^2 ]]; then
  log "ERROR: merge API 失败 HTTP ${HTTP_CODE}"
  echo "$BODY_ONLY" | head -c 500
  echo ""
  echo "MERGED=0"
  echo "MESSAGE=merge API 失败 HTTP ${HTTP_CODE}，请手动合并"
  exit 2
fi

# 检查 merged 字段
MERGED=$(echo "$BODY_ONLY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('merged', ''))" 2>/dev/null || true)
if [[ "$MERGED" == "True" || "$MERGED" == "true" || "$MERGED" == "1" ]]; then
  log "PR #${PR_NUMBER} 合并成功"
  echo "MERGED=1"
  exit 0
else
  # 有些 GitCode 版本不返回 merged 字段，HTTP 2xx 即视为成功
  log "PR #${PR_NUMBER} 合并成功（HTTP ${HTTP_CODE}）"
  echo "MERGED=1"
  exit 0
fi
