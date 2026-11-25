# Spec: Issue #4240 — Agent 事件页统计卡与总量口径对齐

> 目标分支：`weekly-mys` | 缺陷等级：P2 | 场景：运行域-任务编排专用开发流水线

## 背景
Agent/VOD 事件页顶部统计区仅渲染「事件总量 / 已完成 / 进行中」三张卡，失败与取消事件不可见；
总量取 `metrics.total_events`，在仅有失败/取消时会出现「总量 2 / 已完成 0 / 进行中 0」的加不平组合。

## 目标
- 统计区补齐「待处理 / 失败 / 取消」，任意状态组合下状态卡之和等于总量。
- 总量 = 五状态之和，另行标注不受 keyword 影响的原始总数。
- 中英文词条齐全，无 key 回退。
- 事件历史分页/搜索（`events_total`、`events_page`、`events_page_size`）语义不变。

## 范围
In scope：
- `src/domains/mgmt/routes/agents.py`：`event_stats` 固定五状态键；仅回填白名单状态；
  `total_events = sum(event_stats.values())`；`total_events_raw` 由同一份
  `GROUP BY status` 结果求和（与状态卡同源，含未知状态）。
- `src/api/frontend/app/src/views/AgentView.vue`：统计卡补齐，总量取状态之和，总量卡标注原始总数。
- `src/api/frontend/shell/locales/{zh,en}/agents.json`：新增 pending/failed/cancelled/raw_total。
- `tests/mgmt/test_issue_4240_event_stats.py`：字段与总和口径用例。
- `tests/js/test_issue_4240_event_stats.js`：前端总量/原始总数回退用例。

Out of scope：事件历史表格分页/搜索逻辑与状态列样式；数据库结构变更；大盘/报表口径。

## 验收标准
- [ ] 1 已完成 + 1 失败 + 1 取消 + 1 运行中：总量=4，五状态卡之和=4，原始总数=4。
- [ ] 仅失败+取消：总量=2、失败=1、取消=1，无加不平组合。
- [ ] 中英文词条齐全（pending/completed/running/failed/cancelled/total/raw_total）。
- [ ] `event_stats` 固定五键且缺省 0；未知状态只计入原始总数。
- [ ] `total_events_raw` 为全量事件数且不受 keyword 影响。
- [ ] `events_total`/`events_page`/`events_page_size` 行为不变。