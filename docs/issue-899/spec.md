# Issue #899 Spec — git-repo commits 支持 --sha/--ref 分支过滤

## 目标
`GET /api/git-repo/{owner}/{repo}/commits` 增加可选查询参数 `sha`（分支名/tag/commit sha）并透传上游 Git 提供方，使 `grape git-repo commits --sha <ref>` 可核对非默认分支（如 weekly-mys/weekly-10）的实现 commit。

## 现状
端点仅有 `per_page`，只返回默认分支历史；issue-runbook Step 3/6 按 `$WORK_BRANCH` 核对无法执行（后台反馈 #863，同源 #690/#863/#853/#839/#813/#615）。

## 期望行为
1. `sha` 可选：不传时向后兼容（默认分支历史，URL 不带 sha 参数）。
2. `sha` 接受分支名、tag、commit sha；上游 GitCode/GitHub `/commits?sha=<ref>` 原生支持。
3. ref 不存在时沿用上游 404 语义（与 tree/contents nonexistent ref 处理一致，对齐 #734）。
4. CLI（契约驱动）在 API 声明后自动渲染 `--sha` 选项。

## 验收标准
- [ ] `grape git-repo commits --owner o --repo r --sha weekly-10` 返回该分支 commit 历史
- [ ] 不传 `--sha` 行为不变（默认分支历史）
- [ ] 现有测试通过；新增测试覆盖 --sha 过滤与缺省兼容

## 非目标
- issue-runbook.md（grape-skills 仓）Step 3/6 示例更新：另一仓文档改动，可并行。
