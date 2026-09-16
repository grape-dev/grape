#!/usr/bin/env bash
# =====================================================================
# adapt_skills_vcs.sh — VCS 多 provider 适配兜底脚本
#
# 作用：
#   在 sync_skills.sh 拉取官方 skills 后，自动将硬编码 GitCode 的 skill
#   文件转换为多 VCS provider 兼容格式（$BOT_TOKEN / $VCS_API_BASE）。
#   幂等：已适配的文件不会被二次修改。
#
# 适配规则：
#   1. $BOT_GITCODE_TOKEN → $BOT_TOKEN（token 变量统一）
#   2. https://api.gitcode.com/api/v5 → $VCS_API_BASE（API URL 变量化）
#   3. https://gitcode.com/api/v5 → $VCS_API_BASE（同上，另一种写法）
#      排除：raw.gitcode.com / gitcode.com/api/v5/repos/developer-skill/
#      gitcode.com/api/v5/repos/2501_91318609/（外部资源，不是仓库 API）
#   4. "GitCode API" → "VCS API"（描述文本）
#      排除：gitcode_bot / grape_bot（bot 用户名保留）
#   5. gitcode-api-reference.md → vcs-api-reference.md（文件重命名+引用更新）
#      重命名后追加 GitHub 与 GitCode 差异对照表
#   6. "gitcode_bot 机器人账号" → "bot 机器人账号"
#      "GitCode用户名" → "VCS用户名"
#      "GitCode 仓库设置" → "仓库设置"
#
# 触发条件：sync_skills.sh 拉取后自动调用，或手动执行
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SKILLS_DIR="$ROOT_DIR/skills"

# GitHub 差异对照表（追加到 vcs-api-reference.md 末尾）
GITHUB_DIFF_SECTION='

## GitHub 与 GitCode 差异对照（重要！）

调用 VCS API 前先确认仓库 provider（由 prompt 的 VCS API Access 段指定），按下表选择正确的参数格式：

| 功能 | GitCode | GitHub |
|------|---------|--------|
| Issue 关闭 | `state="close"` | `state="closed"` |
| labels 格式 | 逗号分隔字符串 `"bug,feature"` | 数组 `["bug","feature"]` |
| assignees | `assignee` 单值字符串 | `assignees` 数组 `["user1"]` |
| Create Issue body | 需带 `repo` 字段 | 不需要（repo 在 URL 里） |
| PR 普通评论 | `POST /pulls/:n/comments` | `POST /issues/:n/comments`（GitHub 把 PR 视为 Issue） |
| Create file | `POST /contents/:path` | `PUT /contents/:path` |
| Update file | `PUT /contents/:path`（需 `sha`） | `PUT /contents/:path`（需 `sha`） |
| Create branch | `POST /branches`（body: `branch_name`, `ref`） | `POST /git/refs`（body: `ref`, `sha`） |
| Delete file | 不支持 | `DELETE /contents/:path` |
| Accept header | 不需要 | `application/vnd.github+json`（必须带！） |
| Create label | form-encoded | JSON body |
| Add labels to issue | 通过 `PATCH /issues/:n` 的 `labels` 字段 | `POST /issues/:n/labels`（专用端点） |
| Rate limit | 400 req/min | 5000 req/hour（认证用户） |
| 关联 Issue | `inner_issue_nums` / `close_related_issue` | PR body 中 `Closes #N` / `Fixes #N` |'

adapted=0
skipped=0

echo "[adapt-vcs] scanning $SKILLS_DIR ..."

# ── 1. SKILL.md + references/*.md 文本替换 ──────────────────────────
# 用 find + sed 批量处理，幂等：已含 $VCS_API_BASE 的文件跳过

find "$SKILLS_DIR" -type f \( -name '*.md' -o -name '*.sh' -o -name '*.py' -o -name '*.js' \) | while IFS= read -r f; do
  # 幂等检查：如果文件不含任何待替换 pattern，跳过
  # 待替换 pattern：$BOT_GITCODE_TOKEN、api.gitcode.com/api/v5（非外部资源）、GitCode API、gitcode-api-reference.md
  # 注意：gitcode.com/api/v5/repos/developer-skill 和 2501_91318609 是外部资源，不算待替换
  _needs_adapt=0
  if grep -q '\$BOT_GITCODE_TOKEN\|BOT_GITCODE_TOKEN' "$f" 2>/dev/null; then _needs_adapt=1; fi
  if grep -q 'USER_GITCODE_TOKEN' "$f" 2>/dev/null; then _needs_adapt=1; fi
  # api.gitcode.com/api/v5（排除说明性文字中的 api.gitcode.com）
  if grep -q 'https://api\.gitcode\.com/api/v5' "$f" 2>/dev/null; then _needs_adapt=1; fi
  # gitcode.com/api/v5（排除外部仓库）
  if grep -q 'https://gitcode\.com/api/v5' "$f" 2>/dev/null; then
    # 检查是否有非外部仓库的 URL（需要替换的）
    if grep 'https://gitcode\.com/api/v5' "$f" 2>/dev/null | grep -v 'developer-skill\|2501_91318609' | grep -q .; then _needs_adapt=1; fi
  fi
  if grep -q 'GitCode API' "$f" 2>/dev/null; then _needs_adapt=1; fi
  if grep -q 'gitcode-api-reference\.md' "$f" 2>/dev/null; then _needs_adapt=1; fi
  if grep -q 'gitcode_bot' "$f" 2>/dev/null; then _needs_adapt=1; fi
  if grep -q 'GitCode用户名' "$f" 2>/dev/null; then _needs_adapt=1; fi
  if grep -q 'GitCode 仓库设置\|在 GitCode 仓库设置' "$f" 2>/dev/null; then _needs_adapt=1; fi
  if grep -q '# GitCode API 参考' "$f" 2>/dev/null; then _needs_adapt=1; fi

  if [ "$_needs_adapt" = "0" ]; then
    continue
  fi

  changed=0

  # 1) $BOT_GITCODE_TOKEN → $BOT_TOKEN
  if grep -q '\$BOT_GITCODE_TOKEN' "$f" 2>/dev/null; then
    sed -i 's/\$BOT_GITCODE_TOKEN/\$BOT_TOKEN/g' "$f"
    changed=1
  fi

  # 2) BOT_GITCODE_TOKEN（不带 $，如 os.environ["BOT_GITCODE_TOKEN"]）→ BOT_TOKEN
  if grep -q 'BOT_GITCODE_TOKEN' "$f" 2>/dev/null; then
    sed -i 's/BOT_GITCODE_TOKEN/BOT_TOKEN/g' "$f"
    changed=1
  fi

  # 3) USER_GITCODE_TOKEN → USER_TOKEN（chat skill 用）
  if grep -q 'USER_GITCODE_TOKEN' "$f" 2>/dev/null; then
    sed -i 's/USER_GITCODE_TOKEN/USER_TOKEN/g' "$f"
    changed=1
  fi

  # 4) https://api.gitcode.com/api/v5 → $VCS_API_BASE
  if grep -q 'https://api\.gitcode\.com/api/v5' "$f" 2>/dev/null; then
    sed -i 's|https://api\.gitcode\.com/api/v5|\$VCS_API_BASE|g' "$f"
    changed=1
  fi

  # 5) https://gitcode.com/api/v5 → $VCS_API_BASE
  #    排除 raw.gitcode.com 和外部仓库（developer-skill / 2501_91318609）
  if grep -q 'https://gitcode\.com/api/v5' "$f" 2>/dev/null; then
    # 只替换形如 https://gitcode.com/api/v5/repos/{owner}/{repo} 的仓库 API 调用
    # 不替换 https://gitcode.com/api/v5/repos/developer-skill/ 和 2501_91318609
    sed -i '/raw\.gitcode\.com/!{/developer-skill/!{/2501_91318609/!{s|https://gitcode\.com/api/v5|\$VCS_API_BASE|g}}}' "$f"
    changed=1
  fi

  # 6) "GitCode API" → "VCS API"（描述文本，不影响文件名/路径引用）
  if grep -q 'GitCode API' "$f" 2>/dev/null; then
    sed -i 's/GitCode API/VCS API/g' "$f"
    changed=1
  fi

  # 7) gitcode-api-reference.md → vcs-api-reference.md（文件引用）
  if grep -q 'gitcode-api-reference\.md' "$f" 2>/dev/null; then
    sed -i 's/gitcode-api-reference\.md/vcs-api-reference.md/g' "$f"
    changed=1
  fi

  # 8) "gitcode_bot 机器人账号" → "bot 机器人账号"
  if grep -q 'gitcode_bot' "$f" 2>/dev/null; then
    sed -i 's/gitcode_bot/grape_bot/g' "$f"
    changed=1
  fi

  # 9) "GitCode用户名" → "VCS用户名" / "GitCode 仓库设置" → "仓库设置"
  if grep -q 'GitCode用户名' "$f" 2>/dev/null; then
    sed -i 's/GitCode用户名/VCS用户名/g' "$f"
    changed=1
  fi
  if grep -q 'GitCode 仓库设置' "$f" 2>/dev/null; then
    sed -i 's/GitCode 仓库设置/仓库设置/g' "$f"
    changed=1
  fi
  if grep -q '在 GitCode 仓库设置' "$f" 2>/dev/null; then
    sed -i 's/在 GitCode 仓库设置/在仓库设置/g' "$f"
    changed=1
  fi

  # 10) "Bot_GITCODE_TOKEN" → "BOT_TOKEN" (大写/混合写法兜底)
  if grep -q 'BOT_GITCODE_TOKEN' "$f" 2>/dev/null; then
    sed -i 's/BOT_GITCODE_TOKEN/BOT_TOKEN/g' "$f"
    changed=1
  fi

  # 11) "# GitCode API 参考" → "# VCS API 参考"
  if grep -q '# GitCode API 参考' "$f" 2>/dev/null; then
    sed -i 's/# GitCode API 参考/# VCS API 参考/g' "$f"
    changed=1
  fi

  if [ "$changed" = "1" ]; then
    adapted=$((adapted + 1))
    echo "[adapt-vcs] adapted: ${f#$SKILLS_DIR/}"
  fi
done

# ── 2. gitcode-api-reference.md → vcs-api-reference.md 重命名 ────────
# 重命名后追加 GitHub 差异对照表（如果还没有的话）
find "$SKILLS_DIR" -type f -name 'gitcode-api-reference.md' | while IFS= read -r f; do
  dir="$(dirname "$f")"
  target="$dir/vcs-api-reference.md"

  if [ -f "$target" ]; then
    # 目标已存在，只删除旧文件
    rm -f "$f"
    echo "[adapt-vcs] removed old (target exists): ${f#$SKILLS_DIR/}"
  else
    mv "$f" "$target"
    echo "[adapt-vcs] renamed: ${f#$SKILLS_DIR/} → vcs-api-reference.md"
  fi

  # 追加 GitHub 差异表（幂等：已有则跳过）
  if ! grep -q 'GitHub 与 GitCode 差异对照' "$target" 2>/dev/null; then
    printf '%s\n' "$GITHUB_DIFF_SECTION" >> "$target"
    echo "[adapt-vcs] appended GitHub diff table: ${target#$SKILLS_DIR/}"
  fi
done

echo "[adapt-vcs] done (adapted=$adapted)"
