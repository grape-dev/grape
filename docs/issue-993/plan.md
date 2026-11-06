# Issue #993 方案设计

## 模块与职责

| 模块 | 职责 |
|------|------|
| `src/runtime/branch_guard.py` | 分支级写租约（repo_key+head_branch，复用 `distributed_locks`）、远端 sha 记录、push 前置判定、pre-push 钩子安装/入口 |
| `src/runtime/repogit.py` | 建/复用 worktree 时安装 pre-push 钩子 |
| `src/runtime/agent.py` | `_submit_linked_repo_prs` 在 stage/commit/push 前接入租约与 push 前置守卫 |
| `src/api/merge_gate.py` | 合入门禁新增重建文档结构扫描（围栏配平/`pr_number: 0`/元数据口径矛盾） |

## 关键流程

1. **写租约**：`branch_guard:<repo_key>:<head_branch>` → `src.lock.acquire_lock`；同 holder 用 `renew_lock` 重入；租约必须先于 `ensure_feature_branch`（可能写分支）获取；争用失败返回“分支被 task X 占用”且不产生重建提交。
2. **push 守卫**：
   - PR 已 merged/closed → `pr_merged`/`pr_closed` fail-closed，提示从基线新建分支/PR；
   - 远端 head 与租约记录的 `expected_remote_sha` 不一致 → `remote_drift` fail-closed，提示 fetch/rebase 重新解冲突；
   - `expected_remote_sha` 记录本次 push **成功后**的 local sha（钩子用 stdin 的 local_sha），避免同任务第二次合法 push 误报漂移；
   - PR/远端查询异常 → fail-open + warning。
3. **pre-push 钩子**：每个 worktree 的 hooks 目录安装；钩子从 stdin 取 `local_sha`/`remote_sha`，无 VCS token 时通过 `grape git-pulls` 查 PR 状态；守卫提示同时写 git push stderr 与任务日志；覆盖 Agent 手工 push。
4. **自动推送双路径**：worker 端 `_submit_linked_repo_prs` 与 direct 端 `_submit_direct_linked_prs` 都接入租约 + preflight；自身推送用 `--no-verify`（已持租约并做等价检查，避免钩子跨进程 holder 不同导致自锁），成功后记录 HEAD sha。
5. **结构校验**：仅扫描 `docs/` 下 Markdown 重建文档的新增行，且只认行首 `key: value` 元数据（避免正文引用/Python 注解误报）；`pr_number: 0`、同键多值（status/state/pr_number 等）→ 硬阻断；代码围栏奇数 → 软告警；结果与既有冲突标记命中分别归因后写入 `grape_t_merge_gate_log`。

## 一致性/失败语义

- 租约 TTL 30 分钟，异常路径释放；租约状态文件保存 `expected_remote_sha`，不写 token。
- 已合并/漂移优先阻断；查询异常不阻断正常交付。
- 钩子安装失败仅告警，不阻断 worktree 创建。
