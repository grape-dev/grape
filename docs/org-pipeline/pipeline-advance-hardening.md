# 流水线状态推进硬化：不以技能/收尾钩子为真源

## 背景

节点完成后是否进入下一节点，曾经依赖两条"软"路径：

1. 技能自己执行 `grape a2a handoff`；
2. 任务收尾钩子（远程 `complete_task` / 本地 direct 收尾）按场景 `next/handoff` 推进。

任一路径缺失，Issue 就会停在当前节点（典型：coding 跑完不再进 qa）。实际故障
还包括旧进程接管执行、进程重启、技能未写 handoff、收尾钩子异常等。

## 结论：DB 是唯一真源

状态推进不再信任"agent 说了什么"，只认数据库里的事实：

| 层 | 位置 | 角色 |
|---|---|---|
| ① 硬写入通道 | `POST /api/tasks/{id}/complete`、`/fail`（worker → 服务端） | 任务终态只由服务端落库；技能无需交办 |
| ② 收尾钩子 | `complete_task()`、`event_pipeline._execute_agent_inner`、`direct_executor._execute_agent_inner` | 任务 completed 时按场景自动推进（幂等） |
| ③ 周期对账 | `pipeline_reconcile.reconcile_pipeline_progress()`（Mgmt patrol，leader-gated） | 扫描"任务已完成但 stage 没动"的行，兜底推进 |
| ④ 人工/运营接口 | `POST /api/patrol/pipeline-advance` | 立即跑一轮 ③，排障用 |

组织下派侧同样以 DB 为准：

- `org_dispatch.sweep_once` → `_refresh_targets_from_issues`：子 Issue 状态 → target 终态；
- 批次终态 → `_maybe_trigger_terminal_aggregate` → 聚合事件（queued 持久化，巡检补派）；
- `finalize_org_issue_aggregation`：组织 Issue 评论 + 状态归一（服务端），不依赖技能。

## 对账谓词与守卫

推进条件（全部满足）：

1. `agent_tasks.status='completed'`，`event_seq>0`，`issue_id` 为 `#N`；
2. `completed_at` 在回看窗口内（默认 6h，`PIPELINE_ADVANCE_LOOKBACK_HOURS`）；
3. `issue.stage == agent_type`（stage 已前移 = 已被显式交办/上一轮已推进）；
4. `issue.status ∈ {active, processing, awaiting_input}`（pending_review/completed/cancelled 不碰）；
5. 无在途任务/事件（`has_inflight`）：异步交办尚未推进 stage 的窗口不重复派发。

幂等：

- 推进事件键 `auto:<task_id>:<role>`，`events(repo_key, idempotency_key)` 唯一索引兜底；
- 同进程单飞锁（Mgmt 存在两条 patrol_loop，第二路直接跳过）；
- 同一任务 10 分钟内不重复尝试（`node_not_found` 等场景不刷日志）。

## 失败路径

- 无场景节点 / `handoff != a2a` → 不推进；
- `org_dispatch` 节点：批次未终态时不推进（`wait_for_dispatch`）；
- 末节点：由 `finalize_issue_to_pending_review` 收口；`org_aggregate` 由批次终态触发，
  聚合事件持久化为 queued，由 `reconcile_queued_a2a` 补派；
- 派发失败：事件置 failed / 按退避重排，stage 保持当前节点，不指向未运行的下游。

## 运维

```bash
# 手动跑一轮对账（幂等，可重复）
curl -X POST "$BASE/api/patrol/pipeline-advance" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"limit":200}'
```

返回 `advance.scanned/advanced/actions`；`advanced=0` 表示当前没有"任务完成但
stage 未前进"的存量。周期对账默认每 5 分钟一轮（Mgmt patrol）。