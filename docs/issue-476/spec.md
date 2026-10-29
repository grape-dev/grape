# Spec: Issue #476 — 失败终态事件强制携带可诊断 result

> 目标分支: local-liufei | 优先级: P1 | 类型: bug + enhancement（可观测性缺口）

## 需求背景（VOD 收敛 + 前序 Agent 分析）

事件列表出现"无法解释的红"：failed 事件 result 为空 → 无法自助排障。
- dev 单库统计：2738 事件 / 358 失败，result 为空 67 条（18.7%），其中 a2a_event 占 47（70%）。
- 空样本跨 8+ 仓库，最近 5 条连续失败样本均为 `A2A: → sre`。
- 关联前序子问题：#418（派发早退裸置 failed，已收口）、#368（a2a 派发失败裸 update_event_status，已收口）、#369（agent_tasks 判死未回写关联事件，已收口）。

## 根因定位（main 分支源码核查）

1. `src/api/routes/webhook.py _on_handoff_dispatch`（agent/handoff 事件派发 listener，L126-127 / L131-132）
   **两处裸 `update_event_status(seq, "failed")`** —— 只写 status，不落 result / exit_code / completed_at。
   `dispatch_node` 返回 failed 或抛异常时即走此路径 → 最近 `A2A: → sre` 连续失败样本主因（a2a_event 占空样本 70%）。
   #368/#418/#369 已把派发早退/任务判死路径收口到 `_fail_event`/`_sync_event_failed`，**该 listener 路径漏改**。
2. `update_event_status`（event_store.py）仅执行 `UPDATE events SET status=?`，任何调用方传 failed 即产生无头失败。
3. `/api/events/{owner}/{repo}`（list_events）仅返回 7 字段（seq/event_type/agent_type/title/summary/status/created_at），
   **无 result / issue_id / task_id** → 列表页"无法解释的红"且无法跳转关联任务/Issue。
   前端 pipeline.js L414 消费该列表时已按 `e.issue_id` 过滤（实际接口未返回该字段，跳转一直失效）。
4. 命名不一致疑点：事件详情返回 `event_type`（snake_case），`/api/dashboard/ops/timeline` 返回 `eventType`（camelCase）；
   两套前端消费并存；列表接口无 `event` 字段 → 开发期定位后统一（不阻塞主链路）。

## 目标

- 失败终态事件 100% 携带可诊断 result：人可读原因 + 机器可读错误码（result 内嵌 `[code]` 前缀）。
- A2A 类失败额外记录：目标阶段（target_stage=agent/node）、失败环节（phase=dispatch/exception）、引用 seq（ref_seq=源事件 seq）。
- 事件列表接口补 result / issue_id / task_id，前端可渲染失败原因并跳转 Issue / 任务。
- 新增观测口径：跨仓 "failed 且 result 为空" 占比（存量 vs 增量可区分）。
- main / 生产同路径排查结论记录在案。

## 范围

### In Scope
1. `webhook.py _on_handoff_dispatch` 两处裸调用 → 统一走 `_fail_event`（reason 含目标 agent/node + dispatch_node error / 异常信息）。
2. A2A 事件创建/派发时 payload 记录源事件 seq（ref_seq）；`_fail_event` 扩展诊断上下文（code/target_stage/phase/ref_seq）写入 events.payload。
3. `update_event_status` 终态 failed 非空 result 防御（向后兼容；裸调用自动回填 fallback reason + exit_code + completed_at）。
4. `list_events` 补 result 摘要 / issue_id / task_id（LEFT JOIN agent_tasks 取最新 task）。
5. 前端：pipeline.js 列表消费补跳转（issue_id 已可用）；events.js 详情页渲染失败诊断（目标阶段/失败环节/引用 seq）。
6. `src/observability/` 新增跨仓 "failed 且 result 为空" 占比口径 + `/api/obs/events/failed-result-gap` 接口 + 前端看板摘要卡。
7. 存量一次性标记脚本（legacy reason）+ main/生产排查结论记录文档。

### Out of Scope
- 失败自动重试 / 自愈策略。
- events 表结构变更（result 等字段已存在，仅补使用；不新增列）。
- 事件页前端整体重构。

## 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 失败收口入口 | 统一 `_fail_event` | #368/#418 已验证模式，自带幂等 + 终态保护 |
| 机器可读错误码 | result 内嵌 `[code]` 前缀 + payload `_fail_diag` | 免表结构变更，前端可解析，detail 接口即可读 |
| 诊断上下文存储 | events.payload._fail_diag（JSON 合并） | payload 已是 JSON 字段；list/detail 均可透出 |
| update_event_status 防御 | `result/exit_code/completed_at` 回填（非空 result 优先保留） | 向后兼容，兜住全部遗漏裸调用点 |
| 存量数据 | 标记 legacy（result 置 `[legacy]` 前缀 + payload 标记） | 原始失败信息已丢失，标记满足口径区分 |

## 验收标准

- [ ] 事件列表 status=failed 过滤，result 非空率 100%（新增事件；存量已标记 legacy）
- [ ] A2A 类失败样本可读出目标阶段 / 失败环节 / 引用 seq
- [ ] 列表接口含 task/issue 关联标识且前端可跳转
- [ ] "failed 且 result 为空" 占比统计口径 / 看板上线
- [ ] 接口 event 字段映射修复（DB event_type 正常 → 接口非 null；列表接口补 event_type 字段映射说明）
- [ ] main / 生产同路径排查结论记录在案（main 已确认存在，生产待复核）
- [ ] 所有现有测试通过；新增测试覆盖 listener 失败路径与列表接口字段

## 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| update_event_status 收紧影响既有调用方 | Med | 可选参数渐进式 + 防御回填不破坏原 status 语义；全量 grep 调用点核查 |
| 接口加字段消费方兼容 | Med | 仅新增字段，向后兼容 |
| legacy 标记污染统计口径 | Low | 口径显式区分 legacy（result LIKE '[legacy]%'）与真空 |
| dev / main / 生产行为差异 | Med | 排查列为验收项并记录文档 |