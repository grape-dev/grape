# Plan: Issue #2281 — 重建找回与推送重试通道

## 改动点
| 文件 | 改动 |
|------|------|
| `src/domains/runtime/engine/repogit.py` | 新增 `latest_archived_bundle` / `restore_worktree_from_bundle` / `_mark_auto_restored`；`ensure_issue_worktree` 新建路径在分支 ref 缺失时从 bundle 找回，记录复用分支结果 |
| `src/domains/runtime/engine/worktree_guard.py` | 新增 `mark_worktree_restored`；`inject_reset_context` 区分 auto_restored 与待找回，补充 push 重试入口 |
| `src/domains/runtime/engine/cleanup.py` | 新增 `_sweep_base_branch`（env `GRAPE_WORK_BRANCH` → 场景 DB）；三条 sweeper 入口传 `base_branch`，`sweep_events_once` 补齐归档守卫与僵尸守卫 |
| `tests/test_worktree_reset_archive.py` | 新增 8 个用例覆盖复用/找回/无归档/幂等/推送重试/auto_restored/基线优先级 |

## 关键设计
- **找回只在新建 worktree 上执行**：`ensure_issue_worktree` 复用已有 worktree
  的早退路径不触碰工作区，避免覆盖当轮改动。
- **找回是增强而非前置**：无 bundle/verify 失败/fetch/reset 失败 → 仅
  `log.warning` 并保留基线 worktree；与清理侧「归档失败拒绝删除」形成对称：
  删除侧 fail-closed，重建侧 fail-open（不阻断任务）。
- **bundle 选择**：优先读 `worktree_guard` 最新记录 `bundle_path`，记录缺失时
  扫描 `archive/{issue}/*/worktree.bundle` 取 mtime 最新，兼容记录被消费/删除。
- **auto_restored 语义**：恢复成功后回写记录（保留 lost_commits/bundle_path
  审计），下一轮 prompt 改为「已自动找回，先重试 push」，避免 Agent 重复 reset。
- **基线一致性**：sweeper 与 ensure 同源读取 `GRAPE_WORK_BRANCH`，避免把基线
  分支正常提交误计为未推送导致反复归档/拒绝删除。
- **无风险零副作用**：未推送判定为 0 且工作区干净时，`archive_worktree_before_reset`
  原样返回，不产 bundle/记录（#2041 既有语义保持）。

## 风险与缓解
| 风险 | 缓解 |
|------|------|
| bundle 恢复覆盖当轮改动 | 仅在 ensure 新建的干净基线 worktree 上执行；复用路径不进恢复 |
| bundle 目录膨胀 | 沿用 #2041 归档目录与按 Issue 生命周期清理，本 PR 不新增目录 |
| 恢复失败阻断建树 | 恢复失败仅告警、返回基线 worktree |
| sweeper 基线误判 | 未推送判定本地 ref 链 + 远端 `ls-remote` 复核（#2041 既有） |