# Plan: Issue #2251 — 子任务完成判定与组织流水线闭环

## 架构决策

- **完成判定内聚在 `org_dispatch._refresh_targets_from_issues`**：target 行携带 `done_when`，
  每次 sweep 读子 Issue `status/stage`、最新 `agent_tasks.status`、`pull_request_merged` 事件，
  统一由 `_terminal_status_for()` 归一到 `completed / needs_review / failed / cancelled`。
  判定函数纯函数化，便于单测；DB 读取集中在 refresh。
- **`needs_review` 是 target/dispatch 终态**：`TARGET_IN_FLIGHT` 不变，超时仅作用于在途
  target；`needs_review` 参与汇总但不再被 `_timeout_targets` 误判。
- **聚合幂等用独立表 `org_dispatch_aggregate`**：`UNIQUE(dispatch_id, terminal_status)`
  是幂等键落库形态，`INSERT OR IGNORE` 抢占成功者才收口；重复 sweep / 多实例并发都只触发一次。
- **触发事件与收口解耦**：
  1. 收口：`issues_center.finalize_org_issue_aggregation()` 幂等写组织 Issue 评论 +
     归一状态（全部 completed → pending_review；needs_review/partial_failed/failed → pending_review；
     cancelled → cancelled；可选 `auto_finalize` → completed）。
  2. 触发：`a2a_dispatch.trigger_org_aggregate()` 持久化一条 `status='queued'` 的
     `a2a_event`（`aggregate_recheck=true`，幂等键 `org_agg:<dispatch_id>:<terminal_status>`），
     由巡检 `reconcile_queued_a2a` 派发；进程重启不丢、失败走既有退避重排。
- **未终态保护**：`finalize_org_issue_aggregation` 先校验全部 target 终态，否则返回
  `{"action":"pending","remaining":[...]}`，不写评论、不改状态；engine 侧既有
  `remaining / next_step=a2a-qa` 重排逻辑继续生效。

## 数据迁移

- `org_dispatch_target.done_when TEXT NOT NULL DEFAULT 'issue_done'`（`_ensure_column` 幂等 + 空值回填）。
- 新表 `org_dispatch_aggregate`（幂等键 + 触发/收口状态 + seq + comment_posted）。
- 迁移版本 `202611120006`（`strict=True`，失败重试而非盖章）。

## 变更清单

| 文件 | 变更 |
|------|------|
| `src/api/db_setup.py` | `done_when` 列 + 回填；`org_dispatch_aggregate` DDL/索引；迁移注册 |
| `src/domains/mgmt/org_dispatch.py` | done_when 常量/判定、refresh 归一、汇总语义、sweeper 终态触发、请求透出 done_when |
| `src/domains/mgmt/routes/org_dispatches.py` | 创建下派支持 `done_when` |
| `src/domains/runtime/engine/service.py` | 聚合节点收口兜底：终态时调用 `finalize_org_issue_aggregation`（best-effort） |
| `src/api/a2a_dispatch.py` | `trigger_org_aggregate()`：幂等持久化聚合重排事件 |
| `src/api/issues_center.py` | `finalize_org_issue_aggregation()`：评论 + 状态归一 |
| `.grape/skills/org/org-aggregate-cli/SKILL.md` | 终态集合加 `needs_review`；评论幂等口径 |
| `tests/integration/test_org_flow_closure.py` | 闭环集成用例 |

## 测试 seam

- **HTTP seam**：复用 `tests/integration/conftest.py::org_e2e`（真实 DB / 真实状态机 / 仅 VCS adapter mock）。
- **sweeper seam**：`org_dispatch.sweep_once()`（真实 refresh/timeout/aggregate/trigger）。
- **状态驱动**：完成走 `confirm_issue_done`，待审走 `_set_issue_pending_review`，任务终态写入
  `agent_tasks`，合入写真实 `pull_request_merged` 事件；不直改 dispatch status、不直调聚合。
- **VCS 评论 seam**：用例 patch `VcsApiClient.post_issue_comment` 捕获评论，断言内容与次数。

## 风险

| 风险 | 缓解 |
|------|------|
| 迁移非幂等 | `_ensure_column` + 空值回填，迁移前后各跑一次集成用例 |
| 未终态提前 pending_review | finalize 前置校验 + 集成用例断言 active 不变 |
| 聚合重复触发 | 唯一索引抢占 + 事件幂等键，双重去重 |
| 已有单测受 sweeper 触发影响 | 触发只写 queued 事件（不进程内派发），不新增外网/异步副作用 |
| pr_merged 等待合入期间又被超时 | 显式 done_when 语义，超时仍为兜底；issue_done 默认路径已消除误判 |