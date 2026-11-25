# Issue #4295 需求文档（Spec）

> 来源：Issue #4295 正文 + `[issue agent] 📋 Spec Document`（comment 203513854）。
> 本文件是 coding 落地时的需求基线，源码事实全部在本工作区
> （`arch-weekly-11-ljt`）实测核对，不转述。

## 1. 目标

把进程级全局 `db_lock` 的**单次持有**从 99.9s 压到 5s 以内、把 1h 累计等待从 8,005s
压到 1,000s 以内，消除前台读写请求在锁上排队导致的可点名用户故障：

- `POST /api/repos/host` 两条 **502**（duration 47,863ms / 56,243ms，落在 19:53:22–19:55:02
  的 99.9s 持有窗口内）；
- `POST /api/issues/grape-dev/grape` 502（10,374ms）；
- `[patrol] slow scan` 最大 **117,974ms**；nginx **499** 648 次；
- 1h 累计等待 **8,005.5s**（mgmt 4,998.8s + worker），最长 `waited=100.19s`、
  最长 `held=99.90s`（`owner=/api/issues/shawn1235/hello-skill`）。

## 2. 现状（工作区实测）

| 事实 | 实测 |
|---|---|
| 锁实体 | `src/kernel/session_manager.py:59` `db_lock = TrackedRLock()`，**进程级单把 RLock** |
| 埋点模块 | `src/kernel/db_lock_metrics.py` 已在工作基线（290 行）：wait/hold 直方图、waiters/holder/holds 指标、慢 wait/hold 日志（默认 2s） |
| 等锁无上界 | `TrackedRLock.__enter__` → `acquire()` **不带 timeout**；100.19s 等待的直接机制 |
| 慢持有不可观测 | 只有 `_on_release_post` 打 WARNING；**持有期间无看门狗、无栈、无 ERROR** |
| 临界区规模 | `src/domains/runtime/issues_center.py` **45 处** `with db_lock:`（`src/api/issues_center.py` 只是 `sys.modules` 兼容别名） |
| 锁内外部 IO | `close_issue_tasks` / `_close_issue_tasks_bot_takeover` 在锁内调 `maybe_notify_review_point_status` → `_resolve_issue_notify_targets`（conv 兜底建/复用、open_id、本人 bot ctx/api、chat cfg 多次 DB 往返）、`_fire_notify_coro`（IM 投递调度） |
| 502 语义 | 锁等待超时后整请求 502，前端只看到"服务异常"，无重试提示 |

## 3. 范围

### In Scope（本 PR）

1. **有界等锁**：HTTP 请求线程（`path_var` 非空——由 `src/api/server_common.py` 对所有
   HTTP 请求设置，含内部触发的长任务端点）等锁改用 `DB_LOCK_ACQUIRE_TIMEOUT_S`
   （默认 5s，0=关闭→旧的无界语义）；预算耗尽抛 `LockAcquireTimeout`。长任务端点若不能
   接受 503，可在局部用 `foreground_lock_budget(0)` 关闭预算（`None`=继承当前预算、
   `0`=关闭，语义分开）。
2. **503 + Retry-After**：`LockAcquireTimeout` → 503（`reason=db_lock_acquire_timeout`、
   `retry_after_s`、`request_id`），与"上游 VCS 故障"的 502 语义分离。
3. **持有看门狗**：守护线程按 `DB_LOCK_WATCHDOG_S`（默认 1s）扫 `snapshot()`，
   持有超 `DB_LOCK_WATCHDOG_HOLD_S`（默认 5s）当场 ERROR + dump 持锁线程栈，
   同 owner `DB_LOCK_WATCHDOG_DEDUP_S`（默认 60s）去重；看门狗自身**绝不获取 db_lock**。
   看门狗随 `session_manager` 导入启动，并通过 `os.register_at_fork(after_in_child=...)`
   在每个 fork 子进程里复位继承状态后重新拉起——gunicorn `--preload` 等「先 import
   后 fork」形态下 worker 同样有看门狗。
   等锁超时的 ERROR 日志也按 owner 同样口径去重（`DB_LOCK_TIMEOUT_LOG_DEDUP_S`，默认 60s，
   0=不去重），指标 `grape_db_lock_acquire_timeouts_total{owner}` 仍逐次累加。
4. **锁内外部 IO 清出**：`close_issue_tasks` / `_close_issue_tasks_bot_takeover` 的通知投递
   移到 `with db_lock` 之外（状态判定与 UPDATE 仍全在锁内）。
5. **防回归门禁**：`scripts/check_db_lock_external_io.py`（挂 `make lint`）+ 结构断言用例；
   门禁覆盖两种临界区形态——`with db_lock:` 与
   `acquired = db_lock.acquire(timeout=...)` + `try/finally: db_lock.release()`。

### Out of Scope

- `#3927`（closed，complete 终态写 + 后台 refresh）——不重做。
- PR #2129（`purge_stale_event_guards` 分批 DELETE + 语句级 `innodb_lock_wait_timeout`）——
  只做联合验证，不改其实现。
- 全仓 1,226 处 `with db_lock:` 的分域锁改造（按验收结果另立 Issue）。
- MySQL 侧锁等待优化、连接池扩容、前端 `Retry-After` 交互文案。

## 4. 验收标准（本 PR 覆盖到的项）

- [x] 锁等待超时统一 503 + `Retry-After`，有对应用例（`tests/api/test_issue_4295_lock_timeout_503.py`）
- [x] `held > 5s` 当场输出持有线程栈 + ERROR，按 owner 维度指标可查
      （`tests/kernel/test_issue_4295_db_lock_watchdog.py`）
- [x] 断言 `issues_center` 的通知/远程调用不在 `with db_lock:` 内
      （`scripts/check_db_lock_external_io.py` + `tests/kernel/test_issue_4295_external_io_gate.py`）
- [x] 并发用例 3 写 + 1 读：读等待不随写线性增长（免锁读通道 + 有界等锁）
      （`tests/kernel/test_issue_4295_concurrent_read_write.py`）；线程归因断言可失败
      （反证用例 `test_reader_isolation_assertion_can_fail`）
- [x] preload 形态（先 import 后 fork）下 fork 子进程 `watchdog_running()` 为 True、
      父进程持锁状态不残留（`test_watchdog_restarts_in_fork_child_after_preload`）
- [ ] 生产 1h 窗口 `db_lock slow hold` 最大值 < 5s、累计等待 ≤ 1,000s
      → 需上线后由 sre 观察窗口验证（本 PR 提供信号：`grape_db_lock_watchdog_events_total{owner}`
      / `grape_db_lock_acquire_timeouts_total{owner}` / `db_lock watchdog` ERROR 日志）

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 有界等锁把长任务打成 503 | 只约束**HTTP 请求线程**（`path_var` 非空，含内部触发的长任务端点）；后台 sweeper/refresh/批处理无请求上下文 → 保持无界；长任务端点可局部 `foreground_lock_budget(0)` 关闭预算；`DB_LOCK_ACQUIRE_TIMEOUT_S=0` 可整体关闭 |
| 超时抛点若在临界区内会造成"未加锁的 DB 写" | 抛出点固定在 `__enter__`/`acquire()` 返回前——**进入临界区之前**，块体不执行；用例断言 `entered == []` |
| 看门狗日志风暴 / 抓栈开销 | 同 owner 60s 去重、ERROR 级别、栈深截断 15 帧、只读快照不动锁 |
| RLock 可重入导致加锁函数内层再次 `with db_lock` 误超时 | 同线程重入立即成功（预算不参与），用例 `test_reentrant_acquisition_never_times_out` |
| 门禁误报导致被放宽到失效 | 门禁只认"确定出进程"的调用；误报样本固化为反向用例（`test_gate_ignores_local_only_and_plain_db_calls`） |

## 6. 回滚

四项均可独立关闭且不改锁语义：`DB_LOCK_ACQUIRE_TIMEOUT_S=0`（无界等锁）、
`DB_LOCK_WATCHDOG_S=0`（关看门狗）、`DB_LOCK_TIMEOUT_LOG_DEDUP_S=0`（超时日志不去重）、
门禁脚本可从 `make lint` 摘除；通知投递位置回退是纯代码回滚（无数据迁移）。