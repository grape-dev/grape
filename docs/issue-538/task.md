# Task: Issue #538

| # | 任务 | 状态 |
|---|------|------|
| 1 | `list_member_repos`：502/_stale 分支收紧为 `failed_providers` 非空才触发，合法空结果落库空列表并返回 200 | ✅ |
| 2 | `sync_repos`：同上，合法空结果返回 `{"repos": [], "message": "同步成功"}` | ✅ |
| 3 | 新增 `tests/test_member_empty_repos.py`：member 空结果 200 / sync 空结果 200 / member 失败 502 / sync 失败 502 | ✅ |
| 4 | 前端确认：`d.repos || []` + `renderRepoSelection` 空态已天然覆盖，无改动 | ✅ |
| 5 | 回归：test_member_partial_reauth / test_lobby_managed_repos / test_member_probe_deferred / test_repos_host_provider / test_repos_remove_cleanup 全部通过 | ✅ |

## 验证结果
- `python3 -m pytest tests/test_member_empty_repos.py tests/test_member_partial_reauth.py -q` → 5 passed
- `python3 -m pytest tests/test_lobby_managed_repos.py tests/test_member_probe_deferred.py tests/test_repos_host_provider.py tests/test_repos_remove_cleanup.py -q` → 32 passed
