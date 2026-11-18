# Spec: Issue #2245 — coding worktree 的 .git 目录只读，写型回合无法同步/提交/推送

> 上游 Spec 见 Issue #2245 评论 id=200369588；本文档为 coding 实现口径，范围对齐 M1/M2，M3 属 grape-skills 仓协同项。

## 目标

写型 coding 会话预置 worktree 的 git 元数据（gitdir/index.lock/FETCH_HEAD/objects）可写，使
`grape git config/fetch/commit/push` 在主 worktree 内直接可用；只读校验型会话在启动时被显式标注并降级，
不再被判为写型失败。

## 现状与缺陷

- linked worktree 的 gitdir 默认位于共享基线卷 `{WORKSPACE_DIR}/{provider}/{owner}/{repo}/codebase/worktrees/{agent_type}`。
- 当该卷以只读方式提供（复现场景：`.../grape-skills/codebase/worktrees/coding27`）时，`git config/fetch/commit/push`
  全部返回 `Read-only file system`；会话只能在别处重新 clone（约 77MB）。
- `repogit.ensure_issue_worktree` 复用既有 worktree 时只检查 `.git` 是否存在，不探测写权限，也没有降级标注。

## 范围

### In Scope（本仓）

1. M1：worktree git 元数据不可写时，自动迁移到会话可写卷（不复制对象，避免重复 clone）。
2. M2：会话启动自检 git 写权限（探测 `.git/index.lock` 写入），不可写时注入只读标记与可操作提示。
3. 测试覆盖：探测、迁移后 commit/fetch 可用、只读降级标注。

### Out of Scope

1. M3 coding-runbook 只读/写型分支（grape-skills 仓资产；本仓仅提供 `GRAPE_GIT_WRITABLE` / `GRAPE_GIT_READONLY` 判据）。
2. 同会话 `GRAPE_SESSION_TOKEN` 为空问题（另见 #5102）。

## 验收标准

- [ ] 预置 worktree 的 `.git` 指向只读基线卷时，`config/fetch/commit/push` 在会话内正常执行。
- [ ] 只读场景在会话启动时输出只读判定与可操作提示，env 暴露 `GRAPE_GIT_WRITABLE=false`。
- [ ] 迁移不复制对象库（`objects/info/alternates` 指向共享基线），无需重复 clone（约 77MB）。
- [ ] 现有测试通过，新增测试覆盖探测/迁移/降级。
