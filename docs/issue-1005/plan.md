# Plan: Issue #1005 实现方案

## 改动清单
1. `src/api/git_repo_api.py`
   - `_normalize_milestone_states`：补 `id`（`_milestone_id_from_url` 取 url 尾部数字），保留 state 归一化。
   - `_fetch_gitcode_milestones` / `_resolve_gitcode_milestone`：分页取全量里程碑，按 **id 优先、number 兜底** 解析 `milestone_id`。
   - `update_issue`：GitCode 且含 `milestone_id` 时先解析；解析失败 400，列表/写接口 403 原样透传；
     PATCH 成功后按解析出的 number 回读，一致则回显（补 id），不一致返回 **422「上游忽略」**（不再 502）。
   - `get_issue`：回显 milestone 并补 id。
   - 端点描述与 `IssueUpdateBody.milestone_id` 描述同步取值口径与错误语义（OpenAPI 契约）。
2. `tests/test_issue_640_milestone_contract.py`：适配新语义（400 不存在 / 422 上游忽略 / 回显补 id）。
3. `tests/test_issue_1005_milestone_resolution.py`：新增 id 优先、number 兼容、403 透传、get 回显、列表补 id、
   OpenAPI 描述断言。
4. `help/api/10-git-repos.md`、`docs/openapi/10-git-repos.md`：里程碑取值口径、id 来源、错误码说明。

## 设计决策
- 不在 PATCH 前额外请求，除非请求含 `milestone_id`；分页取 `state=all&per_page=100` 直至不足一页。
- 写上游仍使用 `number`（沿用 #640 真机验证的 `milestone` 参数语义），以解析结果保证 id 入参可用。
- 错误码分流依据：解析阶段 400/403；写后回读不一致 422。代理自身故障（网络/token）仍保留 5xx。

## 验证
- `python3 -m pytest tests/test_issue_640_milestone_contract.py tests/test_issue_1005_milestone_resolution.py`
- `python3 -m pytest tests/test_git_repo_api.py -k milestone`
- 已知存量失败 `test_issues_crud_mapping`（grape-auto-comment 标记断言）与本改动无关。