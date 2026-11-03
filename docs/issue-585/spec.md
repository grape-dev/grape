# Spec: Issue #585 — GitHub 通道 pulls comments create 422 + pulls files 分页截断

> Developability: ready（Issue Agent 发布，2026-10-31；issue agent 复核确认 2026-11-02）
> Target branch: `weekly-08-mys`（本流水线基线；前序 Spec 所写 weekly-09 为前序场景基线，已由 issue agent 修正）

## Objective
修复 GitHub 通道 `pulls comments create` 端点 422 错误 + `pulls files` 分页截断问题，解除 Agent 执行通道阻塞（PR 评论无法发送 / PR 文件清单 >30 截断）。

## Scope
1. **Bug 1（422）**：GitHub 通道 `create_pull_comment` 误用 inline review comment 端点 `/pulls/{n}/comments`（必填 commit_id/path/line），改用 `/issues/{n}/comments`（PR=issue 语义等价，仅需 body）；GitCode 通道行为保持不变。
2. **Bug 2（分页截断）**：`list_pull_files` 增加 `page`/`per_page` 参数透传（默认 per_page=100），调用方可按需翻页。
3. **文档**：技能 runbook 错误排查表补充 422 降级条目 + 大 PR 兜底说明。

## Out of Scope
- GitCode 通道 PR 评论 405 系列反馈（#46/#113/#117）。
- inline review comment 完整参数化支持。

## Acceptance Criteria
- GitHub 通道 `pulls comments create` 对 PR 评论不再返回 422，成功返回评论对象
- `pulls files` 支持 `page`/`per_page` 参数透传，默认 per_page=100（>30 文件不再默认截断）
- runbook 错误排查表含 422 → 降级 `issues comments create` 条目
- 大 PR 兜底（`tree --depth recursive`）说明
- 所有现有测试通过；新增测试覆盖两个修复点
