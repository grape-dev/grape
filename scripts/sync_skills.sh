#!/usr/bin/env bash
# =====================================================================
# sync_skills.sh — 构建/部署时从独立仓库拉取官方技能与官方场景
# 方案：skills-standalone-repo-build-time-pull.md（方案 C）
#
# 作用：
#   1. 从 grape-skills 仓库拉取官方技能（skills/<agent_type>/<skill>）
#   2. 铺入部署根 skills/（保留运行时目录 custom/、repo/）
#   3. 铺入 skill-description.json（官方技能清单，供 DB seed / sync-official）
#   4. 铺入官方场景（scenes/<scene_name>/scene.json + README.md + icon.*）
#      到 configs/scenes/（官方场景库与技能同仓；scene_sync 运行时另有远程同步）
#
# 用法：
#   bash scripts/sync_skills.sh
#   SKILLS_REPO=... SKILLS_REF=main bash scripts/sync_skills.sh   # 覆盖默认
# =====================================================================
set -euo pipefail

# 不再内联凭据（避免进 git）；用 `~/.git-credentials`（store），环境当前身份自动匹配
# （grape-skills 为私有仓库，凭据以环境实际配置为准，不强绑特定账号）
SKILLS_REPO="${SKILLS_REPO:-https://gitcode.com/grape-dev/grape-skills.git}"
SKILLS_REF="${SKILLS_REF:-main}"

# 官方技能 agent_type 目录（+ _template），按部署结构铺入
SKILL_AGENT_DIRS=(vod issue coding qa sre chat _template)

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# 官方场景落地目录（与 skill-description.json / project-templates.json 同级）
SCENES_DIR="$ROOT_DIR/configs/scenes"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "[sync-skills] fetch ${SKILLS_REPO}@${SKILLS_REF}"
# 使用环境当前身份（git credential store 按 URL 自动匹配 ~/.git-credentials），不强绑特定账号
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

# 3) 官方场景 → configs/scenes/（从 grape-skills 仓库 scenes/ 目录，与技能同仓）
#    结构：scenes/<scene_name>/scene.json + README.md + icon.*；官方场景由 scene_sync 运行时再同步到 DB，
#    此处落地供离线存档 / 手动导入 / 审查。
if [ -d "$STAGE/skills-repo/scenes" ]; then
  rm -rf "$SCENES_DIR"
  mkdir -p "$SCENES_DIR"
  # 排除模板/第三方/系统目录
  copied=0
  for sdir in "$STAGE/skills-repo/scenes/"*/; do
    base="$(basename "$sdir")"
    case "$base" in
      _template|third-party|__MACOSX|.DS_Store|.git) continue ;;
    esac
    [ -d "$sdir" ] || continue
    cp -a "$sdir" "$SCENES_DIR/$base"
    copied=$((copied + 1))
  done
  echo "[sync-skills] synced $copied official scenes -> $SCENES_DIR"
else
  echo "[sync-skills] WARN: scenes/ missing in repo, no official scenes copied" >&2
fi

echo "[sync-skills] done (${SKILLS_REPO}@${SKILLS_REF})"

# ── VCS 多 provider 适配（兜底）──────────────────────────────────────
# 将硬编码 GitCode 的 skill 文件转换为多 VCS provider 兼容格式。
# 幂等：已适配的文件跳过。skills 仓库自行适配后此调用也无副作用。
bash "$(dirname "$0")/adapt_skills_vcs.sh"