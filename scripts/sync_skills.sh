#!/usr/bin/env bash
# =====================================================================
# sync_skills.sh — 构建/部署时从独立技能仓库拉取官方技能
# 方案：skills-standalone-repo-build-time-pull.md（方案 C）
#
# 作用：
#   1. 从 grape-skills 仓库拉取官方技能（skills/<agent_type>/<skill>）
#   2. 铺入部署根 skills/（保留运行时目录 custom/、repo/）
#   3. 铺入 skill-description.json（官方技能清单，供 DB seed / sync-official）
#
# 用法：
#   bash scripts/sync_skills.sh
#   SKILLS_REPO=... SKILLS_REF=main bash scripts/sync_skills.sh   # 覆盖默认
# =====================================================================
set -euo pipefail

SKILLS_REPO="${SKILLS_REPO:-https://Jason7602:shhnrXxGm1E1SJ-FxKxQ9M2Q@gitcode.com/grape-dev/grape-skills.git}"
SKILLS_REF="${SKILLS_REF:-main}"

# 官方技能 agent_type 目录（+ _template），按部署结构铺入
SKILL_AGENT_DIRS=(vod issue coding qa sre chat _template)

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "[sync-skills] fetch ${SKILLS_REPO}@${SKILLS_REF}"
git clone --depth 1 --branch "$SKILLS_REF" "$SKILLS_REPO" "$STAGE/skills-repo" >/dev/null 2>&1

# 1) 官方技能 → 部署根 skills/<agent>/（只清官方技能，保留 custom/ repo/ 运行时目录）
for d in "${SKILL_AGENT_DIRS[@]}"; do
  rm -rf "$ROOT_DIR/skills/$d"
  if [ -d "$STAGE/skills-repo/skills/$d" ]; then
    cp -a "$STAGE/skills-repo/skills/$d" "$ROOT_DIR/skills/$d"
    echo "[sync-skills] synced skills/$d"
  else
    echo "[sync-skills] WARN: skills/$d missing in repo, skip"
  fi
done

# 2) skill-description.json → 部署根 configs/
if [ -f "$STAGE/skills-repo/skill-description.json" ]; then
  cp -a "$STAGE/skills-repo/skill-description.json" "$ROOT_DIR/configs/skill-description.json"
  echo "[sync-skills] synced configs/skill-description.json"
else
  echo "[sync-skills] WARN: skill-description.json missing in repo" >&2
fi

echo "[sync-skills] done (${SKILLS_REPO}@${SKILLS_REF})"