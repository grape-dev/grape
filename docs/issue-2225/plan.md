# Plan: Issue #2225 实现方案

## 改动清单

1. `src/domains/runtime/engine/session_tmpdir.py`（新增）
   - `inject_session_tmpdir_env(env, task_id)`：平台侧只下发路径（`/tmp/opencode/{task_id}`），不创建目录。
   - `ensure_session_tmpdir(env, task_id)`：worker 侧创建/校验目录；平台变量缺失或不可写时回退
     `tempfile.mkdtemp(prefix="opencode-{task_id}-")` 并 `log.warning`，同时回填 `TASK_TMPDIR`/`SESSION_DIR`。
   - `cleanup_session_tmpdir(env)`：会话结束只删除自身会话子目录（白名单根校验，禁止误删根目录）。
2. `src/domains/runtime/engine/service.py`
   - `enqueue_task` 生成 `task_id` 后调用 `inject_session_tmpdir_env`，随 `env_extra` 入 `agent_tasks`。
3. `src/domains/runtime/engine/direct_executor.py`
   - direct 路径在签发会话 token 后创建并注入会话暂存目录（与 worker 同源 helper）。
4. `src/domains/runtime/engine/agent.py`
   - `_execute_task_impl` 构建 env 后调用 `ensure_session_tmpdir`，保证 agent 进程可见可写。
   - `_cleanup_task_workdir` 调用 `cleanup_session_tmpdir`，覆盖成功/异常/凭据吊销全部路径。
5. `src/domains/runtime/engine/cleanup.py`
   - `_sweep_tmp_residuals` 增加 `/tmp/opencode` 治理：平铺固定名文件按 TTL 清理；会话子目录按
     独立 TTL（`SWEEPER_TASK_TMPDIR_TTL_SECONDS`，默认 7200s）兜底清理，避免活跃会话被误删。
6. `src/domains/runtime/vcs/release_guard.py`（新增）
   - `resolve_session_task_issue(user)`：仅 `tk_` 会话 token，按 `session_key=task_id` 查
     `agent_tasks.issue_id`；chat/伪 id/查不到 → 返回 None（fail-open，不阻断）。
   - `check_issue_consistency(...)`：正文首个显式 `Issue #N` 锚点 / PR 标题首个 `#N` /
     `related_issue_nums` 与当前任务 Issue 不一致 → 拒绝；`allow_cross_issue` 显式放行。
7. `src/api/git_repo_api.py`
   - `update_issue`（有 body 写入时）、`create_pull`、`update_pull` 在调用上游前执行断言；
     不一致抛 409（CLI 非零退出），不产生线上写入。新增 `allow_cross_issue` query 显式覆盖。

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 平台是否创建目录 | 否（只下发路径） | worker 可能在远端，服务端建目录只会留下本地空目录 |
| 变量缺失策略 | `mkdtemp` + 告警 | 兼容旧 worker，不静默退回固定名 |
| 断言范围 | Issue 正文写入 + PR 标题/正文；评论不拦截 | 评论天然跨 Issue 引用，避免误伤 |
| 断言判定 | 首个显式 `Issue #N` 锚点 | 只锚定正文主语，后续引用/关联不受影响 |
| 存量清理 | TTL 兜底（2h）+ 会话结束即时清理 | 防泄漏与长期残留，避免误删活跃会话 |

## 验证

- `python3 -m pytest tests/runtime/test_issue_2225_session_tmpdir.py`
- `python3 -m pytest tests/runtime/test_issue_2225_release_guard.py`
- `python3 -m pytest tests/mgmt/test_git_repo_api.py -k "issues_update or pulls_create"`