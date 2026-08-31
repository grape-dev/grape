#!/usr/bin/env bash
# =====================================================================
# sync_templates.sh — 构建/部署时从独立模板仓库拉取官方项目模板
# 方案：docs/new-version3/project-templates-standalone-repo-design.md（方案 C）
#
# 作用：
#   1. 从 grape-template 仓库拉取官方模板（每个模板一个目录，目录名即模板 id）
#   2. 铺入部署根 examples/（运行时常量 EXAMPLE_DIR 读 <grape>/examples）
#   3. 铺入 project-templates.json（官方模板注册表，供 API + admin 读取）
#
# 用法：
#   bash scripts/sync_templates.sh
#   TEMPLATES_REPO=... TEMPLATES_REF=main bash scripts/sync_templates.sh   # 覆盖默认
# =====================================================================
set -euo pipefail

TEMPLATES_REPO="${TEMPLATES_REPO:-https://gitcode.com/grape-dev/grape-template.git}"
TEMPLATES_REF="${TEMPLATES_REF:-main}"

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "[sync-templates] fetch ${TEMPLATES_REPO}@${TEMPLATES_REF}"
git clone --depth 1 --branch "$TEMPLATES_REF" "$TEMPLATES_REPO" "$STAGE/templates" >/dev/null 2>&1

# 1) 官方模板 → 部署根 examples/（只同步子目录，忽略 README/project-templates.json 等非模板文件）
mkdir -p "$ROOT_DIR/examples"
for d in "$STAGE/templates"/*/; do
  name="$(basename "$d")"
  if [ "$name" = ".git" ]; then
    continue
  fi
  rm -rf "$ROOT_DIR/examples/$name"
  cp -a "$d" "$ROOT_DIR/examples/$name"
  echo "[sync-templates] synced examples/$name"
done

# 2) project-templates.json → 部署根 configs/
if [ -f "$STAGE/templates/project-templates.json" ]; then
  cp -a "$STAGE/templates/project-templates.json" "$ROOT_DIR/configs/project-templates.json"
  echo "[sync-templates] synced configs/project-templates.json"
else
  echo "[sync-templates] WARN: project-templates.json missing in repo" >&2
fi

echo "[sync-templates] done (${TEMPLATES_REPO}@${TEMPLATES_REF})"