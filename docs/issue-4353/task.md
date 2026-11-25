# Tasks: Issue #4353

## 实现

- [x] T1 repogit：新增三路径判定 `classify_worktree_target()`（absent/legal/residual/conflict）——
      合法判据走 `git_common_dir()`（linked worktree 的 admin dir 无 `objects/`），
      `.git` 指向失效 gitdir → residual，指向别仓 gitdir → conflict
- [x] T2 repogit：`prepare_worktree_target()` — residual 分支先 `git worktree prune`，
      再安全移走（`dispose_occupied_workdir_target` / `_quarantine_worktree_dir`，保留
      `.skills`/`.project-info`），移走后二次 prune（登记仍存在则 add 恒报 already exists）
- [x] T3 repogit：`ensure_issue_worktree()` 用 `prepare_worktree_target()` 替代
      `os.path.isdir(.git)` 单判据；conflict → 记失败现场 + 拒绝降级 + `return None`
- [x] T4 repogit：拒绝降级单一决策点 `mark_degrade_refused()` / `degrade_refused()`
      （三元组隔离 + TTL 900s，建树入口/成功即清），`_DEGRADE_REFUSED`
- [x] T5 repogit：`inject_git_write_context()` 命中拒绝降级 → `mode=conflict` +
      `GRAPE_GIT_WORKSPACE_CONFLICT=true` + `GRAPE_GIT_WORKSPACE_ERROR_CODE`，
      并 pop `GRAPE_GIT_READONLY*`（互斥）
- [x] T6 repogit：`describe_git_write_mode()` 统一日志口径（conflict/missing 不出
      "read-only mode marked"）；`worktree_failure_error()` 附 `error_code`
- [x] T7 repogit：`_recover_worktree_already_exists()` 重放前 prune，支持「目标已被
      前置处置移走」的直重放（不再回滚到不存在的备份）
- [x] T8 direct_executor：建树失败面补 `error_code`（任务错误 + `_fail_event` diag），
      只读自检日志走 `describe_git_write_mode()`，`_task_view` 带 `worktree_dir`/`task_id`
- [x] T9 agent（worker）：同口径补 `error_code` 与统一日志

## 测试

- [x] T10 复用：合法 worktree 二次派发复用成功、不重复 add、`.skills` 不被破坏
- [x] T11 清理：残留目录（无 `.git`）→ prune + 移走 → 建树成功，`.skills` 留证
- [x] T12 清理：**已登记**残留（`.git` 指针被删）→ prune 后重建成功（原 `already exists` 形态）
- [x] T13 真冲突：目标是非目录 / `.git` 属别仓 → `ensure_issue_worktree` 返回 None +
      `degrade_refused().code == WORKSPACE_WORKTREE_CONFLICT`
- [x] T14 互斥：拒绝降级后 `inject_git_write_context` 不置 `GRAPE_GIT_READONLY`，
      日志断言 `refuse degrade` 与 `read-only mode marked` 不同时出现
- [x] T15 跨轮：同名 PR 重派（新 task_id）不再命中残留，标记记录新 task_id
- [x] T16 回归：`tests/runtime` 全量 + `make lint` + `python3 scripts/check-arch.py`

## 交付

- [ ] T17 提交 `feat/issue-4353` → 建 PR（base=`arch-weekly-11-ljt`）→ Issue 评论 → A2A QA