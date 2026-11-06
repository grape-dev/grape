# Issue #993 方案设计

## 模块与职责

| 模块 | 职责 |
|------|------|
| `src/runtime/branch_guard.py` | 分支级写租约（repo_key+head_branch，复用 `distributed_locks`）、远端 sha 记录、push 前置判定、pre-push 钩子安装/入口 |
| `src/runtime/repogit.py` | 建/复用 worktree 时安装 pre-push 钩子 |
| `src/runtime/agent.py` | `_submit_linked_repo_prs` 在 stage/commit/push 前接入租约与 push 前置守卫 |
| `src/api/merge_gate.py` | 合入门禁新增重建文档结构扫描（围栏配平/`pr_number: 0`/元数据口径矛盾） |

## 关键流程

1. **写租约**：`branch_guard:<repo_key>:<head_branch>` → `src.lock.acquire_lock`；同 holder 用 `renew_lock` 重入；争用失败返回“分支被 task X 占用”且不产生重建提交。
2. **push 守卫**：
   - PR 已 merged/closed → `pr_merged`/`pr_closed` fail-closed，提示从基线新建分支/PR；
   - 远端 head 与租约记录的 `expected_remote_sha` 不一致 → `remote_drift` fail-closed，提示 fetch/rebase 重新解冲突；
   - PR/远端查询异常 → fail-open + warning。
3. **pre-push 钩子**：每个 worktree 的 hooks 目录安装；钩子从 stdin 取 `remote_sha`，无 VCS token 时通过 `grape git-pulls` 查 PR 状态；覆盖 Agent 手工 push。
4. **结构校验**：新增行扫描 `pr_number: 0`、同键多值（status/state/pr_number 等）→ 硬阻断；代码围栏奇数 → 软告警；结果与既有冲突标记命中合并写入 `grape_t_merge_gate_log`。

## 一致性/失败语义

- 租约 TTL 30 分钟，异常路径释放；租约状态文件保存 `expected_remote_sha`，不写 token。
- 已合并/漂移优先阻断；查询异常不阻断正常交付。
- 钩子安装失败仅告警，不阻断 worktree 创建。