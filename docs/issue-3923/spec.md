# Issue #3923 实现 Spec：worker 侧建树失败被误归因为 credential_not_configured

> 来源：Issue #3923（Spec 评论 id=203083592，目标分支校正见评论 id=203084354）| 目标分支 `arch-weekly-11-zj` | 里程碑 arch-weekly-11

## 目标

worker 进程内 `git worktree add` 失败时如实上报 git 原文与失败阶段，不再被渲染成
`credential_not_configured`；裸仓 HEAD 非法指向纳入校验与自愈，使真实 git 错误可见、可恢复。

## 现象与实测链路

1. `repogit worktree add failed <repo_key>: fatal: HEAD not found below refs/heads!`
2. `repogit worktree add non-readonly failure, refuse degrade`
3. 归因阶段 `resolve_exec_token(<repo_key>)` 命中 `vcs_cards`/`vcs_provider` 端口，该端口在
   worker 进程未绑（worker MANIFEST 不含 `src/api/contracts_wiring.py`）
4. 归因取空 → 默认落 `not_configured` → 最终错误文案「该仓库未配置任何可用凭据……

## 根因（两个独立缺陷叠加）

1. **worker 做不了凭据归因**：worker 无装配层，端口未绑；旧码把「归因为空」当默认值
   `not_configured`，于是 worker 上任何 git 失败都被写成「仓库没配凭据」。
2. **裸仓 HEAD 无校验/自愈**：`codebase/HEAD` 被写成 `ref: refs/remotes/origin/<baseline>`
   后，每次 `git worktree add` 必报 `fatal: HEAD not found below refs/heads!`；
   `ensure_repo_base`/`ensure_issue_worktree` 不校验、失败只 refuse degrade。
3. 附带：错误文案把猜测写成结论（「真因在凭据，不在 git 目录权限」）。

## 范围

### In Scope
- 归因三态：确实无凭据（`no_credential`）/ 归因不可用（`unavailable`，端口未绑）/
  未归因（`unattributed`，归因异常），错误与日志可区分。
- `ensure_repo_base` 在 fetch 后校验裸仓 HEAD 必须在 `refs/heads/*`，非法即自愈并记录修复日志。
- `ensure_issue_worktree` 的 `worktree add` 失败命中 HEAD 断裂时，先自愈 HEAD 再重试一次。
- 建树失败透传 git 原文与失败阶段；删除猜测性文案。

### Out of Scope
- 现场 worker 上已手工执行的 HEAD 止血（提交人已完成）。
- 相邻独立缺陷 #3713（取凭据双链口径）不重复处理。
- 不新增凭据发放能力，不改 worker token 注入链路。

## 验收标准

- [x] 构造 base HEAD 指向 `refs/remotes/*`，平台自愈并成功建树，日志含 `repogit head repaired`。
- [x] worker 上任意非凭据类 git 失败（HEAD 断裂、ref 冲突）只反映真实 git 错误，不出现
      `credential_not_configured`。
- [x] 反向回归：确实无凭据的仓库仍报 `credential_not_configured`。
- [x] 「归因不可用」与「无凭据」在事件错误与日志中可区分。
- [x] 新增测试覆盖 HEAD 自愈与三态归因；相关既有测试全绿。

## 风险与对策

| 风险 | 对策 |
|------|------|
| HEAD 自愈误改正常仓库 | 仅在 HEAD 不在 `refs/heads/*`（或该 ref 不存在）时触发；合法时原样返回 |
| 归因语义变更影响既有消费面 | `TokenResult` 新增 `attribution` 字段，不改既有 `stage`/`token` 语义 |
| 自愈重试放大失败耗时 | 仅重试一次，失败即返回；仅 HEAD 类 stderr 触发 |
| 失败现场随任务数膨胀 | 按 `(repo, issue, agent)` 记账并设上限（200），只保留最近失败 |
