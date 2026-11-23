# Issue #3927 任务拆解

## M1 观测与用例（本 PR 已覆盖）
- [x] `InstrumentedLock` + `lock_owner` + `(lock, owner)` 指标 + 持锁栈 dump
- [x] `db_lock` / `owner_lock` 接入慢锁观测
- [x] 并发回归用例（task lock / 拆写 / 锁外聚合 / 重复 complete / 慢锁）

## M2 complete 路径（本 PR 已覆盖）
- [x] `task_lock(t_<id>)` 登记/回收
- [x] `update_event_complete(task_id=...)` 切任务级锁
- [x] 终态字段与大字段拆事务，大字段锁外写
- [x] 同任务重复 complete 幂等短路
- [x] `complete_task` / `fail_task` 传 task_id

## M3 refresh 与外呼（本 PR 已覆盖主体）
- [x] `_fetch_task_stats` 快照读 + `_aggregate_task_rows` 锁外聚合
- [x] MySQL 下快照读不持进程锁；SQLite 保持短锁
- [x] refresh 逐引擎 + 健康分写回短 UPDATE
- [x] auto-advance A2A 可经 `COMPLETE_A2A_ASYNC=1` 异步化（默认同步）
- [ ] 队列持久化/重试强化（沿用现有 `_drain_a2a_queue_after_terminal`）

## M4 灰度复测（不在本 PR）
- [ ] 同口径 Loki 复测：`db_lock slow hold` / `slow wait` 归零
- [ ] claim/claim-by-tag/heartbeat-v2/tags 最大等待 < 2s
- [ ] 先确认线上部署版本与埋点差异（Open Question）

## 验证命令
```bash
python3 -m pytest tests/runtime/test_3927_lock_scope.py -q
python3 -m pytest tests/runtime/test_health_scorer.py tests/runtime/test_health_scorer_decimal.py -q
python3 -m pytest tests/runtime/test_event_status_protection.py tests/runtime/test_issue_1105_terminal_completed_at.py -q
```