# Issue #4303 需求文档（Spec）

> 来源：VOD 收敛（评论 203553365）+ issue agent 深度可开发性分析（评论 203554049，评级 ready）。
> 生产实例：2026-11-24 21:09:58 worker 停止 → 21:10:14 重启，21:12:35 首轮巡检
> `sweeper: per-seq recovery failed, fallback bulk: not all arguments converted during string formatting`，
> 9 个在途事件同秒被一刀切判 failed（8 个停留 failed；106798 需人工 redispatch 才恢复真实结论）。

## 1. 问题陈述

event sweeper 的 per-seq 精收路径（`src/domains/runtime/engine/cleanup.py`，
`sweep_stuck_events_once`）在三库拓扑（`uses_own_dsn("runtime")=True`）下必然失效：

1. **游标方言层被绕过（根因）**：own 模式下 `_sweep_cursor()` yield 的是
   `db_registry._CompatCursor`（`?→%s` 翻译只发生在其 `execute` 内）；
   `_c2 = _c.connection.cursor()` 经 `_CompatCursor.__getattr__` 透传到
   **裸 pymysql 连接**再开裸游标，方言翻译层被整个绕过 → 首条含 `?` 的
   UPDATE 与 `_cascade_event_terminal` 内的 `SELECT ... WHERE seq=?` 都抛
   `TypeError: not all arguments converted during string formatting`。
2. **整段回落 bulk 一刀切**：外层 `except` 把该轮**全部** dead seq 统一写
   `status='failed'` + `_timeout_txt`，#587/#1938 的精收语义（区分
   「无关联任务行→创建任务失败」/「running 超时」、按任务终态级联、回填
   真实 `result_text`）全部作废，失败原因不可归因且被静默。
3. **回落 bulk SQL 为 f-string 直拼文案**：`result='{_timeout_txt}'` 违反
   占位符绑定约定，文案含 `'` 即崩。
4. **重启瞬间无护栏**：重启把「执行到一半被切断」的在途事件留在 running，
   其 `created_at` 早于重启——恰是最该保护的一批，首轮巡检却最先回收它们。
5. **相邻缺陷**：`cleanup.py` `_commit()` 的 legacy 分支误写为自调用，
   无限递归被 `except` 吞成 `log.debug`，单库拓扑下 sweeper 实际从未显式提交。

## 2. 期望行为

1. 第二游标与第一游标保持同一方言翻译层：own 模式重新包
   `db_registry._CompatCursor(...)`；legacy 模式继续用主库游标
   （sqlite 原生 `?` / MySQLCompatCursor 自带翻译）。
2. per-seq 失败不得整段回落 bulk：按 seq 逐条降级——换新游标重读该 seq 的
   `agent_tasks` 终态，能级联则级联（回填真实 `result_text`），确实无法
   级联才写 `_no_task_txt`/`_timeout_txt`；失败原因按 seq 记日志，不静默。
   bulk 仅作最后手段（降级也失败的 seq），且 SQL 改占位符绑定文案。
3. 回收护栏以 **worker 进程启动时刻**为参照：启动后 N 分钟
   （`SWEEPER_RESTART_GRACE_MINUTES`，默认 10）内不回收在途事件；
   窗口外僵尸照常回收（不引入漏收）。
4. 修复 `_commit()` legacy 分支自递归，改为 `with db_lock: db.commit()`。

## 3. 验收标准（与 Spec 评论一致）

- [ ] 三库拓扑下不再出现 `per-seq recovery failed ... not all arguments
      converted during string formatting`，不触发 bulk 回落。
- [ ] per-seq 分支正确区分「无关联任务」/「running 超时」/「任务已终态」，
      终态按任务终态级联并回填真实 `result_text`。
- [ ] 重启恢复窗口内在途事件不被回收；窗口外僵尸正常回收。
- [ ] 新增单测钉死三库拓扑 + per-seq 路径；`tests/runtime/test_cleanup.py`、
      `tests/runtime/test_sweep_dual_signal.py` 既有用例不新增失败
      （基线已有失败按 pre-existing 引证，不扩diff修复）。
- [ ] 回落 bulk SQL 使用占位符绑定文案。

## 4. 范围

- 只修 event sweeper 游标方言层 + 回落策略 + 重启窗口护栏 + `_commit()` 自递归。
- task 侧活跃度判据导致的孤儿误杀（#370/#2671/#1495）不在本单范围。
