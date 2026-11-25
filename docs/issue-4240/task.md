# Task: Issue #4240

- [x] T1 后端 `event_stats` 固定五状态键（含 cancelled 缺省 0），仅回填白名单状态
- [x] T2 后端 `total_events = sum(event_stats.values())`，新增 `total_events_raw`（不受 keyword 影响）
- [x] T3 前端统计卡补待处理/失败/取消，总量取状态之和并标注原始总数
- [x] T4 zh/en 词条补 pending/failed/cancelled/raw_total
- [x] T5 `tests/mgmt` 新增字段与总和口径用例
- [x] T6 自审查 + 创建 PR + A2A QA
- [x] T7 QA 复审整改：raw 与状态计数同源、前后端状态集合单一来源、teardown 收敛、补 JS 回归、窄屏换行