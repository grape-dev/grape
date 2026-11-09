# Spec — Issue #1579 note_hook_pr 事件未注入 PR_NUMBER

> 来源：Issue #1579 Spec（issue agent 已确认，评级 ready）｜目标分支：weekly-mys

## 背景与根因

`note_hook_pr`（PR 评论）触发的 Agent 任务 `env_extra` 仅注入 `ISSUE_NUMBER`，未注入
`PR_NUMBER`；而 `a2a_event` 路径会注入。QA 复审只能从事件标题猜测 PR 号，存在取错号风险
（复现：events seq 59972 / task t_2da97c2d28cf / issue #1412 / PR #1011）。

根因：`_inject_issue_pr_env` 的 PR 号来源未覆盖 Note Hook 形态——PR 评论 payload 的
`object_attributes.noteable_iid` 未被读取，payload 缺失时也无结构化兜底；
`object_attributes.iid` 在 Note Hook 中是评论自身 id，不能当作 PR 号。

> QA 复审修正：真实 `note_hook_pr` 事件标题取自 MR 标题（如 `fix: #1412 ...`），
> 不含 `note_hook_pr #<N>` 标记，标题猜号分支生产不可达。方案改为从
> `object_attributes.url` 等结构化 URL（.../merge_requests/<N>）兜底，并移除标题猜号。

## 需求

1. `note_hook_pr` 入队构造 agent_tasks 时解析真实 PR 号并注入 `env_extra.PR_NUMBER`：
   payload 优先（`pr_number` / 订阅号 / `merge_request` / `pull_request` / `noteable_iid`），
   payload 缺失时回退结构化 URL（`object_attributes.url` / `merge_request`、`pull_request`
   的 `web_url`，形如 .../merge_requests/<N>、.../pulls/<N>）。
2. 仅消费结构化字段，不从事件标题猜号；标题格式不作为解析来源。
3. 解析失败注入显式标记 `PR_NUMBER_PARSE_FAILED=1` 并输出告警日志，禁止用
   `ISSUE_NUMBER` 顶替。
4. 不改动 `a2a_event` 现有注入逻辑，仅对齐 note_hook_pr 口径；PR 号解析链统一到
   单一 helper，避免 note_hook_pr 与普通事件优先级分叉。
5. qa-cli runbook Step A0 不得声明可从 `note_hook_pr #<N>` 标题提取 PR 号，属
   grape-skills 仓跨仓协同项，在本单 PR 与 Issue 评论中同步说明。

## 验收标准

- `note_hook_pr` 且 payload 携带 PR 号（noteable_iid / merge_request）→ `PR_NUMBER` 注入。
- payload 无 PR 字段、结构化 URL 含 .../merge_requests/<N> → 注入。
- 事件标题为 MR 标题且不含标记 → 不从标题猜号；仍无结构化来源则标记失败。
- 解析失败 → 无 `PR_NUMBER`、有失败标记与告警，不回落 `ISSUE_NUMBER`。
- `a2a_event` / 普通 Issue 事件注入行为不回归。
- 现有测试通过，新增测试覆盖 payload / URL 兜底 / 标题不猜号 / 失败 / 不回归场景。