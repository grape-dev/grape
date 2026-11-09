# Plan: Issue #621

## 任务拆解
1. [x] 定位契约源：`src/api/git_repo_api.py` `IssueUpdateBody.state`（openapi.json 由 FastAPI 生成，无静态副本）。
2. [x] schema 收紧：description + enum ["reopen","close"] + 字段示例 "reopen" + 模型级请求示例 {"state":"reopen"}。
3. [x] 文档同步：`help/api/10-git-repos.md`（PATCH issues/{number} 的 state 行说明 + 速查示例改为 {"state":"reopen","labels":"bug"}）。
4. [x] 测试：`tests/test_issue_621_state_contract.py`（契约断言 + 运行时不变断言）。

## 验证
- `python3 -m pytest tests/test_issue_621_state_contract.py -q` → 2 passed。
- `tests/test_git_repo_api.py` 中 test_issues_crud_mapping 失败为基线既有失败（stash 验证与本次改动无关）。

## 风险
- enum 仅落在 schema（json_schema_extra），不改运行时校验 → 零行为风险。
- CLI 0.3.3 的 Body fields 行只渲染 JSON type（enum/description 不显示），help 完整展示依赖 CLI 后续版本；契约层修正已就位。
