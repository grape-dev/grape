# Issue #899 方案设计与任务拆解

## 方案
在 `src/api/git_repo_api.py` `list_commits` 端点增加可选 `sha: str = Query("")` 查询参数：
- 空（缺省）→ 透传 `{"per_page": n}`，URL 不含 sha，行为与现状完全一致（向后兼容）。
- 非空 → 透传 `{"per_page": n, "sha": sha}`，由上游 GitCode/GitHub 按 ref（分支/tag/sha）过滤；ref 不存在时上游返回 404，经既有 `_vcs_req` 异常处理透传（与 #734 语义一致）。
- CLI 为契约驱动（OpenAPI operation `gitrepo_commits`），FastAPI 声明 Query 参数后契约自动带出 `--sha`，无需手改 CLI。

风险：上游对无效 ref 返回 404 → 直接透传，不额外吞错；不影响 tree ref 存在性校验（其内部继续用 `/commits?sha=` 直调 repo_manager，不走本端点签名变化）。

## 任务拆解（TDD）
1. RED：`tests/test_git_repo_api.py` 新增用例——`?sha=weekly-10` 透传 `sha=weekly-10`；缺省不带 sha（兼容回归）。
2. GREEN：`list_commits` 增加 `sha` 参数并透传。
3. REFACTOR/回归：跑 `tests/test_git_repo_api.py`、`tests/test_openapi.py`、`tests/test_git_repo_tree_slash_ref.py`。

## 提交
- `feat: #899 add optional sha ref filter to git-repo commits endpoint`
