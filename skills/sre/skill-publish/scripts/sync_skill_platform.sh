#!/usr/bin/env bash
# ============================================================================
# sync_skill_platform.sh — Skill 发布/下架到 GitHub/GitCode 目标仓库
#
# 职责：clone 目标仓库 → 检测已有目录 → 建备份分支 →
#       发布（覆盖 skill 目录）/ 下架（删除 skill 目录）→ 1 次 commit → push
#
# 用法：
#   # 发布
#   sync_skill_platform.sh --action publish --platform gitcode \
#     --skill-name hcs-obs-metaquery --skill-dir /worktree/skills/hcs-obs-metaquery \
#     --version 1.0.0
#
#   # 下架
#   sync_skill_platform.sh --action unpublish --platform gitcode \
#     --skill-name hcs-obs-metaquery
#
# 参数：
#   --action      publish（发布）或 unpublish（下架）
#   --platform    gitcode / github
#   --skill-name  skill 名称（如 hcs-obs-metaquery）
#   --skill-dir   源 skill 目录路径（publish 必填，unpublish 不需要）
#   --version     版本号（publish 用于 commit message，unpublish 不需要）
#   --issue-num   Issue 编号（unpublish 用于 commit message，可选）
#
# 环境变量（已由系统注入）：
#   GITCODE_TOKEN / GITHUB_TOKEN            目标仓库 push 鉴权
#   GITCODE_PUBLISH_REPO / GITHUB_PUBLISH_REPO  目标仓库 namespace/repo
#
# 退出码：0=成功；1=参数/环境错误；2=git 操作失败
# 输出：stdout 一行 JSON：{"PUSHED":0|1,"BACKUP_BRANCH":"","TARGET_URL":"","MESSAGE":""}
# ============================================================================
set -uo pipefail

# ---- 参数解析 ---------------------------------------------------------------
ACTION=""
PLATFORM=""
SKILL_NAME=""
SKILL_DIR=""
VERSION=""
ISSUE_NUM=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --action)     ACTION="$2"; shift 2 ;;
    --platform)   PLATFORM="$2"; shift 2 ;;
    --skill-name) SKILL_NAME="$2"; shift 2 ;;
    --skill-dir)  SKILL_DIR="$2"; shift 2 ;;
    --version)    VERSION="$2"; shift 2 ;;
    --issue-num)  ISSUE_NUM="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --action publish|unpublish --platform gitcode|github"
      echo "       --skill-name <name> [--skill-dir <dir>] [--version <ver>]"
      echo "       [--issue-num <num>]"
      exit 0 ;;
    *) echo "[sync] ERROR: unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ---- 校验参数 ---------------------------------------------------------------
if [[ -z "$ACTION" ]]; then
  echo "[sync] ERROR: --action 必填（publish 或 unpublish）" >&2; exit 1
fi
if [[ "$ACTION" != "publish" && "$ACTION" != "unpublish" ]]; then
  echo "[sync] ERROR: --action 必须是 publish 或 unpublish" >&2; exit 1
fi
if [[ -z "$PLATFORM" ]]; then
  echo "[sync] ERROR: --platform 必填（gitcode 或 github）" >&2; exit 1
fi
if [[ "$PLATFORM" != "gitcode" && "$PLATFORM" != "github" ]]; then
  echo "[sync] ERROR: --platform 必须是 gitcode 或 github" >&2; exit 1
fi
if [[ -z "$SKILL_NAME" ]]; then
  echo "[sync] ERROR: --skill-name 必填" >&2; exit 1
fi
if [[ "$ACTION" == "publish" ]]; then
  if [[ -z "$SKILL_DIR" ]]; then
    echo "[sync] ERROR: publish 时 --skill-dir 必填" >&2; exit 1
  fi
  if [[ ! -d "$SKILL_DIR" ]]; then
    echo "[sync] ERROR: skill-dir 不存在: $SKILL_DIR" >&2; exit 1
  fi
  if [[ -z "$VERSION" ]]; then
    VERSION="0.1.0"
    echo "[sync] WARNING: --version 未指定，默认 $VERSION" >&2
  fi
fi

# ---- 环境变量缺失时从项目知识加载（.project-info/ 下递归扫描 JSON）-----------
# 按当前平台检查所需 token 和 PUBLISH_REPO，任一缺失则从项目知识加载
_need_load=0
if [[ "$PLATFORM" == "gitcode" ]]; then
  [[ -z "${GITCODE_TOKEN:-}" || -z "${GITCODE_PUBLISH_REPO:-}" ]] && _need_load=1
else
  [[ -z "${GITHUB_TOKEN:-}" || -z "${GITHUB_PUBLISH_REPO:-}" ]] && _need_load=1
fi
if [[ "$_need_load" == "1" ]]; then
  while IFS='=' read -r key val; do
    [[ -n "$key" && -n "$val" ]] && export "$key=$val"
  done < <(python3 -c "
import json, glob
for f in glob.glob('.project-info/**/*.json', recursive=True):
    try:
        d = json.load(open(f))
        for k, v in {**d.get('secrets',{}), **d.get('config',{})}.items():
            if v: print(f'{k}={v}')
    except: pass
" 2>/dev/null)
fi

# ---- 解析环境变量 -----------------------------------------------------------
if [[ "$PLATFORM" == "gitcode" ]]; then
  TOKEN="${GITCODE_TOKEN:-}"
  PUBLISH_REPO="${GITCODE_PUBLISH_REPO:-}"
else
  TOKEN="${GITHUB_TOKEN:-}"
  PUBLISH_REPO="${GITHUB_PUBLISH_REPO:-}"
fi

if [[ -z "$TOKEN" ]]; then
  echo "[sync] ERROR: ${PLATFORM^^}_TOKEN 未设置" >&2; exit 1
fi
if [[ -z "$PUBLISH_REPO" ]]; then
  echo "[sync] ERROR: ${PLATFORM^^}_PUBLISH_REPO 未设置" >&2; exit 1
fi

OWNER="${PUBLISH_REPO%%/*}"
REPO="${PUBLISH_REPO#*/}"

# 构建 clone URL（HTTPS + token 鉴权，不依赖 SSH key）
# GitCode 私有仓库必须用 oauth2:{token} 格式；GitHub 用 x-access-token:{token}
if [[ "$PLATFORM" == "gitcode" ]]; then
  CLONE_URL="https://oauth2:${TOKEN}@gitcode.com/${OWNER}/${REPO}.git"
else
  CLONE_URL="https://x-access-token:${TOKEN}@github.com/${OWNER}/${REPO}.git"
fi

log() { echo "[sync:${PLATFORM}] $*"; }

# ---- 1. clone 目标仓库（浅克隆，超时 60s）-----------------------------------
TMP_DIR=$(mktemp -d /tmp/skill-sync-XXXXXX)
log "clone ${PUBLISH_REPO} → ${TMP_DIR}"

if ! timeout 60 git clone --depth 1 --quiet "$CLONE_URL" "$TMP_DIR" 2>/dev/null; then
  # 浅克隆失败 → 完整克隆（某些 GitCode 版本不支持 --depth 1）
  log "浅克隆失败，尝试完整克隆..."
  if ! timeout 120 git clone --quiet "$CLONE_URL" "$TMP_DIR" 2>/dev/null; then
    echo "[sync] ERROR: git clone 失败或超时: ${PUBLISH_REPO}" >&2
    rm -rf "$TMP_DIR"
    exit 2
  fi
fi

cd "$TMP_DIR" || { echo "[sync] ERROR: cd tmp dir failed" >&2; rm -rf "$TMP_DIR"; exit 2; }

# ---- 2. 获取默认分支 --------------------------------------------------------
DEFAULT_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")
log "默认分支: ${DEFAULT_BRANCH}"

# ---- 3. 检测 skills/{name}/ 是否已存在 -------------------------------------
SKILL_TARGET_DIR="skills/${SKILL_NAME}"
SKILL_EXISTS="no"
if [[ -d "$SKILL_TARGET_DIR" ]]; then
  SKILL_EXISTS="yes"
  log "目标仓库已有 ${SKILL_TARGET_DIR}/"
fi

# ---- 4. 处理目录不存在的情况 ------------------------------------------------
if [[ "$ACTION" == "unpublish" && "$SKILL_EXISTS" == "no" ]]; then
  log "${SKILL_TARGET_DIR}/ 不存在，无需下架"
  echo "{\"PUSHED\":0,\"BACKUP_BRANCH\":\"\",\"TARGET_URL\":\"\",\"MESSAGE\":\"${SKILL_NAME} 在 ${PUBLISH_REPO} 中不存在，无需下架\"}"
  rm -rf "$TMP_DIR"
  exit 0
fi

if [[ "$ACTION" == "publish" && "$SKILL_EXISTS" == "no" ]]; then
  log "${SKILL_TARGET_DIR}/ 不存在，首次发布（无需备份）"
fi

# ---- 5. 建备份分支（目录已存在且内容不同时）---------------------------------
BACKUP_BRANCH=""
if [[ "$SKILL_EXISTS" == "yes" ]]; then
  # publish 时先比较内容——内容一致则跳过，不建备份
  if [[ "$ACTION" == "publish" ]]; then
    _tmp_compare=$(mktemp -d /tmp/skill-cmp-XXXXXX)
    cp -r "$SKILL_TARGET_DIR" "$_tmp_compare/old" 2>/dev/null
    find "$_tmp_compare/old" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "$_tmp_compare/old" -name "*.pyc" -delete 2>/dev/null || true
    cp -r "$SKILL_DIR" "$_tmp_compare/new" 2>/dev/null
    find "$_tmp_compare/new" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "$_tmp_compare/new" -name "*.pyc" -delete 2>/dev/null || true
    find "$_tmp_compare/new" -name ".DS_Store" -delete 2>/dev/null || true
    if diff -rq "$_tmp_compare/old" "$_tmp_compare/new" >/dev/null 2>&1; then
      log "内容一致，跳过备份和发布"
      echo "{\"PUSHED\":0,\"BACKUP_BRANCH\":\"\",\"TARGET_URL\":\"\",\"MESSAGE\":\"${SKILL_NAME} 内容与目标仓库一致，无需推送\"}"
      rm -rf "$_tmp_compare" "$TMP_DIR"
      exit 0
    fi
    rm -rf "$_tmp_compare"
  fi
  # 内容不同（或 unpublish）——用 skill name 命名备份分支，覆盖旧的（只保留 1 个）
  if [[ "$ACTION" == "publish" ]]; then
    BACKUP_BRANCH="backup/pre-sync-${SKILL_NAME}"
  else
    BACKUP_BRANCH="backup/pre-delist-${SKILL_NAME}"
  fi
  log "建备份分支: ${BACKUP_BRANCH}（覆盖旧备份）"
  # 删除旧备份分支（本地 + 远端），best-effort
  git branch -D "$BACKUP_BRANCH" 2>/dev/null || true
  timeout 30 git push --quiet origin --delete "$BACKUP_BRANCH" 2>/dev/null || true
  # 建新备份分支
  git branch "$BACKUP_BRANCH" 2>/dev/null || true
  # 推备份分支到远端（force 覆盖，best-effort，超时 30s，失败不阻塞）
  timeout 30 git push --quiet --force origin "$BACKUP_BRANCH" 2>/dev/null || \
    log "WARNING: 备份分支推远端失败或超时（非阻断，不影响发布）"
fi

# ---- 6. 执行目录操作 --------------------------------------------------------
if [[ "$ACTION" == "publish" ]]; then
  # 删除旧目录（如果存在）
  if [[ "$SKILL_EXISTS" == "yes" ]]; then
    rm -rf "$SKILL_TARGET_DIR"
    log "删除旧 ${SKILL_TARGET_DIR}/"
  fi
  # 确保 skills/ 目录存在
  mkdir -p skills
  # 复制新 skill 目录
  cp -r "$SKILL_DIR" "$SKILL_TARGET_DIR"
  log "复制 ${SKILL_DIR} → ${SKILL_TARGET_DIR}/"
  # 删除不应上传的文件（__pycache__/.pyc/.DS_Store）
  find "$SKILL_TARGET_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
  find "$SKILL_TARGET_DIR" -name "*.pyc" -delete 2>/dev/null || true
  find "$SKILL_TARGET_DIR" -name ".DS_Store" -delete 2>/dev/null || true
  # commit message
  COMMIT_MSG="feat: publish skill ${SKILL_NAME} v${VERSION}"
else
  # unpublish: 删除 skill 目录
  rm -rf "$SKILL_TARGET_DIR"
  log "删除 ${SKILL_TARGET_DIR}/"
  # commit message
  if [[ -n "$ISSUE_NUM" ]]; then
    COMMIT_MSG="unpublish: remove skill ${SKILL_NAME} (Issue #${ISSUE_NUM})"
  else
    COMMIT_MSG="unpublish: remove skill ${SKILL_NAME}"
  fi
fi

# ---- 7. git add + commit（1 skill = 1 commit）------------------------------
git add -A
# 检查是否有变更
if git diff --cached --quiet 2>/dev/null; then
  log "无变更（内容与目标仓库一致），跳过 commit"
  echo "{\"PUSHED\":0,\"BACKUP_BRANCH\":\"\",\"TARGET_URL\":\"\",\"MESSAGE\":\"${SKILL_NAME} 内容与目标仓库一致，无需推送\"}"
  rm -rf "$TMP_DIR"
  exit 0
fi

git commit --quiet -m "$COMMIT_MSG" || {
  echo "[sync] ERROR: git commit 失败" >&2
  rm -rf "$TMP_DIR"
  exit 2
}
log "commit: ${COMMIT_MSG}"

# ---- 8. git push（超时 60s）-------------------------------------------------
log "push origin ${DEFAULT_BRANCH}"
if ! timeout 60 git push --quiet origin "$DEFAULT_BRANCH" 2>/dev/null; then
  echo "[sync] ERROR: git push 失败或超时" >&2
  rm -rf "$TMP_DIR"
  exit 2
fi

# ---- 9. 输出结果 ------------------------------------------------------------
# 构建目标 URL
if [[ "$PLATFORM" == "gitcode" ]]; then
  TARGET_URL="https://gitcode.com/${OWNER}/${REPO}/tree/${DEFAULT_BRANCH}/${SKILL_TARGET_DIR}"
else
  TARGET_URL="https://github.com/${OWNER}/${REPO}/tree/${DEFAULT_BRANCH}/${SKILL_TARGET_DIR}"
fi

echo "{\"PUSHED\":1,\"BACKUP_BRANCH\":\"${BACKUP_BRANCH}\",\"TARGET_URL\":\"${TARGET_URL}\",\"MESSAGE\":\"\"}"
log "完成: ${TARGET_URL}"

# ---- 10. 清理临时目录 -------------------------------------------------------
rm -rf "$TMP_DIR"
exit 0
