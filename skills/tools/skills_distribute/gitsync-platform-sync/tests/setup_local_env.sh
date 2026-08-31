#!/usr/bin/env bash
# 搭建本地测试环境：用本地 git bare 仓库模拟 gitcode / github / ai_shell
# 用法: bash tests/setup_local_env.sh <工作目录> [--with-skills]
set -euo pipefail

BASE="${1:-/tmp/gitsync-local}"
WITH_SKILLS="${2:-}"

echo "==> 准备测试目录: $BASE"
rm -rf "$BASE"
mkdir -p "$BASE"

cd "$BASE"

# ---- 1. 创建三个 bare 仓库模拟远端 ----
echo "==> 创建 bare 仓库 (gitcode / github / ai_shell)"
for r in gitcode.git github.git aishell.git; do
  git init -q --bare "$r"
done

# ---- 2. 用工作仓库 seed gitcode: master 分支 + github 分支 ----
echo "==> seed gitcode 内容"
mkdir -p seed && cd seed
git init -q
git config user.name "Dev A"
git config user.email "dev.a@example.com"

# 3 个 commit
for i in 1 2 3; do
  echo "content-$i" > "file$i.txt"
  git add .
  GIT_AUTHOR_DATE="2024-01-0${i}T10:00:00+08:00" \
  GIT_COMMITTER_DATE="2024-01-0${i}T10:00:00+08:00" \
  git commit -q -m "commit $i"
done
C2=$(git rev-parse HEAD~1)

# github 分支落后于 master（只到 commit 2）
git branch github "$C2"

# 可选：加 skills 目录
if [ "$WITH_SKILLS" = "--with-skills" ]; then
  mkdir -p skills/ai/maas/huawei-demo
  cat > skills/ai/maas/huawei-demo/SKILL.md <<'MD'
---
name: huawei-demo
description: demo skill
---
# Demo skill
MD
  git add .
  GIT_AUTHOR_DATE="2024-01-04T10:00:00+08:00" \
  GIT_COMMITTER_DATE="2024-01-04T10:00:00+08:00" \
  git commit -q -m "add demo skill"
fi

git remote add origin "$BASE/gitcode.git"
git push -q origin master github
cd "$BASE"
rm -rf seed

# ---- 3. seed github 仓库: master 指向 gitcode github 分支 tip（原始身份，模拟需 force 对齐）----
echo "==> seed github 仓库"
# 先把 gitcode 的对象推给 github（github 是空 bare）
git --git-dir=gitcode.git push -q "$BASE/github.git" refs/heads/github
# 把 github 的 master 设为 gitcode github 分支 tip（原始身份，未重写）
GITCODE_GH_TIP=$(git --git-dir=gitcode.git rev-parse refs/heads/github)
git --git-dir=github.git update-ref refs/heads/master "$GITCODE_GH_TIP"
git --git-dir=github.git symbolic-ref HEAD refs/heads/master
echo "   github master = gitcode github 分支 tip ($(git --git-dir=github.git rev-parse refs/heads/master | head -c 8))"

# ---- 4. 写 config ----
echo "==> 写 config.yaml"
cat > config.yaml <<YAML
identity:
  name: "GitHub Mirror Bot"
  email: "mirror-bot@example.com"
remotes:
  gitcode: "$BASE/gitcode.git"
  github: "$BASE/github.git"
  gitcode_ai_shell: "$BASE/aishell.git"
branches:
  source_master: "master"
  source_branch: "github"
targets:
  - name: github_master
    remote: github
    branch: master
    rewrite_identity: true
    match_strategy: content
  - name: ai_shell
    remote: gitcode_ai_shell
    branch: ai_shell
    rewrite_identity: false
    match_strategy: sha
map_file: "$BASE/map.txt"
acknowledged_commits: []
backup_ref: "refs/heads/backup/pre-sync-"
tmpdir: ""
clawhub:
  skills_dir: "skills"
  manifest_file: "$BASE/clawhub-manifest.json"
  manifest_backup_file: "$BASE/clawhub-manifest.backup.json"
db:
  product_dir: "$BASE/products"
YAML

echo ""
echo "==> 环境就绪: $BASE"
echo "配置文件: $BASE/config.yaml"
echo "使用: gitsync --config $BASE/config.yaml <命令>"
echo ""
echo "  # 查看同步状态"
echo "  gitsync --config $BASE/config.yaml check --json"
echo ""
echo "  # 首次同步（github 目标需 force 对齐）"
echo "  gitsync --config $BASE/config.yaml sync --force --verbose"
echo ""
echo "  # 再次检查（应已同步）"
echo "  gitsync --config $BASE/config.yaml check --json"
