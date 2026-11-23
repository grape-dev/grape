# Spec: Issue #2251 — 子任务完成判定与组织流水线闭环

> 权威 Spec 见 Issue #2251 评论（Issue Agent 发布）。本文件是 Coding 阶段落地的需求摘要，
> 用于 PR 内自解释；口径与 Issue Spec 一致。

## Objective

为组织需求下派（`org_dispatch`）补齐「`done_when` 完成判定 + `pending_review → needs_review`
终态 + sweeper 幂等触发聚合 + 组织 Issue 收口」闭环，消除子任务跑完 QA 停在 `pending_review`
时 target 无限 `running`、聚合等不到终态、2h 后被超时误判 `failed` 的断链。

## Scope

### In

- `org_dispatch_target` 新增 `done_when`（`issue_done` 默认 / `pr_merged` / `task_terminal`），
  迁移幂等，存量 target 回填 `issue_done`。
- `_refresh_targets_from_issues` 识别子 Issue `pending_review` / `awaiting_input`，按 `done_when`
  判定；默认 `issue_done` 下 `pending_review → needs_review` 终态。target/dispatch 状态集合
  新增 `needs_review`。
- dispatch 汇总语义：全 `completed → completed`；有 `needs_review` 无 failed → `needs_review`；
  有 failed → `partial_failed` / `failed`；全 cancelled → `cancelled`。
- sweeper 首次发现 dispatch 进入终态（completed/needs_review/partial_failed/failed/cancelled）
  时，按幂等键 `dispatch_id + terminal_status` 触发一次 `org_aggregate`（`aggregate_recheck`）。
- 组织 Issue 收口：服务端幂等写入聚合评论（结论 + 每个 target 的子 Issue/任务/PR +
  needs_review/failed 清单）并归一组织 Issue 状态；未全部终态绝不提前置 `pending_review`。
- 新增 `tests/integration/test_org_flow_closure.py`。

### Out

- 组织上下文注入（#2235 已取消）。
- 2/3 节点可配置、组织生命周期。
- 前端大改（本仓库无前端，仅状态字段透出）。

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| done_when 默认 | `issue_done` | 兼容存量 target，行为不突变 |
| pending_review | `needs_review` 终态（issue_done/task_terminal 未达任务终态时） | 消除无限 running 与超时误判 |
| pr_merged | 仅 `pull_request_merged` 事件 / 子 Issue 已 completed 视为完成 | 显式等待合入是用户选择 |
| awaiting_input | 一律 `needs_review` | 卡在用户输入，不继续跑子任务 |
| 聚合幂等键 | `org_dispatch_aggregate(dispatch_id, terminal_status)` UNIQUE | 跨进程/重复 sweep 只触发一次 |
| 聚合触发方式 | 持久化 `queued` a2a_event（`aggregate_recheck`），巡检 reconcile 派发 | 进程重启不丢、失败可退避重排 |
| 组织 Issue 收口 | 服务端确定性写评论 + 状态归一 | 不依赖 LLM 节点是否成功 |
| 未终态 | 不写完成评论、不改组织 Issue 状态 | 禁止提前 pending_review |

## Acceptance

- [x] 2 目标子任务结束后 10 分钟内聚合收口（sweeper 60s + 巡检 5min 内派发）。
- [x] `pending_review` 不再导致 target 无限 running；target/dispatch 出现 `needs_review`。
- [x] dispatch 汇总语义正确（completed / needs_review / partial_failed / failed / cancelled）。
- [x] 聚合只触发一次，重复 sweep 幂等（`dispatch_id + terminal_status`）。
- [x] 组织 Issue 有聚合评论 + 正确状态，且未终态不提前 pending_review。
- [x] `done_when=pr_merged / task_terminal` 判定正确。
- [x] `tests/integration/test_org_flow_closure.py` 覆盖各分支，`pytest -m integration` 全绿。
- [ ] 真实组织 + 2 仓库线上冒烟（需真实组织/仓库与 VCS 凭据环境，部署后由 QA/SRE 复核）。