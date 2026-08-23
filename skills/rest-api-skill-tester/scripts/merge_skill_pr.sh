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
# 输出：MERGED=1/0 + BRANCH_DELETED=1/0 + MESSAGE
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
  echo "BRANCH_DELETED=0"
  echo "MESSAGE=merge API 失败 HTTP ${HTTP_CODE}，请手动合并"
  exit 2
fi

# 检查 merged 字段
MERGED=$(echo "$BODY_ONLY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('merged', ''))" 2>/dev/null || true)
if [[ "$MERGED" != "True" && "$MERGED" != "true" && "$MERGED" != "1" ]]; then
  # 有些 GitCode 版本不返回 merged 字段，HTTP 2xx 即视为成功
  log "PR #${PR_NUMBER} 合并成功（HTTP ${HTTP_CODE}，无 merged 字段）"
fi

log "PR #${PR_NUMBER} 合并成功"
echo "MERGED=1"

# ============================================================================
# 兜底删除源分支（prune_branch 不可靠，合并后查询+主动删除）
# ============================================================================
# 获取 PR 的源分支名
PR_DETAIL=$(curl -s --max-time 10 \
  -H "PRIVATE-TOKEN: ${TOKEN}" \
  "${API_BASE}/repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}" 2>/dev/null)
BRANCH_NAME=$(echo "$PR_DETAIL" | python3 -c "import json,sys; print(json.load(sys.stdin).get('head',{}).get('ref',''))" 2>/dev/null || true)

if [[ -z "$BRANCH_NAME" ]]; then
  log "WARNING: 无法获取源分支名，跳过兜底删除"
  echo "BRANCH_DELETED=0"
  echo "MESSAGE=合并成功，但无法获取源分支名，请手动确认分支是否删除"
  exit 0
fi

# 查询分支是否还存在
BRANCH_CHECK=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "PRIVATE-TOKEN: ${TOKEN}" \
  "${API_BASE}/repos/${OWNER}/${REPO}/branches/${BRANCH_NAME}" 2>/dev/null)

if [[ "$BRANCH_CHECK" == "404" ]]; then
  log "源分支 ${BRANCH_NAME} 已删除（prune_branch 生效）"
  echo "BRANCH_DELETED=1"
  echo "MESSAGE=合并成功，源分支已自动删除"
  exit 0
fi

if [[ "$BRANCH_CHECK" == "200" ]]; then
  log "源分支 ${BRANCH_NAME} 仍存在，执行兜底删除..."
  DELETE_RESP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
    -X DELETE \
    -H "PRIVATE-TOKEN: ${TOKEN}" \
    "${API_BASE}/repos/${OWNER}/${REPO}/branches/${BRANCH_NAME}" 2>/dev/null)
  if [[ "$DELETE_RESP" =~ ^2 ]]; then
    log "源分支 ${BRANCH_NAME} 兜底删除成功"
    echo "BRANCH_DELETED=1"
    echo "MESSAGE=合并成功，源分支已兜底删除"
  else
    log "WARNING: 源分支 ${BRANCH_NAME} 兜底删除失败 HTTP ${DELETE_RESP}"
    echo "BRANCH_DELETED=0"
    echo "MESSAGE=合并成功，但源分支 ${BRANCH_NAME} 删除失败 (HTTP ${DELETE_RESP})，请手动清理"
  fi
  exit 0
fi

log "WARNING: 查询分支 ${BRANCH_NAME} 状态异常 HTTP ${BRANCH_CHECK}"
echo "BRANCH_DELETED=0"
echo "MESSAGE=合并成功，但查询分支状态异常 (HTTP ${BRANCH_CHECK})，请手动确认"
exit 0
