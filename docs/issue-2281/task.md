# Tasks: Issue #2281

- [x] T1 repogit：bundle 选择 + fetch/reset 找回（`latest_archived_bundle` /
      `restore_worktree_from_bundle`）
- [x] T2 ensure_issue_worktree：分支 ref 缺失时自动找回，复用路径保持零改动
- [x] T3 worktree_guard：`mark_worktree_restored` + inject 区分 auto_restored
- [x] T4 inject_reset_context：push 重试入口（未找回 / 已找回两种提示）
- [x] T5 cleanup：`_sweep_base_branch` 三条 sweeper 入口对齐场景基线
- [x] T6 cleanup：`sweep_events_once` 补齐归档守卫与僵尸目录守卫
- [x] T7 测试：复用/找回/无归档/幂等/推送重试/auto_restored/基线优先级
- [x] T8 回归：`tests/test_worktree_reset_archive.py` + repogit 相关用例
- [x] T9 基线收敛：rebase 到 `arch-weekly-11-zj` tip `abd6278eb`（见下方记录）

## 基线收敛记录（2026-11-16）

- **基线**：`origin/arch-weekly-11-zj` tip `abd6278eb`（含 #2245 worktree 写权限修复、
  #2041 归档守卫、#827 等提交）。上一轮 PR head `ef6565e4f` 的 merge-base 为
  `727a1245`，属基线漂移。
- **方式**：只把本 Issue 的提交 `ef6565e4f` 重放到新 tip（`rebase --onto` 语义）：
  分支历史中 37 条提交已在上游以等价补丁合入（`git cherry` 判定为 `-`），另 3 条
  （#2133 readd 注册 / 前端缓存版本 / PR #1306 审查轮修复）内容已随已合入的 PR
  #1306 进入基线，均不重复重放，避免把他人改动带进本 PR。
- **冲突**：`src/domains/runtime/engine/cleanup.py` 一处——基线把
  `sweep_events_loop` 的清理调用改成 `await _aio.to_thread(repogit.cleanup_issue_worktree, ...)`
  （事件循环不可阻塞），本 Issue 改动是给同一次调用传 `base_branch=`。解决：保留
  基线的线程池调用形态，并把 `base_branch=_sweep_base_branch(repo_key)` 传入该调用，
  两边语义同时成立。
- **兼容性**：#2245 的 worktree 写权限修复（只读 gitdir 迁移、`worktree add` 门禁、
  detached HEAD 保真）未回退；`tests/runtime/test_issue_2245_git_writable.py` 全绿。
- **回归**：`tests/test_worktree_reset_archive.py` 49 passed（新增 7 个用例）；
  CI 门禁相关用例集（#2028/#2041/#2245/#2133/#1982）与基线同口径对比，失败集合完全
  一致（12 项，均为本机环境的既有失败），无新增失败。