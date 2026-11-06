# Task: Issue #990 — 会话 token 快照一致性

- [x] T1 `src/api/auth_token.py`：`rotate_session_token()` + `SESSION_TOKEN_RECOVERY_HINT`
- [x] T2 `src/runtime/service.py`：`sync_task_session_token()` 复用/轮换/回写快照
- [x] T3 `src/api/routes/runtime.py`：`runtime_task_env` 返回同步 token
- [x] T4 `src/runtime/direct_executor.py`：复用快照 token，消除双签发漂移
- [x] T5 `src/api/deps.py`：`SESSION_TOKEN_INVALID` 401 恢复指引
- [x] T6 新增 `tests/test_issue_990_session_token_snapshot.py`
- [x] T7 现有相关测试 + 全量回归