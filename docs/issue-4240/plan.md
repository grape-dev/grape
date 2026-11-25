# Plan: Issue #4240

## 决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 失败/取消呈现 | 分列「失败」「取消」 | 状态卡之和等于总量，语义清晰 |
| 总量口径 | 五状态之和，另标原始总数 | 消除加不平组合，保留原始数据可追溯 |
| 待处理 | 展示（含 0） | 状态集合完整 |
| 未知状态 | 不进状态卡字典，只计入原始总数 | 防止再次出现加不平 |
| 原始总数数据源 | 复用 `event_status_counts` 的 `GROUP BY status` 结果求和 | 与状态卡同源同库，含未知状态，少一次跨域查询 |

## 改动
1. `src/domains/mgmt/routes/agents.py`
   - `EVENT_STATUSES` 常量定义五状态，`event_stats` 由其派生（单一来源）。
   - `GROUP BY status` 回填仅接受五状态白名单。
   - `total_events_raw` 由同一次 `GROUP BY status` 结果求和（含未知状态），不受 keyword 影响；取不到时置 `None` 表示降级。
   - `metrics` 下发 `total_events`（状态之和）与 `total_events_raw`。
2. `src/api/frontend/app/src/views/AgentView.vue`
   - 模板 `v-for` 渲染 `statusCards`（总数卡 + 五状态卡）；总量卡次要行展示 `agents.event.raw_total`。
   - `EVENT_STATUSES` 单一来源驱动 `statusCards`；`totalEvents` 为状态之和；`rawTotal` 缺省/不可用时回退 `totalEvents`。
3. `src/api/frontend/shell/locales/{zh,en}/agents.json`：同批补齐四个词条。
4. `tests/mgmt/test_issue_4240_event_stats.py`：覆盖字段、总和、原始总数与分页语义；teardown 仅清本用例 repo_key 行。
5. `tests/js/test_issue_4240_event_stats.js`：前端 `totalEvents`/`rawTotal` 回归（含 raw=0 与缺省回退）。

## 风险与缓解
- `total_events` 语义变化：仅 AgentView 消费；`total_events_raw` 保留原始值。
- 原始计数失败：捕获异常后 `total_events_raw=None`（前端回退状态之和），不冒充原始值，详情接口保持可用。