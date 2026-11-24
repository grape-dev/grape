# Issue #3963 需求文档：同仓 enqueue 撞 repo-dispatch 租约不再判死

> 基线：`arch-weekly-11-ljt`（开发分支 `feat/issue-3963`）

## 1. 现象（生产证据）

2026-11-23 20:00–20:03（master-002/003）Loki：

- `task enqueue busy for repo ...` 4 条，覆盖 3 个仓库（grape-dev/grape、grape-dev/grape-skills、GTDTeam/CodeArts）；
- `failed after 0 retries` 2 条（task 18 / task 46，`max_retry=0`）；
- `Autopilot zombie recovery` 2 条（exec 26574/26577，靠僵尸回收兜底才收敛）；
- 前 1 小时同文案 0 条 → 与主控负载升高（#3927 的 `db_lock` 17–50s 长持有）正相关。

## 2. 根因（已按代码核对）

| 层 | 代码位置 | 事实 |
|---|---|---|
| 抢锁原语 | `src/kernel/lock.py::acquire_lock_with_retry` | 默认 `attempts=20 / interval=0.05`，最长等待 19×0.05 ≈ **0.95s** |
| 入队调用点 | `src/domains/runtime/engine/service.py` `enqueue_task` | 以 `ttl=30` 抢 `repo-dispatch:<repo>`，0.95s 抢不到即 `raise RuntimeError("task enqueue busy for repo ...")` |
| 调度侧 | `src/domains/runtime/engine/scheduler.py::_execute_task` | `for attempt in range(max_retry + 1)`，`max_retry=0` 时首次失败即落 `autopilot_executions.status=failed`，无瞬态分类 |

口径失配：**等待窗口 0.95s ≪ 租约 TTL 30s**，而临界区（`with db_lock:` 内 INSERT agent_tasks）在高峰期常 > 1s；
busy 又被当永久失败，定时任务直接被判死丢弃。

## 3. 目标

同仓 enqueue 撞上 `repo-dispatch` 租约时**排队等待**而不是抛 `task enqueue busy` 判死；
busy 归为**瞬态**（不消耗 attempt、超预算 requeue 下一 tick），定时任务不丢；并补齐指标与告警。

## 4. 范围内

1. `acquire_lock_with_retry` 支持总等待预算 `wait_seconds`（默认 None = 旧行为不变，该原语被多处复用）。
2. enqueue 调用点预算 ≈ 租约 TTL，失败抛**结构化** `REPO_DISPATCH_BUSY`（保留旧文案供既有 Loki 查询/看板）。
3. 调度侧 busy 不消耗 attempt、同 tick 短退避重试、超预算 requeue 下一 tick；**不复用** `_ENV_FAILURE_CODES`（60s 退避 + 连续 3 次熔断会把秒级争用升级成任务停用）。
4. 可观测：`grape_repo_dispatch_busy_total{repo}`、`grape_repo_dispatch_lease_wait_seconds{repo}`；`deploy/alerts.yml` 连续 busy 告警；`autopilot_executions.error_code` 落 busy 分类。
5. 测试：`tests/runtime/test_issue_3963_repo_dispatch_lease.py`。

## 5. 范围外

- #3927 的 `db_lock` 单次 17–50s 长持有治理（本条只作为放大器，修复互补）。
- zombie recovery 判定语义；历史 exec 26574/26577 个别收口。
- 组织解绑路径的锁策略改造（同为 repo-dispatch key 持有者，行为约束一致即可）。

## 6. 验收标准

- [ ] `acquire_lock_with_retry` 在 enqueue 调用点的有效等待预算 **≥ 租约 TTL**，有单测断言。
- [ ] 同仓 5 并发 enqueue（其一持锁 2s）其余 4 个 ≤TTL 全部入队，0 次 busy 失败。
- [ ] busy 不消耗 attempt：`max_retry=0` 的任务遇 busy 仍成功派发；永久错误仍按原语义落 failed。
- [ ] 指标/告警：`grape_repo_dispatch_busy_total{repo}`、`grape_repo_dispatch_lease_wait_seconds` 可查，连续 busy 告警存在；`autopilot_executions.error_code=REPO_DISPATCH_BUSY`。
- [ ] 回归：busy 路径不再产生 `failed after 0 retries`；`acquire_lock_with_retry` 其它调用点（未传预算）行为不变（有回归用例）。
- [ ] 现有测试全绿。