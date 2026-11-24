# Issue #3927 任务拆解

## M1 观测与用例（本 PR 已覆盖）
- [x] `InstrumentedLock` + `lock_owner` + `(lock, owner)` 指标 + 持锁栈 dump
- [x] `db_lock` / `owner_lock` 接入慢锁观测
- [x] 并发回归用例（task lock / 事件锁 / 拆写 / 锁外聚合 / 重复 complete / 慢锁）
- [x] 审查修订：慢等待区分 waiter_stack / holder_stack(previous)；指标粗化
      （task_lock/event_lock + owner 折叠）+ `LOCK_METRICS_MAX_ENTRIES` 上限（R3）

## M2 complete 路径（本 PR 已覆盖）
- [x] `task_lock(t_<id>)` 登记/回收；`event_lock(t_ev:<seq>)` 登记/回收
- [x] `update_event_complete(task_id=...)` 终态保护统一按事件维度锁（R1）；
      `reset_event_terminal`、`update_event_status`、级联/takeover/late-result
      直写路径同纳 event_lock（R1 复审）
- [x] 终态字段与大字段拆事务；大字段在事件锁内短写、不持全局锁（R2 修订）
- [x] payload 写失败登记 pending，重复 complete 短路时自愈补写（R2）
- [x] SQLite/单库 `_write_guard` 短临界区防跨线程事务交错（R4）
- [x] 同任务重复 complete 幂等短路
- [x] `complete_task` / `fail_task` 传 task_id

## M2b update_task_status 拆分（审查 R5）
- [x] db_lock 短临界区：终态判定 + status/completed_at/exit_code + 级联
- [x] 大字段**先于终态**锁外单条 UPDATE 短写（MySQL 免锁 / SQLite 短临界区），
      `WHERE status NOT IN (终态)` 防覆盖并发赢家
- [x] 大字段写失败登记 pending，重复调用/巡检补写（`pending_task_big_count`）
- [x] claim / claim_by_tag / update_task_status `lock_owner` 归因埋点
- [x] 并发 3 complete + 1 claim 回归用例（spec 验收第 1 条）
- [x] 混合锁口径回归用例（带/不带 task_id 同 seq 互斥）
- [x] 跨写者事件锁回归用例（update_event_status/级联/takeover/late-result）
- [x] 大字段先于终态 + pending 自愈回归用例

## M3 refresh 与外呼（本 PR 已覆盖主体）
- [x] `_fetch_task_stats` 快照读 + `_aggregate_task_rows` 锁外聚合
- [x] MySQL 下快照读不持进程锁；SQLite 保持短锁
- [x] refresh 逐引擎 + 健康分写回短 UPDATE
- [x] auto-advance A2A 可经 `COMPLETE_A2A_ASYNC=1` 异步化（默认同步）
- [ ] 队列持久化/重试强化（沿用现有 `_drain_a2a_queue_after_terminal`）

## M3b CI 门禁（审查-受控执行 Required）
- [x] `.github/workflows/lock-regression.yml`：push/PR 必跑
      `test_3927_lock_scope.py` + `tests/kernel/test_owner_db.py`

## M4 灰度复测（不在本 PR）
- [ ] 同口径 Loki 复测：`db_lock slow hold` / `slow wait` 归零；
      确认 17–50s 命中 lock 名与 owner（判定口径见 plan.md §2b 灰度验证步骤）
- [ ] claim/claim-by-tag/heartbeat-v2/tags 最大等待 < 2s
- [ ] `pending_payload_count()` 长期为 0（非 0 = payload 短写持续失败需排查）
- [ ] 先确认线上部署版本与埋点差异（Open Question）

## 验证命令
```bash
python3 -m pytest tests/runtime/test_3927_lock_scope.py -q
python3 -m pytest tests/runtime/test_health_scorer.py tests/runtime/test_health_scorer_decimal.py -q
python3 -m pytest tests/runtime/test_event_status_protection.py tests/runtime/test_issue_1105_terminal_completed_at.py -q
python3 -m pytest tests/kernel/test_owner_db.py -q
```