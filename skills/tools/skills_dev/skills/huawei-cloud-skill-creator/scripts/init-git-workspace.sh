#!/usr/bin/env bash
# 初始化 git workspace 从 master，防止 PR 分支成为孤立 root commit
# 背景：multica workspace 默认 git init + origin remote，无 master 内容
# 直接写文件提交 → commit(initial) 孤立提交 → PR diff 误将 master 全量文件视为删除
set -euo pipefail

SKILL_NAME="${1:-}"
log() { echo "[init-git-workspace] $*"; }

# 未传参则从当前 branch 名自动推断（multica 预设分支 add-skill/<name>）
if [[ -z "$SKILL_NAME" ]]; then
  SKILL_NAME=$(git symbolic-ref --short HEAD 2>/dev/null | sed 's|^add-skill/||')
  [[ -n "$SKILL_NAME" ]] || { log "ERROR: skill_name 未提供且无法推断"; exit 1; }
  log "Auto-detected skill_name: ${SKILL_NAME}"
fi

BRANCH="add-skill/${SKILL_NAME}"
git fetch origin master 2>&1 | sed 's/^/  /'
git checkout -B "${BRANCH}" origin/master 2>&1 | sed 's/^/  /'
MERGE_BASE=$(git merge-base HEAD origin/master 2>/dev/null || echo "")
[[ -n "$MERGE_BASE" ]] || { log "ERROR: merge-base 为空，分支仍未继承 master 历史"; exit 1; }

log "Done. Branch ${BRANCH} based on origin/master, merge-base=${MERGE_BASE}"
exit 0