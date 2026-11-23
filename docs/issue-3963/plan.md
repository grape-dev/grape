# Issue #3963 方案设计

## 1. 总体思路

把「抢锁窗口」与「租约 TTL」解耦，并把 busy 从「终态失败」降级为「瞬态争用」：

```
scheduler._execute_task
  └─ _execute_autopilot_agent → _execute_autopilot_remote
       └─ service.enqueue_task
            └─ kernel.lock.acquire_lock_with_retry(wait_seconds ≈ TTL)   # 排队等待
                 └─ 预算耗尽 → RepoDispatchBusyError(error_code=REPO_DISPATCH_BUSY)
       ← except 分支写 autopilot_executions.error_code=REPO_DISPATCH_BUSY
  ← 瞬态通道：不消耗 attempt，1/2/4s 退避同 tick 重试；预算耗尽 → 记录 skipped + requeue 下一 tick
```

## 2. 分模块改动

### 2.1 `src/kernel/lock.py`

新增参数（**默认保持旧行为**）：

```python
acquire_lock_with_retry(lock_key, ttl=60, attempts=20, interval=0.05,
                        wait_seconds=None, jitter_ratio=0.0)
```

- `wait_seconds is None` → 走原 `attempts × interval` 路径，语义逐字不变（`event_pipeline` 等调用点零变化）。
- 给定预算 → deadline 驱动：每次失败按 `min(interval, remaining)` 睡眠（可选抖动，且不超过剩余预算），
  预算耗尽后**末尾补一次尝试**，返回 `False` 表示预算内未抢到。
- 睡眠下限 1ms，避免 `interval=0` 时热自旋。

### 2.2 `src/domains/runtime/engine/service.py::enqueue_task`

- 常量：`_REPO_DISPATCH_LEASE_TTL = 30`、`_REPO_DISPATCH_LEASE_WAIT_SECONDS = TTL + 5（余量）`、
  `REPO_DISPATCH_BUSY = "REPO_DISPATCH_BUSY"`。
- 调用点：`acquire_lock_with_retry(key, ttl=TTL, interval=0.25, wait_seconds=WAIT, jitter_ratio=0.2)`；
  实测耗时落 `record_repo_dispatch_lease_wait(repo_key, seconds)`。重试间隔取 0.25s 而非原语
  默认 0.05s（35s 预算下 0.05s 会打出约 700 次抢锁/等待者，纯 DB 噪声）。
- 失败抛 `RepoDispatchBusyError`（`RuntimeError` 子类，`error_code` 属性 = `REPO_DISPATCH_BUSY`），
  文案**保留** `task enqueue busy for repo {repo_key}`（既有 Loki 查询/看板不失效），并记 `busy_total{repo}`。
- 临界区（`with db_lock:` 插入）与 `finally` 释放租约不变。

为什么预算 = TTL + 余量：租约 TTL 是「持锁者崩溃后锁最晚过期时间」，等待 ≥ TTL 才能覆盖
最坏情况（AC 明确要求「有效等待 ≥ 租约 TTL」）；5s 余量抵消抖动/时钟粒度。

### 2.3 `src/domains/runtime/engine/scheduler.py`

- 新常量：`ERROR_REPO_DISPATCH_BUSY = "REPO_DISPATCH_BUSY"`、`_BUSY_RETRY_BUDGET = 3`、
  `_BUSY_RETRY_DELAYS = (1, 2, 4)`、`_BUSY_LOG_FINGERPRINT = "task enqueue busy for repo"`。
  **不**加入 `_ENV_FAILURE_CODES`（不走 60s 退避 + 熔断）。
- `_execute_autopilot_remote` 的 enqueue `except` 分支：`_enqueue_failure_code(exc)` 分类后写
  `error_code`（保持原有 `入队异常: {e}` 文案，含 busy 指纹）。
- `_classify_env_error` 增加 busy 指纹回推（存量库缺 `error_code` 列时仍能识别瞬态）。
- `_execute_task` 主循环：`for range(max_retry+1)` → `while attempt <= max_retry`（显式 `attempt += 1`），
  新增 busy 分支：
  - `busy_retries < _BUSY_RETRY_BUDGET`：`busy_retries += 1`、**不** `attempt += 1`、sleep 1/2/4s（+抖动）后 `continue`；
  - 预算耗尽：写 `autopilot_executions.status=skipped` + `error_code=REPO_DISPATCH_BUSY`（非 failed：
    管理台/统计不把它算失败），记 WARNING（含指纹），跳出循环 → 走正常收口：`next_run` 按 cron 推进（= 下一 tick 重投），
    **不**推进 `consecutive_failures`/`backoff`/熔断。
- 退出路径不调用 `_notify_subscribers`（争用不是任务结果）。

### 2.4 可观测

- `src/domains/insight/observability/__init__.py`：
  - `REPO_DISPATCH_BUSY = Counter("grape_repo_dispatch_busy_total", ..., ["repo"])`
  - `REPO_DISPATCH_LEASE_WAIT = Histogram("grape_repo_dispatch_lease_wait_seconds", ..., ["repo"],
     buckets=(0.05,0.1,0.25,0.5,1,2.5,5,10,20,30,60))`
  - `record_repo_dispatch_busy(repo_key)` / `record_repo_dispatch_lease_wait(repo_key, seconds)`（异常内吞，不拖垮入队）。
- `src/domains/insight/public/__init__.py` 转发（跨域只走 public，函数内 import 防环）+ `__all__`。
- `deploy/alerts.yml`：`GrapeRepoDispatchBusyHigh` —— 5 分钟窗口同仓 busy ≥ 3 次，warning。

### 2.5 测试 seam

| 切片 | seam |
|---|---|
| 等待预算 | `kernel.lock.acquire_lock_with_retry(wait_seconds=...)`（公开原语） |
| 入队等待/结构化 busy | `service.enqueue_task`（公开接口；持锁/释放走真实 `distributed_locks`） |
| 瞬态语义 | `scheduler._execute_task` + `autopilot_executions` 行（行为级） |
| 埋点 | `observability.REPO_DISPATCH_BUSY/_LEASE_WAIT` 的 `_value`/`_sum` |
| 告警 | `deploy/alerts.yml` 文本契约（promtool 不可用时最小断言） |

## 3. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 抢锁等待变长拖慢入队线程 | 预算上限 TTL+5s；超预算即 requeue，不阻塞调用方线程池长期占用；抖动防同刻齐抢 |
| 长持锁（#3927）时入队最长等待 35s；chat/events 的 async 路由也走同一 enqueue，极端场景会阻塞事件循环 | 预算有硬上限（不会无限等）；正常临界区毫秒级，只有 #3927 级长持锁才逼近上限——该场景本身已是全局阻塞态，本单把它从「立即判死」改成「有界排队」，并在 Issue 评论标注风险 |
| `lock.py` 公共原语外溢 | `wait_seconds` 默认 None 保持旧路径，回归用例锁定 `attempts/interval` 语义 |
| 载入 `skipped` 会被误当接管跳过 | 语义上同为「非失败、本 tick 不重试」；区分靠 `error_code=REPO_DISPATCH_BUSY` 与 error 文案（管理台按 error_code 区分「排队冲突/执行失败」） |
| 指标标签基数 | 仅 `repo` 一维（与既有 per-repo 指标一致） |