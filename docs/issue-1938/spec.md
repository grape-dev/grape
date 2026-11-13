# Spec: Issue #1938 — 级联收敛补偿不得伪造 Agent 结论（结构化补偿归因 + 真实结论回填）

> 来源：Issue #1938 正文 + Issue 评论 199800704（VOD 需求收敛）+ 评论 199801303（Issue Agent Spec/复核）
> | Coding 整理（目标分支 `arch-weekly-11`，基线 HEAD `73d2b3e0`）

## Objective

任务进入终态、但关联事件仍未收到 Agent 回报时，服务端会做「事件级联收敛补偿」。当前补偿把固定文案
`任务已完成：级联收敛（回报丢失补偿）`（及其 cancelled/failed 变体与 `级联收敛：任务{status}` 拼串兜底）
直接写进 `events.result`，该位置在事件列表 / Issue 处理记录中就是「Agent 答复结论」。结果：

1. 补偿记录被当作 Agent 的真实答复展示，下游无法区分「Agent 没返回结论」与「回报在写入事件时丢失」；
2. 补偿不可归因（哪条路径触发、原始任务终态是什么、事件最后已知状态是什么，全都不落库）；
3. 口径污染：历史 8627 条记录中 29 条答复结论等于该固定串（Issue 正文统计，未含 `timeout_failed` 变体）；
4. `timeout_failed` 变体还存在「超时失败被记成成功」的数据失真（`status='completed'`、`exit_code=0`）。

本需求的目标：**补偿不得伪装成 Agent 结论**——补偿结论结构化落库、显式自述来源、可统计可告警，
并与「Agent 答复结论」口径隔离；有真实结论时优先回填，取不到才写补偿文案。

## Scope

### In Scope
- 事件表新增补偿归因字段（`cascade_reason` / `report_missing` / `triggered_by` /
  `cascade_task_status` / `cascade_prev_event_status`），补偿文案由字段生成，删除硬编码常量与拼串兜底。
- 覆盖全部 `TASK_TERMINAL_STATUSES`（含 `timeout_failed`），纠正事件终态与 `exit_code` 映射。
- 真实结论优先回填：从 `agent_tasks.result_text` 取真实结论，取不到才写补偿文案。
- 展示层标注/过滤：事件列表、事件详情、Issue 处理记录（PR 订阅卡片 / pipeline segments）标注
  「系统补偿，原始回报丢失」，并给出 `is_agent_conclusion` 口径布尔值。
- 可观测：按 agent / 场景统计「回报丢失补偿」次数的 Prometheus 指标 + 仓库维度告警指标与阈值规则。
- 三条触发路径（`update_task_status` 兜底 / `sweeper` / `converge-event` 端点）各配回归用例。

### Out of Scope
- 事件状态机枚举扩展（不为 events.status 新增 `timeout_failed` 枚举值）。
- 存量 29 条历史记录的正文改写（历史数据不动；展示层按结构化字段判定，历史行无字段即按原样展示）。
- 补偿路径的重试/重放策略调整；`event_active_guard` 语义变更。

## 现状（目标分支 `arch-weekly-11` 逐行核对）

| 位置 | 现状 |
|------|------|
| `src/domains/runtime/engine/service.py:2049` `_EVENT_CASCADE_RESULTS` | 固定文案字典，completed/cancelled/failed 三变体硬编码 |
| `service.py:2056` `_cascade_event_terminal()` | 直接用字典取文案；未覆盖终态走 `f"级联收敛：任务{status}"`；else 分支无条件写 `status='completed', exit_code=0` |
| `service.py:2177-2192` `update_task_status` 事务内兜底 | 仅 `if status in _TERMINAL_STATUSES`（completed/failed/cancelled）才触发 → `timeout_failed` 不收敛 |
| `service.py:2103` `converge_event_to_task()` | `_task_status not in _TERMINAL_STATUSES` 直接返回 `task_not_terminal` → `timeout_failed` 任务无法补偿 |
| `src/domains/runtime/engine/scheduler.py:639 / 1384 / 1520` | 均调用 `update_task_status`，走同一兜底（非独立写路径），补偿来源无法区分 |
| `src/domains/runtime/routes/runtime.py:1220` `POST /tasks/{task_id}/converge-event` | 管理端补偿入口，无归因信息回传 |
| `src/api/db_setup.py` events DDL + `_ensure_column` 迁移块 | 无补偿归因字段 |
| `src/domains/runtime/routes/events.py:2394 list_events` / `issues.py` PR 订阅卡片 | 直接回传 `events.result`，无「系统补偿」标注 |
| `src/domains/insight/observability/alerts.py` / `deploy/alerts.yml` | 无「回报丢失补偿」指标与阈值告警 |

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 归因字段 | `cascade_reason`（枚举）/ `report_missing`（0/1）/ `triggered_by`（路径枚举）/ `cascade_task_status` / `cascade_prev_event_status` | 满足「可统计、可告警、可归因」；文案由字段渲染，展示层只读结构化字段 |
| 文案 | 单行可解析：`系统补偿，原始回报丢失｜补偿路径=…｜原始任务终态=…｜最后已知事件状态=…` | 人读明确、机器可解析；不再出现「任务已完成：级联收敛」这类伪装成答复的措辞 |
| `report_missing` 语义 | 补偿发生即 1（原始回报确实丢失）；配套 `cascade_reason` 区分 `report_missing_synthesized`（写补偿文案）与 `report_missing_backfilled`（已回填真实结论） | 「是否丢失」与「是否取到真实结论」是两件事，拆开才不丢信息 |
| 「不计入 Agent 答复」口径 | `is_agent_conclusion = not cascade_reason or cascade_reason == report_missing_backfilled` | 回填的是 Agent 真实结论（只是迟到），可以计入；合成文案必须排除 |
| `timeout_failed` 映射 | 触发集合改为 `TASK_TERMINAL_STATUSES`（四态）；事件终态映射 `timeout_failed → failed`，`exit_code` 恒非 0（无显式 exit_code 时取 -1） | events.status 枚举不含 `timeout_failed`（新增枚举会波及状态机/展示/去重），映射到 failed + 非零退出码既诚实又零扩散；同时补上"超时任务事件永不收敛、守卫靠 TTL 兜底"的既有缺口 |
| 真实结论来源 | `agent_tasks.result_text`（同一事务内同游标读取；补偿入口直接由 task 传入） | Spec/复核指定；`_set_task_late_result`（`service.py:3173`）已把真实结论写入该列 |
| 结构化产出 | 回填文本若为结构化 JSON（含 actions/conclusion）则同步写 `events.structured_output` | pipeline 展示主路径读 `structured_output`，回填后结论位正确 |
| 指标 | Prometheus `grape_event_cascade_compensation_total{agent_type,scene_id,trigger,task_status}` + 告警侧仓库维度 `cascade_compensation` 计数 + 1 条种子阈值规则 + `deploy/alerts.yml` 对应规则 | 前者满足「按 agent/场景」；后者进既有告警中心，复用 `evaluate_rules` 现有 dim 模型 |
| 幂等 | 事件已终态 → no-op（不覆盖、不改字段）；重复补偿调用结果 `noop` | 保持既有语义，不退化 |
| `event_active_guard` | 释放逻辑原样保留（同一事务/游标内 DELETE） | 既有语义不得退化 |

## 字段与枚举

`cascade_reason`（events.cascade_reason，VARCHAR(64)）：
- `report_missing_synthesized`：原始回报丢失，且无真实结论可取 → 写补偿文案（不计入 Agent 答复）
- `report_missing_backfilled`：原始回报丢失，但已从 `agent_tasks.result_text` 回填真实结论（可计入）

`triggered_by`（events.triggered_by，VARCHAR(64)）：
- `update_task_status`：任务状态机事务内兜底
- `sweeper`：定期巡检兜底（`scheduler.py` 三个调用点）
- `converge_event`：管理端补偿端点 `POST /tasks/{task_id}/converge-event`

## Acceptance Criteria（逐条对应 Issue 评论 199800704 / 199801303）

- [ ] 三条补偿路径产生的记录，其 `result` 可区分「系统补偿」与 Agent 答复，并保留原始任务终态与最后已知事件状态
- [ ] 能取到真实结论的任务优先回填真实结论（`report_missing_backfilled`），取不到才写补偿文案
- [ ] 事件/Issue 展示对补偿文案有明确标注或过滤（`is_agent_conclusion=false`），统计口径不再计入 Agent 答复
- [ ] 提供按 agent / 场景统计「回报丢失补偿」次数的可观测指标，并有阈值告警
- [ ] 三条触发路径各有回归用例；`timeout_failed` 变体纳入用例与统计口径（校验事件 status/exit_code 映射）
- [ ] 补偿路径幂等性与既有 `event_active_guard` 释放语义不退化
- [ ] 不再存在 `f"级联收敛：任务{status}"` 拼串兜底

## 风险与兼容

- 新增列走 `_ensure_column`（幂等 + MySQL DDL 安全），`VARCHAR(32/64) + INTEGER` 不用 TEXT，避免 MySQL
  TEXT 默认值限制；老库升级自动补列，读取侧对缺列场景做防御（历史行字段为空 → 视为非补偿）。
- 展示层只新增字段，不改既有字段语义：不改 `result` 正文的消费契约（除补偿文案本身）。
- `timeout_failed` 收敛后事件从「长期活跃」变为终态，A2A 去重不再被阻断，属修复方向（与 #587 语义一致）。