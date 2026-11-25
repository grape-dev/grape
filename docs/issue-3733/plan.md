# Issue #3733 方案设计

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-22

## 方案要点

保留「空指令不派发」的拦截语义不变，改造「拦截的表达方式」：**不可派发状态以库内为准**
（跨进程可见、跨重启可见），进程内内存只做「省掉 DB 往返」的节流；界面读接口出参展示原因。

### 1. 数据面：状态三列 + 一次性自检

`autopilot_tasks`（mgmt 域）新增：

| 列 | 类型 | 语义 |
|---|---|---|
| `dispatch_state` | `TEXT NOT NULL DEFAULT ''` | `''`=正常可派发；`blocked_empty_instruction`=已启用但不可派发（指令为空） |
| `dispatch_state_reason` | `TEXT NOT NULL DEFAULT ''` | 人类可读原因（直接进界面） |
| `dispatch_state_at` | `TEXT NOT NULL DEFAULT ''` | 状态进入时间（ISO，UTC） |

- 补列沿用既有机制：`src/api/db_setup.py` 的 `_ensure_column()` + `register_migration(..., domains=("mgmt",))`（先例 `202611170046` autopilot engine_runtime_uids）；
  **实现落在本域** `src/domains/mgmt/migrations/autopilot_dispatch_state.py`（`db_setup` 只留接线，
  先例 `202611172020` issue_ident_columns）。两条理由：① `autopilot_tasks` 归 mgmt
  （`configs/table_ownership.json`），补列必须由 owner 域执行；② 含表名的 UPDATE 留在 api 层会被
  `tests/arch/test_foreign_table_refs.py` 判成新增跨域引用（`autopilot_tasks←api`）破棘轮。
- 同一条迁移内做**存量一次性幂等自检**：
  `UPDATE autopilot_tasks SET dispatch_state=:blocked, dispatch_state_reason=:reason, dispatch_state_at=:now
   WHERE enabled=1 AND TRIM(instruction)='' AND dispatch_state<>:blocked`
  —— `dispatch_state<>` 谓词使重复启动零改动（幂等），且**只标记不改 `enabled`**。
- 迁移版本号 `202611240007`（原拟 `202611220001`，基线占用让位；二次取 `202611240002` 后基线合入 #3789 同号；再取 `202611240003` 后基线 #3763 的 create-autopilot-executions-table 已占用同号；`202611240004` 又先后被 #3846 沙箱表 / #3928 后端能力列占用，最终顺延到 `202611240007`；已过 `test_migration_track_guard` 的撞号/格式守卫）。

### 2. 跨进程权威 + 进程内节流（日志与 tick / 副本解耦）

- 新写原语 `set_autopilot_dispatch_state_any(task_id, state, reason)`：
  `UPDATE autopilot_tasks SET dispatch_state=?, dispatch_state_reason=?, dispatch_state_at=?, updated_at=?
   WHERE id=? AND dispatch_state<>?` → 返回 rowcount，**0 行 = 状态未变更**。
  并发副本同时发现空指令时，行锁让只有一个 UPDATE 命中（rowcount=1），其余 0 → 只有一处 WARNING。
- `_get_enabled_tasks()` 跳过分支：
  1. 先看本进程内存 `_NOT_DISPATCHABLE_SEEN[task_id]`，命中则直接跳过 DB（省往返、不刷屏）；
  2. 未命中 → 调写原语；`rowcount>0`（真正的状态变更）才 `log.warning` 一条，其余走 `log.debug`；
  3. 内存按 `(task_id, state)` 记忆 —— 但它**只在行内状态已经是目标状态时才生效**，
     避免「用户补齐指令后又清空」被本地内存挡住重新标记。
- 稳态：库内已为 blocked 后，后续 tick 全部 `log.debug`，Loki 该 WARNING 归零。

### 3. 手动触发路径

- `trigger_task_now()` 在**建协程之前**判空指令：写状态（幂等）+ 返回
  `{"ok": False, "message": "<明确原因>"}` → 路由照现有口径转 409，界面 toast 显示原因；
  此路径**不建 execution、不写 last_run_at**（都在 `_execute_task` 之后）。
- `_execute_task()` 保留同语义二次守卫（其它调用方），同样只写状态 + 留痕一次，不建 execution。

### 4. 接口与界面

- mgmt 域 `public`：列白名单 `_TABLES["autopilot_tasks"]`、可移植查询
  `autopilot_tasks_enabled_full` / `autopilot_task_full_row` 追加三列（**追加在末尾**，
  既有下标解包 r[0..12] 不变），并导出新写原语。
- `routes/autopilot.py`
  - `_row_to_task` 出参新增 `dispatch_state` / `dispatch_state_reason` / `dispatch_state_at` /
    `dispatchable`（**由当前 instruction 直接派生**，不依赖调度器是否跑过，接口永远说真话）；
  - `PUT /tasks/{id}`：提交非空 instruction 时清空三列（恢复派发），并在响应体提示；
  - `GET /tasks` / `GET /tasks/{id}` 的 SELECT 列序同步追加三列。
- `AutopilotView.vue`
  - 列表状态列：**已启用**且不可派发时显示 ⚠ 徽标（title=原因），启停圆点照旧保留；
  - 详情：顶部一条 `apv-blocked` 提示条（原因 + 「补齐指令」按钮 → 复用 `editTask`），同样只在启用态出现
    （暂停的任务本来不派发，不该常年挂告警——否则「已启用但不可派发」文案自相矛盾）；
  - 词条进 `shell/locales/{zh,en}/autopilot.json`，不硬编码中文。

### 5. 测试 seam

| 切片 | seam（用户可见行为） | 用例 |
|---|---|---|
| 调度器跳过 | `_get_enabled_tasks()` 返回值 + 库内 `dispatch_state` + 日志条数 | 多 tick 单条 WARNING、状态落库、指令非空不写状态 |
| 手动触发 | `trigger_task_now()` 的 `{"ok": False, "message"}` + `_execute_task` 副作用计数 | 零 execution、零 last_run、拒绝原因明确 |
| 迁移自检 | 直接对临时 sqlite 库跑迁移函数 | 幂等（二次零改动）、只标 enabled+空指令、不改 enabled |
| 接口出参 | `_row_to_task` 返回 dict | `dispatchable` 派生正确 |

### 6. 关键决策

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 存量行 | 置 `enabled=0` / 显式状态 | 显式状态 | 停用会掩盖「已启用却在空转」；补齐即恢复 |
| 去重位置 | 仅内存 / 库内权威 + 内存节流 | 库内权威 + 内存节流 | 240 条/h ≈ 4 副本 × 60s tick，仅内存去重按副本残留噪音 |
| 留痕时机 | 每次跳过 / 状态变更 | 状态变更 | 稳态 Loki 归零，且不丢「首次不可派发」这个信号 |
| `dispatchable` 口径 | 读库内状态 / 由 instruction 派生 | 由 instruction 派生 | 调度器未跑过时接口也不能说谎 |

## 复审整改（PR #1956 第二轮 QA 审查）

| 级别 | 问题 | 整改 |
|---|---|---|
| 🟠 Required | 补列 `_ensure_column` 只传 `allow_copy=True`：INSTANT/INPLACE/COPY 全失败或被域归属守卫跳过时只记人工清单并**正常返回**，执行器约定「fn 正常返回 = 盖章」→ 列没建成、版本号盖死、下次启动不再重试；而 `list_enabled_autopilot_tasks_any` 已显式 SELECT 这三列，缺列即 1054，调度器每 tick 抛错被兜底吞成 `Autopilot scheduler loop error`，**全部 autopilot 任务一起停派** | `backfill_dispatch_state` 补列改为 `allow_copy=True, strict=True, raise_on_guard=True`（对齐 `db_setup.py:10923` / `11051`）：补列失败/被守卫跳过一律抛错 → 不盖章，下次在属主域重试。用例 `test_backfill_requires_strict_column_add` 锁住调用参数，防回归静默盖章 |
| 🟡 Optional | 指令经**非 PUT 路径**补齐（直接改库、旧版本回滚后写入、其它写入方）时状态列不联动：出参 `dispatchable=true` 但 `dispatch_state_reason` 仍是「任务指令为空」，自相矛盾且长期残留 | 读侧归一化：`_row_to_task` 在 `instruction` 非空时把 `dispatch_state` / `dispatch_state_reason` / `dispatch_state_at` 三列同源归零（库内残留值不动，仍由调度器/迁移作判重权威）。用例 `test_row_to_task_normalizes_stale_blocked_state_when_instruction_present` |
| 🟡 Optional | 新增 3 组 pytest + 1 个 node 用例未接入 `.github/workflows/windows-smoke.yml` 门禁，回归只能靠人工 | linux-backend 新增 step「Autopilot not-dispatchable state regression tests (issue #3733)」，跑三组 pytest + `node tests/js/test_issue_3733_autopilot_not_dispatchable.js` |
| 🟡 Optional | PR 描述「迁移未跑或旧库列缺失时接口也不说谎（列缺失按可派发降级）」只覆盖**行长**：list/get 的 SELECT 已显式引用三列，真实缺列直接 1054，走不到 18 列兼容分支 | 口径修正为「**行长度兼容**（旧调用方/单测传 18 列旧行不抛 IndexError）」；真实缺列由 Required 修复后的补列迁移在启动期抛错拦住，不带缺列对外服务 |
| ⚪ Nit | 新文件末行无换行；迁移模块 `MIGRATION_VERSION` / `MIGRATION_NAME` 与 `register_migration` 字面量双份维护；`run()` 的 cursor 未关闭 | 补末行换行；删除模块内未引用的两个常量（版本号只由 `db_setup.py` 注册处持有——静态守卫 `tests/api/test_migration_track_guard.py` 只解析该文件的模块级字符串字面量，双份维护会静默漂移）；`run()` 用 `try/finally` 关闭 cursor |
