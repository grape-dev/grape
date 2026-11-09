# Plan: Issue #1667 A2A 去重上下文维度与排队派发

## 切片
1. **切片 1**：`_has_active_a2a_event` 增加 `context_key`（idempotency_key 优先，其次 context 摘要）维度；旧调用方不传 key 时保持 #587 语义。
2. **切片 2**：`A2AHandoffBody` 增加 `queue` / `force`；命中活跃事件时 `force` 放行、`queue` 落 status='queued' 的 a2a 事件（payload `_a2a_queue.existing_seq` 关联前序）。
3. **切片 3**：抽出 `src/api/a2a_dispatch.py::run_a2a_dispatch`，正常 A2A 与排队 drain 共用同一派发实现（含 `_on_handoff_dispatch` 兼容 re-export）。
4. **切片 4**：`event_store.update_event_complete/update_event_status` 终态后调用 `drain_queued_a2a`；排队事件原子 `queued -> running` 抢占，保证并发终态回调下仅派发一次。
5. **切片 5**：`GET /api/a2a/status` 输出 `queued` / `queue_seq` / `existing_seq`。
6. **切片 6**：queue 幂等（同上下文复用排队记录）、force 取代同上下文旧排队（cancelled）。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 排队载体 | events 行 `status='queued'` + payload `_a2a_queue` | 零 schema 变更，天然获得 seq 语义，且不会被 worker claim |
| 派发实现 | 抽出 `run_a2a_dispatch` 共用 | 避免正常/排队两条路径行为漂移与循环依赖 |
| 去重默认 | 无 context/idempotency_key 时保持旧 duplicate 语义 | 存量调用方零感知，向后兼容 |
| 幂等派发 | `UPDATE ... WHERE status='queued'` 原子抢占 + 派发后标记 `dispatched_at` | 多终态回调（complete/failed/cancelled）不会重复派发 |
| 失败处理 | drain 派发失败走 `_fail_event` | 与 A2A 派发失败诊断字段一致，便于排障 |

## 测试 seam
- `tests/test_a2a_dedup_queue_1667.py`：TestClient + 临时 SQLite（复用 conftest 隔离库），seed managed_repos / engine / repo 技能。
- 覆盖：不同 context 放行、同 context duplicate（#729 original_event）、queue 响应与 drain 一次、queue 幂等、force 覆盖。

## 提交拆分
- feat(#1667): a2a context dedup + queue/force + queued drain
- test(#1667): cover dedup queue force drain