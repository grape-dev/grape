# Plan: Issue #585 — GitHub 通道 PR 评论 422 + PR 文件分页截断

## 方案

### Bug 1（create_pull_comment 422）
- 文件：`src/api/git_repo_api.py` `create_pull_comment`
- 按 provider 分流请求路径：GitHub → `POST /issues/{number}/comments`（PR 本身是 issue，仅需 body）；GitCode → 保持 `/pulls/{number}/comments`（行为不变，向后兼容）。
- grape-auto-comment 标记追加、outbound_ledger 入账（issue_comment 键）逻辑不变，对两通道同样生效。
- 端点 description 注明 Issue #585 契约。

### Bug 2（list_pull_files 分页截断）
- 文件：`src/api/git_repo_api.py` `list_pull_files`
- 新增 `page: int = Query(1, ge=1)`、`per_page: int = Query(100, ge=1, le=100)` 查询参数，经 `_vcs_url` params 透传上游。
- 默认 per_page=100 消除 GitHub 默认 30 的静默截断；API 层不自动循环翻页（避免大 PR 超时），由调用方按 `changed_files` 手动翻页。

### 文档
- `.grape/skills/qa/*/references/*runbook*.md` 错误排查速查表补充：`pulls comments create` 422 → 降级 `issues comments create`；`pulls files` 分页参数与大 PR 兜底（`tree --depth recursive`）。

## 测试
- `tests/test_git_repo_api.py` 新增：
  - GitHub 通道 PR 评论走 `/issues/{n}/comments` 且带 marker（test_create_pull_comment_github_uses_issue_comments_endpoint）
  - GitCode 通道保持 `/pulls/{n}/comments`（test_create_pull_comment_gitcode_keeps_pulls_endpoint）
  - `pulls files` 分页参数透传 + 非法值 422（test_pulls_files_pagination_passthrough）
  - 默认分页参数断言（test_pulls_mapping 扩展）
- 回归：test_git_repo_api.py / test_issue_453_outbound_ledger.py / test_issue_372_deep_analysis.py / test_openapi.py。

## 风险
- issues 评论显示在 PR conversation 区（普通评论），与 GitCode 一致；inline review comments 属后续增强，不在本范围。
- 分页仅透传，超大 PR 由调用方翻页或 tree 兜底。
