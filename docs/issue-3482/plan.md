# Plan: Issue #3482 already exists 恢复分支

## 改动落点

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/domains/runtime/engine/repogit.py` | 新增 `_is_already_exists_collision()`、`_merge_collided_assets()`、`_recover_worktree_already_exists()`；`ensure_issue_worktree` worktree add 失败分支在只读特征匹配前插入 already exists 识别：迁移占用目录（保留 `.skills` 产物）→ 重放 worktree add → 合并回产物；失败回滚并改走 `_create_worktree_with_session_gitdir`（clone --shared 会话级 gitdir）兜底 |
| 2 | `tests/runtime/test_repogit.py` | 新增 `TestWorktreeAlreadyExistsRecovery`：①预置 `.skills`（无 .git）→ 建树成功、产物保留、可 commit、`inject_git_write_context` 不置只读；②迁移失败 → 会话级 gitdir 兜底成功、产物保留、gitdir 可解析；③错误特征匹配单测 |

## 关键设计

- 恢复分支位于共享函数 `ensure_issue_worktree`，direct（direct_executor L438）与 worker（agent._execute_task L2960）双路径同源收敛，行为一致。
- 触发判据：`already exists` in stderr 且目标目录存在且无 `.git`（`_has_git=False`）——目录含 `.git` 的场景仍走原闸门/拒绝逻辑。
- 迁移 = 同父级原子 `os.rename` 到 `<issue_dir>.grape-collide-backup`；重放成功后逐条目合并回（同名以重建基线为准，`.git` 不迁）；任一步失败回滚 rename，兜底会话级 gitdir（目标目录原样保留产物，`.git` 指针指向 `.git-session`，gitdir 可解析）。
- `inject_git_write_context` 无需改动：恢复后 `.git` 落位即可解析 gitdir，`GRAPE_GIT_READONLY` 不再被置。
- 兜底失败才返回 None（维持 refuse degrade 后果），不静默。

## 测试

- `python3 -m pytest tests/runtime/test_repogit.py -x -q`（新增用例 + 全部既有用例）
- `python3 -m pytest tests/runtime/ -q`（回归范围）
- 编译检查：`python3 -m py_compile src/domains/runtime/engine/repogit.py`
