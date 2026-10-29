# 方案设计：Issue #495

## 架构决策（与 Spec 一致）

| 决策 | 选择 | 理由 |
|------|------|------|
| 门控位置 | claim 侧过滤（`claim` + `claim_by_tag` 单点收口） | 广播/定向/本机 local_claimer 全部经这两条链 |
| 熔断阈值 | env `ENGINE_CIRCUIT_BREAKER_THRESHOLD`，默认 3 | 与 health_scorer 的 env 配置风格一致 |
| 摘除方式 | 置 `status='offline'` + `healthy=0` | 保留记录，人工恢复；claim 门控读 healthy 即时生效 |
| 计数存储 | 服务端进程内存（per-runtime） | fail/complete 均回流服务端单进程，无需跨进程持久化 |

## 改动点

1. **新增 `src/runtime/circuit_breaker.py`**：
   - `record_task_failure(runtime_id, task_id, exit_code)`：计数 +1；达阈值 → `UPDATE agent_runtimes SET status='offline', healthy=0` + WARNING 日志。
   - `record_task_success(runtime_id)`：计数清零。
   - `get_failure_count(runtime_id)` / `reset_all()`（测试辅助）。
2. **`claim_by_tag`**：复用既有 `SELECT max_concurrent FROM agent_runtimes` 查询叠加 `healthy` 列，`healthy` 非 NULL 且 != 1 → 拒绝认领（NULL 视为未知，fail-open 不阻塞存量行）。
3. **`claim`（legacy 定向认领）**：入口同款 healthy 门控。
4. **`fail_task`**：终态判死后调用 `record_task_failure`（幂等/重试早退路径不计数）。
5. **`complete_task`**：`exit_code == 0` 落定后调用 `record_task_success`。

## 测试

新增 `tests/test_issue_495_circuit_breaker.py`（复用 test_local_claimer 的内存 sqlite 夹具模式）：
- healthy=0 引擎 claim_by_tag 返回 None，healthy=1 正常认领。
- 连续 3 次 fail_task → runtime 置 offline + healthy=0。
- 中途成功（complete_task exit=0）→ 计数清零，再失败不足阈值不熔断。
- 阈值可配置（monkeypatch env/模块属性）。
