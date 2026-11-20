#!/usr/bin/env bash
# ============================================================================
# merge_skill_pr.sh — QA Agent 测试通过后调用的合并脚本(grape CLI 版)
#
# 职责：grape pulls merge 自动合并 PR → 查询源分支是否已删
#       （grape 0.3.3 无 branches delete 接口，删除失败降级为告警）
# 测试通过后统一自动合并，不再区分只读/变更类。
#
# 用法：
#   merge_skill_pr.sh <pr_number>
#
# 环境变量（已由系统注入）：
#   REPO_FULL_NAME     仓库全名 owner/repo（必填）
#   平台认证：grape CLI 自动读取 $GRAPE_SESSION_TOKEN，无需手动配置
#
# 退出码：0=调用成功（合并结果看 MERGED）；1=参数/环境错误；2=merge 调用失败
# 输出：MERGED=1/0 + BRANCH_DELETED=1/0 + MESSAGE
# ============================================================================
set -uo pipefail

PR_NUMBER="${1:-}"

if [[ -z "$PR_NUMBER" ]]; then
  echo "[merge_skill_pr] ERROR: pr_number 必填"
  exit 1
fi

REPO_FULL="${REPO_FULL_NAME:-}"

if [[ -z "$REPO_FULL" ]]; then
  echo "[merge_skill_pr] ERROR: REPO_FULL_NAME 未设置"
  exit 1
fi
if ! command -v grape >/dev/null 2>&1; then
  echo "[merge_skill_pr] ERROR: grape CLI 未安装或不在 PATH（安装见 skill references/grape-cli-installation.md）"
  exit 1
fi

OWNER="${REPO_FULL%%/*}"
REPO="${REPO_FULL#*/}"

log() { echo "[merge_skill_pr] $*"; }

# ============================================================================
# 调 grape pulls merge 自动合并
# ============================================================================
log "合并 PR #${PR_NUMBER}: grape git-repo pulls merge"
MERGE_RESP=$(grape git-repo pulls merge \
  --owner "$OWNER" --repo "$REPO" --number "$PR_NUMBER" \
  --json-out 2>/dev/null)

if [[ -z "$MERGE_RESP" ]]; then
  log "ERROR: merge 调用失败（grape 无输出）"
  echo "MERGED=0"
  echo "BRANCH_DELETED=0"
  echo "MESSAGE=merge 调用失败，请手动合并"
  exit 2
fi

# 检查 merged 字段（有些版本不返回 merged 字段，有输出即视为成功——但 merged=False 必须判失败）
MERGED=$(echo "$MERGE_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('merged', ''))" 2>/dev/null || true)
if [[ "$MERGED" == "False" || "$MERGED" == "false" || "$MERGED" == "0" ]]; then
  log "ERROR: 合并失败（merged=${MERGED}）"
  echo "MERGED=0"
  echo "BRANCH_DELETED=0"
  echo "MESSAGE=合并失败（merged=${MERGED}）"
  exit 2
fi
log "PR #${PR_NUMBER} 合并成功（merged=${MERGED:-无字段-有响应即视为成功}）"
echo "MERGED=1"

# ============================================================================
# 查询源分支是否已删除（grape pulls merge 无 prune_branch 参数）
# ============================================================================
# 获取 PR 的源分支名（grape pulls get）
PR_DETAIL=$(grape git-repo pulls get \
  --owner "$OWNER" --repo "$REPO" --number "$PR_NUMBER" \
  --json-out 2>/dev/null)
BRANCH_NAME=$(echo "$PR_DETAIL" | python3 -c "import json,sys; print(json.load(sys.stdin).get('head',{}).get('ref',''))" 2>/dev/null || true)

if [[ -z "$BRANCH_NAME" ]]; then
  log "WARNING: 无法获取源分支名，跳过分支检查"
  echo "BRANCH_DELETED=0"
  echo "MESSAGE=合并成功，但无法获取源分支名，请手动确认分支是否删除"
  exit 0
fi

# 查询分支是否还存在（grape git-repo branches list）
BRANCHES=$(grape git-repo branches \
  --owner "$OWNER" --repo "$REPO" --per_page 100 \
  --json-out 2>/dev/null)
BRANCH_EXISTS=$(echo "$BRANCHES" | python3 -c "
import json,sys
try:
    for b in json.load(sys.stdin):
        if b.get('name') == '${BRANCH_NAME}':
            print('yes')
            break
except Exception:
    pass
" 2>/dev/null)

if [[ "$BRANCH_EXISTS" != "yes" ]]; then
  log "源分支 ${BRANCH_NAME} 已删除"
  echo "BRANCH_DELETED=1"
  echo "MESSAGE=合并成功，源分支已删除"
  exit 0
fi

# ⚠️ grape CLI 0.3.3 无 branches delete 接口（原实现 curl DELETE /branches/{name} 属平台 API 直连，已按纪律移除）
log "WARNING: 源分支 ${BRANCH_NAME} 仍存在，但 grape CLI 无分支删除接口——请手动清理"
echo "BRANCH_DELETED=0"
echo "MESSAGE=合并成功，但源分支 ${BRANCH_NAME} 未删除（grape CLI 无分支删除接口），请手动清理"
exit 0