# Tasks: Issue #2245

- [x] T1 `repogit` 新增 gitdir 解析 / 写权限实写探测（`index.lock`）。
- [x] T2 `repogit.ensure_worktree_git_writable`：只读 gitdir 迁移到 `<worktree>/.git-session`（`clone --shared`，alternates 共享对象）。
- [x] T3 `ensure_issue_worktree` 接入：只读基线跳过 fetch、复用/新建后自检、`worktree add` 失败降级建树。
- [x] T4 M2 `inject_git_write_context`：env 标记（`GRAPE_GIT_WRITABLE`/`GRAPE_GIT_READONLY`）+ 只读降级提示块。
- [x] T5 调用点接入：worker / direct / legacy direct 三条建树路径。
- [x] T6 测试：探测、只读迁移后 commit/fetch 可用、只读降级标注、ensure 自动修复。
- [x] T7 文档与自审查、pre-pr 增量检查。
- [ ] T8（协同，不阻塞）grape-skills `coding-runbook` 依据 `GRAPE_GIT_WRITABLE` 区分只读校验会话与写型会话（M3）。

## QA 审查修复轮（PR #1309）

- [x] R1（Required）`worktree add` 失败降级收紧：仅只读挂载/权限类错误才走会话 gitdir 降级建树，其它失败（如分支已被占用）直接失败，避免同分支双工作树。
- [x] R2（Required）写权限探测覆盖共享 common dir：admin gitdir 可写但 common 的 `objects`/`refs` 只读时同样迁移到会话可写 gitdir。
- [x] R3（Required）迁移后 HEAD 保真：ref 分支与 detached 均固定到原 HEAD，`reset --mixed` 前后校验一致，不一致回滚 `.git` 指针。
- [x] R4（W1）`tests/runtime/test_issue_2245_git_writable.py` 接入 `windows-smoke.yml` linux-backend 门禁。
- [x] R5 新增回归：common 只读迁移、detached HEAD 保真、非只读 `worktree add` 失败不降级。
