# Plan: Issue #707

## 方案
服务端入口前置校验 + 翻译层值映射收敛，双 provider 一致。

## 改动
1. `src/vcs/providers/gitcode.py`：`_UPDATE_STATE = {"open": "reopen", "opened": "reopen", "reopen": "reopen", "closed": "close", "close": "close"}`
2. `src/vcs/providers/github.py`：`_UPDATE_STATE = {"open": "open", "opened": "open", "reopen": "open", "closed": "closed", "close": "closed"}`
3. `src/api/git_repo_api.py update_issue`：state 非空 → `strip().lower()` 归一 → 不在 `{"open","opened","reopen","closed","close"}` 报 400（message 以 `'state':` 开头、列出 reopen/close 及别名）；归一后的值进入 neutral（保证翻译必命中，杜绝大小写旁路透传上游）。
4. `IssueUpdateBody.state` description 补充别名映射说明。
5. 新增 `tests/test_issue_707_state_error_copy.py`：翻译映射断言（双 provider）+ API 级 400 文案断言（无 state_event）+ state=open 翻译为 reopen 的请求体断言。

## 测试
`python3 -m pytest tests/test_issue_707_state_error_copy.py tests/test_issue_621_state_contract.py tests/test_git_repo_api.py tests/test_git_provider.py -q`
