# Plan: Issue #2245 — 实现方案

## 关键决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| gitdir 可写实现 | 调整挂载 rw vs 会话可写 gitdir | 会话可写 gitdir（`<worktree>/.git-session`） | 不改全局只读基线卷，改动收敛到会话卷，风险最小 |
| 对象来源 | 复制对象 vs alternates | `git clone --shared`（objects/info/alternates 指向共享基线） | 不复制对象库，避免约 77MB 重复 clone |
| 只读判定时机 | 启动自检 vs 失败后判定 | 启动自检（`repogit.ensure_worktree_git_writable`） | 前置提示，避免编码中途失败 |
| 判定手段 | `os.access(W_OK)` vs 实写探测 | 实写 `.git/index.lock` | root 场景下 `os.access` 不可靠；实写与 git 行为一致 |

## 实现路径（本仓）

1. `src/domains/runtime/engine/repogit.py`
   - `resolve_gitdir(wt_dir)`：解析 `.git` 目录/指针文件为真实 gitdir。
   - `git_common_dir(gitdir)`：解析 linked worktree 的 `commondir`（共享基线）。
   - `probe_path_writable(path)` / `probe_git_write(gitdir)`：实写探测，优先创建 `index.lock`（已存在他人锁时退化为临时文件，不误删）。
   - `ensure_worktree_git_writable(repo_key, wt_dir, token)`：可写→`native`；不可写→迁移为 `relocated`；工作区也不可写→`readonly` + 可操作提示。
   - `_relocate_gitdir_to_session(...)`：`git clone --shared --bare <common> <wt>/.git-session` → 配置 `core.worktree`/origin →
     切 `.git` 指针 → `git reset --mixed HEAD` 重建索引（不动工作区文件）。
   - `inject_git_write_context(task, env, prompt)`：M2 自检入口，写 `GRAPE_GIT_WRITABLE`/`GRAPE_GIT_READONLY`/
     `GRAPE_GIT_READONLY_REASON`，只读时向 prompt 追加降级提示块。
   - `ensure_issue_worktree`：只读基线跳过 fetch 直接复用；复用/新建后调用 `ensure_worktree_git_writable`；
     `worktree add` 因只读失败时降级用会话可写 gitdir 建树。
2. 调用点：`engine/agent.py`（worker）、`engine/direct_executor.py`（direct）、`pipeline/event_pipeline.py`（legacy direct）
   在建树后调用 `inject_git_write_context`。
3. 测试：`tests/runtime/test_issue_2245_git_writable.py`。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| alternates 依赖共享基线对象 | Med | 基线为长期只读卷，不删除；同时保留远程 origin 可 fetch 重建 |
| 迁移丢失未提交改动 | High | 迁移只切换 gitdir + `reset --mixed`（不触碰工作区）；迁移失败保持只读并提示 |
| 并发 index.lock | Low | 探测遇已存在锁不删除他人锁，退化临时文件探测 |
| 会话卷容量增长 | Med | 仅新对象写入会话卷，基线对象共享 |
