# Issue #4303 方案设计（Plan）

## 改动面（全部在 sweeper 一条代码路径上，无契约/API 变更）

### 1. `src/domains/runtime/engine/cleanup.py`

**a. `_commit()` 自递归修复（相邻缺陷一并修）**

legacy 分支 `_commit()` → `_commit()` 无限递归被 except 吞掉。改为与全仓一致的
`with db_lock: db.commit()`（db_lock 为 TrackedRLock，`_sweep_cursor()` legacy
临界区内重入安全）。own 模式分支行为不变（autocommit，不碰主连接）。

**b. 重启恢复窗口护栏（新增）**

- 模块级 `_PROCESS_STARTED_AT = time.time()`（cleanup 在进程启动即被 import，
  各进程独立取值，≈ 进程启动时刻）。
- `_restart_grace_minutes()`：env `SWEEPER_RESTART_GRACE_MINUTES`
  （默认 10，0=关闭），调用时读取便于测试与运维控制。
- `sweep_stuck_events_once()` 入口处：uptime < 窗口 → log.info + return 0
  （首轮巡检天然落在窗口内；仅 `created_at` 距今阈值不够——被重启打断的事件
  `created_at` 早于重启，护栏必须扣住「进程刚启动过」）。

**c. 第二游标方言层修复（根因）**

新增 `_second_sweep_cursor(c)`：

- `isinstance(c, db_registry._CompatCursor)`（own 模式/三库）→
  `db_registry._CompatCursor(c.connection.cursor())`：从裸 pymysql 连接再开
  游标后**重新包一层翻译层**（`?→%s` 只发生在 `_CompatCursor.execute` 内）。
- 其余（legacy）→ `db.cursor()`：主库游标由 `db.cursor()` 统一提供正确方言
  （sqlite 原生 `?` / MySQLCompatCursor 自带翻译；MySQLCompatCursor 无
  `connection` 属性，沿用 `c.connection.cursor()` 在 legacy MySQL 下会
  AttributeError，同样落入降级链）。**绝不能**在 legacy 分支包
  `_CompatCursor`——sqlite 上 `?` 会被译成 `%s`，绑定直接报错。

**d. per-seq 精收重构：逐条降级替代整段回落 bulk**

`_no_task_txt`/`_timeout_txt` 提升为模块常量 `_NO_TASK_TXT`/`_TIMEOUT_TXT`
（文案逐字不变，测试断言的是子串）。per-seq 主循环提取为
`_recover_stuck_events(dead, terminal_by_seq, now_iso) -> int`，三层：

1. **主路径**（不变语义）：terminal → `_cascade_event_terminal` 级联（回填
   真实 result_text）；无关联任务行 → `_NO_TASK_TXT`；有任务行非活跃 →
   `_TIMEOUT_TXT`。**每个 seq 独立 try/except**：单 seq 异常只影响该 seq，
   按 seq 记 `log.warning`（失败原因不静默），其余 seq 照常收敛。
2. **逐条降级**：失败 seq 换新 `_sweep_cursor()` 重读该 seq 的 agent_tasks
   终态——能级联则级联，确实无法级联才写对应文案；再失败记 `log.error`。
3. **最后手段 bulk**：降级也失败的 seq 才整批 UPDATE，占位符绑定文案
   （`result=?`，替换原 `result='{_timeout_txt}'` f-string 直拼），
   `log.error` 显式告警；bulk 也失败则保留事件原状（下一轮巡检重试），
   不再吞异常误标。

终端态任务查找（`_terminal_by_seq`）保持在调用方（带自身 except）。

### 2. 测试

**新增 `tests/runtime/test_4303_sweep_own_dsn.py`**（按 per-issue 命名惯例）：

- `_FakeRawCursor`：裸 pymysql 游标语义——带参执行=`sql % args`，SQL 无 `%s`
  即抛生产同款 `TypeError`；`%s` 翻回 `?` 后落真 sqlite 执行（数据可见性与
  autocommit 域连接等价，每语句后提交）。
- `_FakeOwnConn`：`_CompatConn` 语义，`cursor()` 返回
  `db_registry._CompatCursor(_FakeRawCursor(...))`。
- fixture `own_dsn_topology`：`monkeypatch.setattr(db_registry,
  "uses_own_dsn", lambda d: d == "runtime")` + `new_connection` 返回假连接
  （复用 `tests/api/test_p2_lib_switch.py`、`test_issue_3826_*` 的既有 patch 模式）。
- 用例：
  1. 三库拓扑 per-seq 文案 UPDATE 不抛 TypeError、`?→%s` 翻译发生
     （断言假游标收到的 SQL 只含 `%s`）、不触发 bulk 回落（无 `seq IN` 形态
     UPDATE、无 per-seq failed 日志）。
  2. 三库拓扑下任务已终态（completed, exit=0, 带真实 result_text）→
     事件级联 completed、回填真实结论，不被写成 failed。
  3. 重启恢复窗口内（`_PROCESS_STARTED_AT=now`）在途事件不动；
     窗口外（1h 前）照常回收；`SWEEPER_RESTART_GRACE_MINUTES=0` 关护栏。
  4. per-seq 写失败 → 逐条降级重读终态后正确收敛（原因记 warning）；
     降级也失败 → 最后手段 bulk 占位符绑定文案 + log.error（假游标记录参数，
     断言文案经绑定参数传入而非内联）。
- 既有三个受护栏影响的测试文件加 autouse fixture，把
  `_PROCESS_STARTED_AT` 钉到 1h 前（它们测的是回收逻辑本身）：
  `test_cleanup.py`、`test_pending_self_report_convergence.py`、
  `test_issue_587_event_convergence.py`。`test_sweep_dual_signal.py` 只测
  `sweep_stuck_agent_tasks_once`（无护栏），不动。

**CI**：新测试文件接入 `.github/workflows/windows-smoke.yml` linux-backend
job（显式 per-issue 步骤表，缺接线的测试文件 CI 永不执行）。

## 风险与对策

- 护栏误伤既有测试：仅 `sweep_stuck_events_once` 有护栏；受影响文件加
  `_PROCESS_STARTED_AT` fixture，护栏自身用例显式控制。
- 文案契约：`_NO_TASK_TXT`/`_TIMEOUT_TXT` 逐字保留，仅提升作用域。
- 基线红：`test_cleanup.py` 当前 7 个 pre-existing 失败（cascade 语义与
  陈旧断言漂移等），按 base-run 证据引用，不在本单修复（避免 scope creep）。
