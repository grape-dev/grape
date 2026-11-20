# Spec: Issue #2281 — 会话恢复后 worktree 本地提交丢失，需全量重实现

> 开发基准分支：arch-weekly-11-zj（场景注入权威基线；前序 Spec 记录的
> arch-weekly-11 为上一流程基线，本实现以 arch-weekly-11-zj 为准）

## Objective
补齐「sweeper 清理 → worktree 重建」链路的未推送 commit 保护闭环：删除前归档
（#2041 已落地）、**重建时自动找回**、**推送重试入口**、下一轮交接上下文告知
（#2041 已落地并扩展 auto_restored 语义）。

## 现状与残留缺口
- `cleanup_issue_worktree` 已过 `worktree_guard.archive_worktree_before_reset`
  （未推送/脏 → bundle + 补丁 + untracked 快照，归档失败保留现场）。
- `sweep_once` / `sweep_events_loop` 已过该守卫，但未传 `base_branch`，未推送判定
  回退到 `origin/main`，与场景基线 `arch-weekly-11-zj` 不一致。
- `sweep_events_once`（拆分后的第三条 sweeper 入口）仍直接调用
  `cleanup_issue_worktree` 且僵尸目录 `rmtree` 无守卫，绕过保全语义。
- `ensure_issue_worktree` 重建时仅「bare mirror 有分支 ref 才复用」；分支 ref 已
  丢失（重克隆/被清理）时不读归档 bundle，上一轮 commit 无法找回。
- 重置记录只有「需手工 `git fetch && reset`」一种告知，缺少「push 被阻断时先
  重试 push」的恢复入口。

## 期望行为
1. **重建找回**：`ensure_issue_worktree` 新建 worktree 后，若 `feat/issue-N`
   分支 ref 不在 bare mirror，从该 Issue 最近归档 bundle 执行
   `verify → fetch → reset --hard FETCH_HEAD`；无归档/恢复失败仅告警，仍返回
   可用基线 worktree（不阻断建树）。
2. **分支复用**：分支 ref 仍在时直接复用（`worktree add --force <branch>`），
   保留上一轮未推送 commit，不做多余 reset。
3. **推送重试通道**：`inject_reset_context` 提示块显式给出
   `git push origin HEAD:refs/heads/feat/issue-N`；已自动找回（auto_restored）
   时声明「commit 已恢复、只需重试 push、不要重实现」，未找回时保留
   `git fetch <bundle> && git reset --hard FETCH_HEAD` 手工恢复入口与
   「本轮有改动时禁止 reset」前置条件。
4. **清理判据对齐**：sweeper 各入口传场景基线作为未推送对比 ref；僵尸目录删除
   同样过 `guard_issue_workdir_removal`；无未推送 commit 时行为不变
   （不产 bundle、不产记录），「宁可残留、不可误删」语义不变。

## 验收标准
- [x] 分支 ref 仍在 → 新一轮 ensure 复用，`git log origin/<base>..HEAD` 含原
      commit、`git status` 干净。
- [x] 分支 ref 丢失 + 有归档 bundle → 新一轮 ensure 从 bundle 恢复原 commit，
      记录写入 `auto_restored=true` / `auto_restored_head`。
- [x] 无未推送 commit → 不产 bundle/记录，清理与重建行为不变。
- [x] 重置上下文含 `worktree_reset=true`、丢失 commit 列表、bundle 路径，并提供
      push 重试命令；自动找回时不再引导手工 reset。
- [x] 现有测试通过；新增测试覆盖上述路径。