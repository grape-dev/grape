# Spec: Issue #580 — labels create 颜色带 # 前缀在 GitHub 通道返回 422

> 依据 Issue Agent 已发布 Spec（Issue #580 评论 id 197187224）落地的开发文档。

## Objective
修复 `grape git-repo labels create` 在 GitHub 通道因 `color` 带 `#` 前缀返回 422 的缺陷：透传层对 `color` 做 `lstrip('#')` 归一化，标签模型默认值去 `#`，仓库内 API 文档示例同步修订。

## In Scope
1. `src/api/git_repo_api.py`：`LabelCreateBody.color` 默认值 `#d73a4a` → `d73a4a`。
2. `labels create` 透传前对 `color` 执行 `lstrip('#')` 归一化（仓库无独立 labels update 端点，无该路径需改）。
3. 仓库内 color 示例带 `#` 的 API 文档修订：`help/api/10-git-repos.md`、`docs/openapi/10-git-repos.md`（`#d73a4a` → `d73a4a`）。
4. 测试：GitHub/GitCode 双通道 × 带/不带 `#` 双输入回归。

## Out of Scope
- Issue #581（labels 数组化），根因不同独立处理。
- issue-cli 技能内文档（labeling-guide.md / issue-runbook.md）：不在本仓库，属技能材料，另行修订。

## Acceptance Criteria
- GitHub 通道 `labels create {"color":"#e11d21"}` 成功，透传 body 的 color 为 `e11d21`。
- GitHub 通道不带 `#` 输入行为不回归。
- GitCode 通道带/不带 `#` 均正常（回归）。
- 默认 color（不传）不再带 `#`。
- 仓库内 API 文档示例不再带 `#`。
- 现有测试全部通过。

## 方案
| 决策 | 选择 | 理由 |
|------|------|------|
| 归一化位置 | 服务端透传层 | 单点修复，所有客户端受益 |
| 默认值 | 模型默认值也去 # | 源头与透传双保险 |
| 格式校验 | 保留宽容（仅 strip #） | 兼容既有输入，避免破坏 |
