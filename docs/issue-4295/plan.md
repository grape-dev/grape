# Issue #4295 方案设计（Plan）

## 0. 一句话

**锁内只留"读/写自己那一小段"，外部 IO 与目标解析全部移到锁外；同时在锁上加
"持有中可观测 + 等锁有上界"，让残留的长持有当场暴露、等锁超时不再表现为无声 502。**

## 1. 改动面

| 文件 | 改动 | 作用 |
|---|---|---|
| `src/kernel/db_lock_metrics.py` | `LockAcquireTimeout`、`acquire_budget_s()`、`foreground_lock_budget()`、`TrackedRLock.acquire` 有界分支、持有看门狗（`start_watchdog`/`stop_watchdog`/`watchdog_scan`）、`snapshot()` 补 `tid` | 锁实体层的两个新能力（有界等锁 + 持有中可观测） |
| `src/kernel/session_manager.py` | 随 `db_lock` 就位自动启动看门狗；re-export `LockAcquireTimeout`/`acquire_budget_s`/`foreground_lock_budget` | 让看门狗在多进程入口（mgmt/event/worker/insight）都生效，无需各入口分别装配 |
| `src/api/exception_handlers.py` | 新增 `LockAcquireTimeout` 处理器 → 503 + `Retry-After` | 语义分离：锁等待超时=本机繁忙（可重试）≠ 上游 VCS 故障（502） |
| `src/domains/runtime/issues_center.py` | `close_issue_tasks` / `_close_issue_tasks_bot_takeover`：通知投递移出 `with db_lock` | 把最大的"锁内外部 IO + 多次 DB 往返"清出临界区 |
| `scripts/check_db_lock_external_io.py`（新） | AST 门禁：`with db_lock:` 块内不得出现外部 IO | 防回归（下一个人再把远程调用塞回锁里，`make lint` 直接失败） |
| `Makefile` | `lint` 追加门禁脚本 | 进 CI 必经路径 |
| `.env.example` | 新增 5 个 env 的说明（默认值 + 关闭开关） | 运维可调、可回滚 |
| `docs/issue-4295/*` | spec/plan/task | 与既有 `docs/issue-N/` 一致 |

## 2. 关键设计决策

### 2.1 预算是"请求上下文"驱动，不是"全局开关"

```python
def acquire_budget_s() -> float:
    budget = env(DB_LOCK_ACQUIRE_TIMEOUT_S, 5.0)
    if budget <= 0: return 0.0
    return budget if path_var.get() else 0.0   # 只有前台请求线程有预算
```

- 为什么不用中间件逐个装配：仓库有**两份**请求中间件（`src/api/middleware.py` 与
  `src/api/server_common.py::_attach_request_log`），四进程入口各装其一。锚在
  `db_lock_metrics` 侧读 `path_var`（`_owner_key()` 本来就用它做 owner 归因），
  一处生效、无入口遗漏。
- 为什么后台必须无界：sweeper/refresh/批处理持锁是**自己**的长活，被预算打断会
  变成任务失败；它们等待他人时没有前台 SLA，保持旧行为最安全。
- 重入：同线程已持锁 → `RLock.acquire(blocking, budget)` 立即返回 True，预算不参与。

### 2.2 超时必须抛，不能返回 False

`with db_lock:` 走 `__enter__`，若 `acquire()` 只返回 False，块体会在**没拿到锁**的
情况下执行（未加锁的 DB 访问，比慢更糟）。因此：有界分支失败 → `_on_budget_exhausted`
记指标 + ERROR 后 `raise LockAcquireTimeout`，抛出点在进入临界区之前。
显式 `acquire(timeout=N)` 仍按 RLock 契约返回 `False`（`pipeline_config.py` 的
`acquire(timeout=30)` 依赖该语义）。

### 2.3 看门狗独立守护线程 + 只读快照

- 持有中无法在持有线程内自检（它正卡在锁里），必须旁路线程；
- 看门狗**不得获取 db_lock**——否则它自己成为等待者，长持有期间看门狗也停摆；
  实现只读 `_STATE`（`_STATE_GUARD` 是独立小锁，与 db_lock 无关）；
- 去重按 owner 60s（长持有持续几十分钟时不至于刷爆日志），栈截断 15 帧；
- 线程随 `session_manager` 导入启动（daemon，幂等），`DB_LOCK_WATCHDOG_S=0` 关闭。

### 2.4 通知移出锁：状态写仍原子，投递不占锁

`close_issue_tasks` / `_close_issue_tasks_bot_takeover` 的锁内工作收敛为
`SELECT 判定 → UPDATE → commit`，并在锁内定格 `_promoted_review`（本次真实推进行数）
与 `_affected`（rowcount）；**出锁后**再按 `_promoted_review` 决定是否投递审核点通知。

- 不变的是幂等判据（`UPDATE ... WHERE status=...` 的 rowcount），通知风暴防线未动；
- 变的是"目标解析"（issue 行 → conv 兜底建/复用 → open_id → 本人 bot ctx/api → cfg）
  与 IM 投递调度不再占住全局锁；
- 通知本身仍是 fire-and-forget（`_fire_notify_coro`），不改变响应时延。

### 2.5 门禁用 AST 且**高精度**

只报"确定出进程"的形态，避免误报导致门禁被放宽：

- 命中源：函数名/属性名含 `notify|vcs|gitcode|github|gitlab|feishu|dingtalk|wecom|
  slack|telegram|webhook|urlopen|requests|httpx|aiohttp|subprocess|_client`；
- 客户端变量：`client = _pr_vcs_client(...)`（工厂绑定）与惯用名 `client/_client/vcs/_vcs`
  的**方法**调用；`fetchone/fetchall/execute/commit/...` 等 DB 本地方法排除；
- 白名单只登记"名字像 IO、实现只碰本地/DB"的完整点分名（DDL 建表、映射表查询、纯函数）；
- **只在 `db_lock` 语义确定时才算临界区**：`db_lock`/`_db_lock` 及其 import 别名、
  `X = db_lock` 赋值；`with lock:` 若指向用户级 `threading.Lock`（`src/api/deps.py`）
  不算——`db, lock = _db()` 这类静态无法自证的形态按文件登记（`EXTRA_LOCK_ALIASES`）。

门禁当前在整棵 `src/` 上**0 命中**，用例固化成三件事：零命中、能抓出合成违规、
不误报 DB/本地调用。

## 3. 影响面与验证

| 验证 | 命令 | 结果 |
|---|---|---|
| 新增用例 | `pytest tests/kernel/test_issue_4295_*.py tests/api/test_issue_4295_*.py` | 29 passed |
| 锁埋点回归 | `pytest tests/kernel/test_db_lock_metrics.py` | 8 passed |
| kernel 全量 | `pytest tests/kernel -q` | 1031 passed / 53 failed（基线同为 53 failed / 1002 passed，无新增） |
| 门禁 | `python3 scripts/check_db_lock_external_io.py` | 0 命中 |
| 静态门禁 | `make lint`（含新增门禁） | 见交付评论 |

## 4. 未纳入（另立 Issue 的依据）

- 全仓 1,226 处 `with db_lock:` 的分域锁/读写锁改造：影响面过大，本 PR 只收口
  "前台读路径 + 锁内外部 IO + 可观测性"，分域锁按上线后 1h 窗口指标再评估。
- MySQL 侧 `innodb_lock_wait_timeout` 与连接池：属 #3927 / PR #2129 家族。
- 前端对 `Retry-After` 的自动重试交互：前端改动需与后端同 PR 说明，独立排期。