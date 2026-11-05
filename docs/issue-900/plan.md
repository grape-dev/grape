# Plan: Issue #900（R2：QA 复审修复版）

背景：PR #905 首版实现已合并又被回退（4c8d1ca68）；QA 复审报告 3 项 Spec 问题（seq=47676）。本版在回退后的 weekly-mys 基线上重新实现并修复三项。

| # | 文件 | 改动 |
|---|------|------|
| 1 | src/api/git_repo_api.py `list_milestones` | `page: Query(None, ge=1)`、`per_page: Query(None, ge=1)`（超 100 钳制到 `_MILESTONES_MAX_PER_PAGE=100`，不再 422）、`title: Query("")`；**仅显式提供时透传上游**（无参 URL 与旧版一致）；description 全英文并注明 Issue #900 |
| 2 | 契约 | FastAPI OpenAPI Query 参数即契约；新增 `test_milestones_openapi_contract_params` 断言 schema 含三参数（QA (b)） |
| 3 | tests/test_git_repo_api.py | 透传 / title 过滤 / 钳制（per_page=500→100 非 422，QA (a)）/ 默认无参不发送 page/per_page（QA (c)）/ 契约断言 / page=0 等 422 |
| 4 | docs/issue-900/{spec,plan,task}.md | 随实现恢复并按 QA 结论修订 |

## 测试
- pytest tests/test_git_repo_api.py -k milestone
- 回归 pytest tests/test_git_repo_api.py tests/test_openapi.py（基线既有失败 test_issues_crud_mapping 与本改动无关）

## 风险
- 上游限流：per_page 钳制 ≤100（常量 `_MILESTONES_MAX_PER_PAGE`）。
- 契约刷新依赖部署侧 `grape refresh`（端到端验收合并后复核，见 spec 开放项）。
