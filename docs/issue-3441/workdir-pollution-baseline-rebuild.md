# 污染分支基线重建操作指引（Issue #3441）

> 适用场景：多个任务曾并发共用同一 workdir（如 `issues/1588/coding` 被另一 Issue 的会话
> checkout 并提交），导致某任务的提交父链混入其它 Issue 的提交，需要把「干净的部分」
> 搬到干净基线上重建分支。
>
> 本例（后台反馈 10233 / Issue #3383 抢救记录）：
> - workdir：`issues/1588/coding`（与任务 Issue #3383 编号不一致，并发争用所致）
> - 污染现场：#3383 的红测试提交 `aa7276f0b` 的父提交混入 #3350 的提交 `b12b39b81`
> - 目标：从干净基线重建 `feat/issue-3383`，仅保留本 Issue 的提交

## 一、确认污染范围

```bash
cd <workdir>                          # 如 /root/grape-workspaces/<provider>/<owner>/<repo>/issues/1588/coding
git reflog --date=iso | head -20       # 找出被其它会话 checkout 的痕迹（如 HEAD@{3,6} 切到 feat/issue-3350）
git log --oneline --graph -20          # 确认本 Issue 提交的父链中混入了哪些外来提交
```

判定「外来提交」的标准：提交 message 中的 Issue 编号、改动文件与当前 Issue 无关，
且其父链来自其它 Issue 的开发分支。

## 二、重建干净基线分支

```bash
# 1. 记录本 Issue 的干净提交清单（从旧到新，剔除混入的外来提交）
#    下例中 aa7276f0b 是本 Issue 的提交，b12b39b81 是混入的 #3350 提交
git log --oneline origin/<基线分支>..feat/issue-3383   # 人工核对哪些 commit 属于本 Issue

# 2. 从远端干净基线新建重建分支
git checkout -b feat/issue-3383-rebuild origin/<基线分支>

# 3. 按原顺序 cherry-pick 本 Issue 的提交（只挑自己的，跳过外来提交）
git cherry-pick aa7276f0b
#    如遇冲突：解决后 git cherry-pick --continue；确属外来改动则 git cherry-pick --skip

# 4. 验证重建结果：提交链上不再含外来 Issue 的提交
git log --oneline --graph origin/<基线分支>..feat/issue-3383-rebuild
```

## 三、替换旧分支并推送

```bash
# 1. 核对无误后强推覆盖污染分支（需分支不受保护；受保护则推重建分支并建 PR 时注明替换关系）
git push origin feat/issue-3383-rebuild:feat/issue-3383 --force-with-lease

# 2. 旧污染现场保全：重置/删除 workdir 前服务端会走 worktree_guard 归档闸
#    （archive_worktree_before_reset，见 src/domains/runtime/engine/worktree_guard.py）。
#    如需手工备份，可额外打 bundle：
git bundle create /tmp/issue-3383-polluted-head.bundle HEAD@{1}   # 在覆盖前执行
```

## 四、后续防线（本次已落地的机制）

1. **按 Issue（及 PR）隔离分配 worktree**：任务派发时 workdir 由
   `get_agent_workdir` 派生（同 Issue 复用、跨 Issue 硬隔离）：
   - 有关联 Issue、无 PR：`grape-workspaces/{provider}/{owner}/{repo}/issues/<N>/<agent>`；
   - 有关联 Issue、有 PR（同 Issue 多 PR 隔离，Issue #3375）：
     `grape-workspaces/{provider}/{owner}/{repo}/issues/<N>/pr-<P>/<agent>`；
   - 无关联 Issue 的 PR 事件：`grape-workspaces/{provider}/{owner}/{repo}/prs/<P>/<agent>`
     （该布局不含 Issue 标识，不进入下述启动一致性校验）；
2. **启动一致性校验（fail-fast）**：worker 任务启动入口
   `dependency_check.validate_task_dependencies`（第 6 项）调用
   `worktree_guard.validate_workdir_issue_consistency`，校验 workdir 中的
   `issues/<N>` 标识与任务 Issue 标识一致，不一致立即中止并输出含双方标识的
   显式错误，不静默继续。`issues/<N>/<agent>` 与 `issues/<N>/pr-<P>/<agent>`
   两种含 Issue 标识的布局均受支持（`extract_workdir_issue_num` 与
   `parse_issue_workdir` 复用同一尾部锚定解析）。
   作用范围（#3441 复审收敛）：仅校验**含 `issues/` 段的布局**。既有非 Issue
   目录（autopilot 远端派发的仓库级 `issue-agent`、VOD 的
   `vod/<provider>/<repo_>`）以及合成 Issue 键 `autopilot-*` 一律跳过，
   避免把既有派发链路拦成启动即失败；范围外的错配仍由派发入口的其它校验兜底。
   direct 路径（`direct_executor._execute_agent_inner`）不做该校验：其
   workdir 由本任务 `issue_id` 经 `get_agent_workdir` 同源派生，不存在外部
   错配入口；错配风险统一由 worker 派发入口拦截；
3. 相关根因边界：worktree 静态预置错配见 Issue #3377（彼为配置错配，
   本条为并发争用，两者分别修复）。
