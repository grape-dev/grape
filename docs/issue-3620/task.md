# Issue #3620 — 任务拆解（task）

| # | 任务 | 状态 | 产物/验证 |
|---|------|------|-----------|
| 1 | `_repo_platform_token` 改用 `repo_session` 能力位判定 | done | `src/domains/mgmt/routes/repos.py` |
| 2 | tko_ 仓级严格校验 + 接管会话 active 二次校验 | done | `_ensure_takeover_repo_scope` |
| 3 | auth_token.py 方式 C 能力位语义注释对齐 | done | `src/kernel/auth_sdk/auth_token.py` |
| 4 | 三凭据边界 + tko_ 正向/越权回归测试 | done | `tests/mgmt/test_issue_3620_takeover_git_credential.py`（9 用例） |
| 5 | 相关存量回归 | done | `test_auth_caps` / `test_repo_token_getters_provider_keyed` 全绿 |
| 6 | 自审查 + 敏感信息增量扫描 | 执行中 | pre-pr-release-check |
| 7 | 建 PR + Issue 评论 + A2A QA | pending | PR → weekly-mys |