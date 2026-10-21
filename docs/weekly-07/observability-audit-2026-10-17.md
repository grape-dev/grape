# 可观测性实现审计（2026-10-17）

审计范围：可观测性重构落地后的全部实现（P0 五片 + P1 四片）——数据面、后端接口、前端展示、
后台循环、对主服务其它部分的影响面、测试覆盖盲区。

结论先说：**发现 1 个"静默丢数据"的功能缺陷（已修）、1 类系统性 SQL 方言地雷（已修并加护栏）、
3 个前端显示错误（已修）、4 处"欠考虑"（未修，等你拍板）、性能影响面已实测（数字见下）**。

---

## 一、问题清单（按严重度）

| # | 严重度 | 问题 | 状态 |
|---|--------|------|------|
| 1 | **高（静默丢数据）** | 用户面统一事件视图的**告警来源查询恒抛异常且被吞掉** → 事件列表里 monitor/gap/mutation 全部消失，只剩 task/change，前端无任何报错 | ✅ 已修 |
| 2 | **高（同类地雷）** | SQL 里出现**字面 `%`** + 带参数时，pymysql 会做 Python `%` 格式化 → 报错。全仓有多处 `LIKE 'xxx:%'` | ✅ 已修（兼容层统一逃逸 + 单测） |
| 3 | 中（显示） | 子 Tab 显示原始 key `observability.tab.events`（语言包缺键） | ✅ 已修 |
| 4 | 中（显示） | 事件页状态行显示 `[object Object]`（`I18N.t('observability.ev_state')` 命中的是**命名空间对象**，真实 i18n 会把对象原样返回） | ✅ 已修 |
| 5 | 中（显示） | 任务来源事件的标题把原始 key `observability.rule.failed` / `.completed` 显示给用户（`t(k) \|\| 兜底` 的写法永不生效：真实 i18n 缺键返回 key 本身，是 truthy） | ✅ 已修 |
| 6 | 中（功能缺口） | **token 只记 worker 端**：本机（主控机）跑的引擎、chat agent、devops agent、IM bot 原来全不记（详见 §二.1） | 🔶 **口径已定并落地一半**（见 §六点六） |
| 7 | 中（容量） | `obs_alert_events` 与 `obs_llm_usage` **没有留存策略**，只增不减 | ⬜ 待拍板 |
| 8 | 中（性能/影响面） | 告警求值每 30s **全表扫 `agent_tasks`**（无时间下界），且持全局 `db_lock` 期间跑 59 条 SQL → 实测把其它 DB 使用者的中位延迟从 1.1ms 抬到 9.6ms（约 9×） | ⬜ 待拍板 |
| 9 | 低（UX） | 事件视图**无分页**（固定 `limit=200`，更早的事件看不到）、**不自动刷新**（与日志页一样只在切换时拉一次） | ⬜ 随重设计 |
| 10 | 低（自监控） | 后台循环失败只打日志：连续失败时用户面/管理台都看不到（观测系统自身无观测） | ⬜ 待拍板 |
| 11 | 低（性能） | `trace.timeline()` 每次请求都跑 `stamp_missing()`（`agent_tasks.trace_id IS NULL` **无索引**）；任务表大了就是每次开弹窗一次全表扫 | ⬜ 待拍板 |
| 12 | 低（数据卫生） | `obs_log` 有 2 行（历史遗留）仍是数据库本地时间格式（`2026-10-17 15:43:06`），与 ISO 行混存 → 排序/留存边界失真 | ⬜ 待拍板 |

---

## 二、功能缺口详情

### 1. token 消耗只记 worker 端（用户指出，已核实）

**采集链路只有一条**：`src/runtime/agent.py`（worker 侧运行时 agent）→ `_report_llm_usage()`
→ `client.report_usage()` → `POST /api/runtime/tasks/{task_id}/usage` → `llm_usage.record_usage()`。

- `src/runtime/usage_collect.py` 的模块 docstring 自己写着「引擎侧 LLM 用量账本采集**(worker 侧)**」；
  `UsageCollector` 只在 `src/runtime/agent.py`（1569/1883/1932/1949 行）被使用。
- **主控机上跑的引擎全都没采集**（这些都是 master 进程内直接跑 ACP 引擎的入口）：
  | 入口 | 位置 | 近 7 天量 |
  |---|---|---|
  | chat agent | `src/agent/executor.py::stream_chat_agent` | `chat_messages` 71 条 |
  | devops agent | `stream_devops_agent`（`event_pipeline.py` / `routes/events.py`） | `devops_agent_session` 11 |
  | IM bot | `im/handler_async.py` → `consume_stream_async` | 飞书群消息 |
  | 需求会话 | `issue_agent_session` | 28 |
  | 视频理解 | `im/vod_handler_async.py` | — |
- **实测数据**：台账 `obs_llm_usage` 只有 **9 行 / 7 个任务**（来源全部是 worker 上报：
  `hermes`/`hwcloud`/`opencode`/`report`/`hermes:title_generation`）；而本机 `~/.cache/hermes-acp-homes/*`
  里 33 个 ISO home、118 行引擎账本、**约 774 万 token** —— 引擎账本有数，只是没人来读。
- **影响**：总览的 token KPI、模型占比、用量报表**系统性偏低**，且偏低幅度随 chat/devops 使用增长。
- **修法（可复用现成能力）**：`UsageCollector`/`collect_usage(engine, cwd, session_id)` 与"引擎原生账本"
  的耦合是 cwd/引擎类型，主控侧同样有（同一台机、同一个 ISO home 规则），在这 4 个入口收尾处
  调用即可；难点是**归属键**：chat/devops 运行没有 `agent_tasks` 行 → 需要 synthetic owner
  （`obs_llm_usage` 已预留 `scene_id` / `agent_type` / `user_id` / `engine_id` 列，目前 `scene_id` 基本为空，
  正为此设计）→ 需要定口径（见 §五.1）。

### 2. 留存策略不完整

| 表 | 留存 | 现状 |
|---|---|---|
| `obs_log` | 14 天 | ✅ 有（`logs.cleanup` → `maybe_cleanup` 挂循环） |
| `obs_runtime_samples` | 7 天 | ✅ 有 |
| `obs_metric_rollup` | 分钟 7 天 / 小时 30 天 | ✅ 有（P1-2） |
| `obs_alert_events` | — | ❌ 无清理（每次触发/恢复一行，长期累积） |
| `obs_llm_usage` | — | ❌ 无清理（**台账/计费依据**，删不删是产品决策，但必须显式定） |

### 3. 事件视图的后端静默失败（已修，详见 §四.1）

### 4. 事件视图缺分页 / 缺刷新

- `loadObsEvents` 固定 `limit=200`，没有游标/翻页 → 更早的事件永远看不到（`ev_count` 却显示总数）。
- 事件 Tab 与日志 Tab 一样"只在切 Tab 时拉一次"（`renderObsContent` 里 `if (!isAutoRefresh)`），
  10s 轮询不覆盖它 → **事件流不会自己更新**（对一个"看当下发生了什么"的视图是硬伤）。
- 状态筛选只给了 进行中/已恢复 两个 chip，而后端真实状态集是 `firing/resolved/stuck/failed/done`
  （任务来源的 stuck/failed/done 无法直接筛）。

### 5. 归因链路的两个小账（供参考，不阻塞）

- `obs_llm_usage.scene_id` 大部分为空（9 行里 6 行有值），`agent_type`/`engine_id`/`repo_key`/`user_id` 齐全。
- trace id 回填只在启动跑一次 `stamp_missing(limit=5000)`（✅ 有界），但 `timeline()` 里**每请求再跑一次**
  （见问题 11）。

---

## 三、前端显示问题（3 个已修，附怎么漏掉的）

用真实 i18n 语义（`locales/*.json` + `t/_lookup/_interpolate` 的真实行为）驱动真实 `observability.js`
渲染了 6 个视图（3 分区 × 子 Tab）+ 告警面板 3 个子视图 + 时间线弹窗 + 任务日志弹窗，扫描每个元素的
`innerHTML`，结果：

```
修复前：[inspect/*] #pageContent → rawkey observability.tab.events         （4 个子 Tab 都中）
        [inspect/events] #obsContent → obj@1145: <span class="ts t3">[object Object]<
        [inspect/events] #obsEventBody → rawkey observability.rule.failed / rule.completed
        i18n 统计：缺失 3 键（tab.events / rule.failed / rule.completed）、解析成对象 1 键（ev_state）
修复后：问题清单（无）；140 个键全部正常解析
```

**为什么单测/前端测试没抓到**（这是测试盲区，值得单独记住）：

1. `tests/js/test_observability_sections.js` 的 harness 把 `I18N.t` 桩成 `'«' + key + '»'`：
   - 永远返回**字符串** → `[object Object]`（真实 i18n 返回对象）不可能出现；
   - 缺键时返回的是"带标记的 key"（看起来像正常文案）→ raw key 也看不出来。
2. 该 harness 还**兜住了真实的 i18n 分层**：真 i18n 缺键返回 key 本身（truthy），`t(k) || 兜底` 永不走兜底。
3. 事件 Tab 的渲染入口是 `switchObsTab('events')`；直接调 `renderObsContent(...)`（我第一版审计脚本就是这么写的）
   不会渲染事件表 → 第一轮扫描"零问题"是**假绿**，差点漏掉。

→ **改进建议**（已在本轮顺手做了一半）：把"真实 i18n 语义"的前端审计脚本固化成 `tests/js/`
里的一个用例（见 §六.3），并给 `_obsTOr()` 这种"缺键回退"写法立个规矩。

---

## 四、已完成修复（4 项，含验证）

### 1. 用户面事件视图静默丢数据（问题 1）

`src/observability/alerts.py::list_events` 里拼的是
`dim_key LIKE 'runtime:%'`；SQL 里出现字面 `%` 且带参数 → pymysql `query % args` 抛
`unsupported format character '''` / `not enough arguments for format string`，而这段被 `try/except` 包住
只打了一条 warning：

```
obs list_events failed: unsupported format character ''' (0x27) at index 409
```

→ `list_events` 返回 `[]` → **用户面事件列表里所有告警类来源（monitor/gap/mutation）直接消失**，
只剩任务/变更来源，前端完全看不出来。

- 触发条件：调用者有**可见设备维度**（`visible_runtime_dim_keys` 非空）时才拼出那个 LIKE 分支。
  真机上 liufei 有 2 台可见设备 → 必中；而测试环境里 `REPO` 没有对应设备 → 分支被跳过
  （**这就是 30 个单测全绿却线上坏掉的原因**）。
- 修复：改 `substr(dim_key, 1, 8) = 'runtime:'`（两个后端都支持，SQL 里没有 `%`）。
- 回归用例：`tests/test_obs_alerts.py::test_user_events_survive_visible_device_dims`。
  测试库被 `tests/conftest.py` 强制成 SQLite，**raw SQL 问题在 SQLite 上不会复现**，
  所以用例除了行为断言，还**直接断言拼出来的 SQL 文本里没有字面 `%`**。
  已验证：把修复退回旧写法 → 用例失败；修复后 → 通过。
- 真机验证（MySQL）：插入 1 条仓库维度 + 1 条设备维度真实告警事件后调 L1 接口 →

```
事件总数: 70 | 来源分布: {'gap': 1, 'monitor': 1, 'task': 60, 'change': 8}
  ✓ gap      runtime_silent  level=important state=firing  dim=runtime:4
  ✓ monitor  task_stuck      level=warning   state=firing  dim=repo:gitcode:requiredsa/hello-flask
list_events failed 计数: 0
```

### 2. 字面 `%` 地雷的系统性护栏（问题 2）

`src/mysql_compat._replace_qmarks`（MySQL 侧把 `?` 换成 `%s` 的地方）现在把**非占位符的字面 `%` 逃逸成 `%%`**：

- 字符串字面量内一律逃逸（`LIKE 'github:%'` → `'github:%%'`；pymysql 格式化后还原成单个 `%`）；
- 字面量外的 `%s` / `%(name)s`（`src/admin/cooldown.py`、`src/featured_order.py` 直接写了原生占位符）原样保留；
- 无参语句同样逃逸 —— `LIKE 'a:%%'` 与 `LIKE 'a:%'` 在 SQL 里语义等价（通配符个数不影响匹配）。
- 单测：`test_literal_percent_is_escaped_for_pymysql`（含"模拟 pymysql 的 `t % (5,)`"一步）、
  `test_native_percent_s_placeholder_untouched`、`test_literal_percent_without_params_is_equivalent_like`。
- 残余边界（已在 docstring 写明）：`?` 式语句里若写字面 `%s`（如 `LIKE '%s%'`）不会被逃逸 —
  请用 `substr/instr` 代替。

### 3. 三个前端显示 bug（问题 3/4/5）

- 语言包补 `tab.events`（事件 / Events）；新增标量 `ev_state_label`（状态 / State）替代误用的命名空间对象。
- 新增 `task_state.{failed,completed,stuck}`，任务来源事件标题改走它。
- 顺手引入 `_obsTOr(key, fallback)`：**值 === key 视为未命中**再回退，修掉 `t(k) || 兜底` 这个永不生效的写法
  （真 i18n 缺键返回 key 本身）。`_obsEventTitle` 已改用它。

---

## 五、性能与影响面（实测，本机 + 远程 MySQL）

方法：进程内计时 + 包装 `session_manager.db` 的 cursor 计数 SQL；并发干扰用一个后台线程
反复跑 `SELECT id,status FROM agent_tasks WHERE id=1`（走同一把 `db_lock`），对比空闲窗口的延迟分布。

### A. 后台循环单次开销（都跑在 `asyncio.to_thread` 里，不阻塞事件循环）

| 循环 | 频率 | 单次耗时 | SQL 条数 | 锁获取 |
|---|---|---|---|---|
| 告警求值 `evaluate_rules` | 30s | **34~40 ms** | **59** | 8 |
| 数据缺失 `gap.probe_all` | 30s | 5 ms | 11 | 3 |
| 指标聚合 `metric_rollup.catch_up` | 60s | **21 ms**（稳态；回填时 180 桶 ≈ 720ms/tick） | 9（回填 900） | 7 |
| 设备采样 | 60s | （既有） | — | — |

→ 稳态总占用约 **66ms / 60s ≈ 0.1%**，量级安全。**首次回填**（重启后补 24h）实测单桶 4ms/5 条 SQL，
1440 桶分期跑完约 5.8s，分摊到 8 分钟，可接受。

### B. 请求路径（预热后中位）

| 接口 | 中位 | 首调（冷） | SQL 中位 |
|---|---|---|---|
| `GET /api/obs/overview?range=24h`（首屏默认） | **12 ms** | 337 ms | 14 |
| `overview 1h / 7d` | 11 / 12 ms | — | 14 |
| `GET /api/obs/events`（事件 Tab） | 30 ms | 43 ms | 47 |
| `GET /api/obs/logs`（日志 Tab） | 7 ms | 7 ms | 14 |
| `trace.timeline`（时间线弹窗） | 5.5 ms | — | 12 |

→ P1-2 的聚合表生效后，总览趋势不再扫任务表：**24h 从 87 条 SQL / 321ms（改前实测）降到 14 条 / 12ms**。
冷启动那次 337ms 是一次性的 schema introspection（兼容层把 `PRAGMA table_info/index_info` 翻成
`information_schema` 查询且无缓存，`user/db.py` 的可见性链路每请求会重复发几条）。

### C. **并发干扰（最值得关注的一条）**

| 干扰负载 | 探针中位 | p95 | 最大 |
|---|---|---|---|
| 空闲基线 | 1.07 ms | 1.42 ms | 8.4 ms |
| **跑 `evaluate_rules`（30s 循环的真实负载）** | **9.64 ms（≈9×）** | 19.2 ms | 32.7 ms |
| 跑 `catch_up` | 1.78 ms | 8.6 ms | 30.5 ms |
| 跑 `build_overview(24h)` | 1.88 ms | 3.3 ms | 58.9 ms |

原因：`db_lock` 是**进程级** `threading.RLock`，主服务所有 DB 访问串行化；告警求值在锁内跑
59 条 SQL（其中一条是全表扫 `agent_tasks`）。

**根因 SQL**（`alerts.py::_repo_metrics`，每 30s 一次）：

```sql
SELECT repo_key, status, started_at, completed_at FROM agent_tasks
 WHERE status IN ('queued','dispatched','running','completed','failed','cancelled')
   AND repo_key IS NOT NULL AND repo_key != ''
```

`EXPLAIN`：`idx_agent_tasks_status` 走 range 但 **rows=318、filtered=100%** —— 等于全表读进 Python。
任务表按现在 315 行的量级无感，**但这是唯一一条会随任务量线性变慢的循环**（30s 一次，10 万行时
每次拉 10 万行）。建议：加时间下界（卡住看 `started_at` 下界、失败率看 `completed_at` 窗口内）+
在 SQL 里 `GROUP BY repo_key` 聚合，别把行捞进 Python。

### D. 日志 bridge（P0 加的）开销

`redact()` 单次 **5.0 μs**；handler 只做异步入队（有界队列、满则丢弃），主链路不受落库影响 ✅。

---

## 六、测试盲区（这次两个事故都由此漏过）

1. **测试库被强制成 SQLite**（`tests/conftest.py: os.environ["DEVOPS_DB_BACKEND"]="sqlite"`）
   → 所有 MySQL 方言问题（本次的字面 `%`、上一片的复合 UNIQUE `ON CONFLICT` 漏翻）**单测天然盖不到**，
   只能靠"兼容层单测 + 真机验证"兜。建议：兼容层关键分支保持纯函数单测（已做），
   并在 `docs` 里把"SQLite 单测 ≠ 生产可用"写成硬规矩。
2. **前端 harness 把 i18n 桩成返回字符串** → 缺键/对象返回两类显示 bug 永远测不出来。
   建议把本轮的"真实 i18n 审计"固化成一条 node 用例（扫 3 分区 × 子 Tab + 面板 + 弹窗的
   `innerHTML`，断言无 `observability.` 原文、无 `[object Object]`）。
3. **假绿模式**：入口调错（直接 `renderObsContent` 而不是 `switchObsTab`）会让审计脚本扫到空页还报"零问题"。
   写法上要断言"确实渲染出了目标元素"（本轮脚本已加 `runtime:` 之类的存在性断言）。
4. **测试库是共享的 SQLite 文件**（`data/test_sessions.db`，会话开始先删库重建）。
   审计过程中我自己踩了一次：上一轮 pytest 没退干净（或杀掉不彻底）时开新的一轮，
   两轮共用同一个库文件 → 出现**成片假失败**（同一批文件一次 9 failed、干净重跑 2 failed）。
   规矩：**pytest 串行跑，杀进程后确认没有残留再开下一轮**；判读失败前先确认没有并发轮次。
5. **两条既有失败**（与本轮改动无关，已在审计前的树上复现）：
   - `test_mysql_compat.py::TestConfig::test_get_db_backend_env[-sqlite]`：环境相关（本机配了 mysql 配置时
     该断言取到 mysql）——时好时坏；
   - `test_mysql_compat.py::TestTranslateSql::test_delete_not_in_same_table_wrapped`：
     用例期望 `DELETE FROM \`tasks\`` 而脚本里的表名是 `issue`（用例写错了表名，翻译逻辑本身没问题）。

**回归结论（干净串行复跑，HEAD vs 审计前 3e7c9af40c，同样 10 个文件）**：
审计前 `1 failed, 184 passed` → 本轮 `2 failed, 187 passed`（多出的 3 个用例是我新加的，2 个失败即上面两条既有失败）。
`tests/js` 全量：34 个文件 8 个失败，与既有基线一致（未改动的 app.js 相关 2 个、mocha 风格 1 个、
`test_observability_page_size_162` 在审计前的树上同样 8 passed/7 failed）。

---

## 六点五、2026-10-17 拍板后已落地（#2/#3/#4）

用户决定：**事件留存 90 天 / 台账年度归档 / 告警求值降载现在就动 / 循环自监控暴露到 /metrics 与管理台**。

| 项 | 实现 | 证据 |
|---|---|---|
| **告警求值降载** | `_repo_metrics` 由「全表捞进 Python」改为 **3 条 GROUP BY 聚合**；`evaluate_rules` **整轮预取**开放事件（1 条查询替代原来的 每(规则,维度) 1 条 + 每规则 1 条策略扫描），`_open_event/_resolve_open` 支持 `cache`（gap 探测共用同一份）；口径与逐行版**逐项钉死等价** | `evaluate_rules` **59 条/34~40ms → 19 条/13.6ms**（稳态）；`test_obs_retention.py::test_repo_metrics_*` 两条用例把卡住/排队/失败率/突变两窗口的数值板上钉死 |
| **事件留存 90 天** | `alerts.cleanup_events(days=90)`：**只删 `status='resolved'` 且过期的**（仍 firing 的再老也留——它是"还在告警"的唯一依据）；挂告警循环每约 1 小时一次 | 单测 + MySQL 真机演练：删 1（400 天前已恢复）/ 留 2（近期已恢复、仍 firing） |
| **台账年度归档** | 新表 `obs_llm_usage_archive`（结构镜像、dedup_key 不唯一、不带原 id）+ `llm_usage.archive_old_usage(keep_days=365)`：**搬不删**，分批 2000 行；时间戳**本地串/ISO 双格式**都处理（台账 `created_at` 是 `datetime('now')` 本地格式，这是审计踩过的坑）；列集合用两表 `PRAGMA` 交集，以后加列自动跟上；挂 rollup 循环每天一次 | 单测两种格式各一条 + MySQL 真机：本地格式 400 天行搬家成功、2 天行保留、重复跑幂等 |
| **循环自监控** | 新模块 `src/observability/loops.py`：每循环 beat/fail → ① `/metrics`：`grape_obs_loop_{runs_total,failures_total,consecutive_failures,last_success_unixtime}{loop}`；② **L2 `GET /api/admin/obs/loops`**（`require_admin`，返回 loops/unhealthy/count）；已在告警/聚合/设备采样三个循环接线 | 真机 `/metrics` 实测三循环计数与 `consecutive_failures=0` 在涨、`last_success_unixtime` 在更新；L2 无 token 401（单测覆盖鉴权与快照） |

顺手修掉一个**真实的资源泄漏**：lifespan 关闭时 `_lease_sweep/_sweep/_probe/_reap/_knowledge_write` 都 cancel 了，
但**新增的三个可观测性循环没有 cancel** → 任何在本进程启动过 app 的场景（含 pytest 里的 `TestClient`）
都会把它们留在后台继续读写同一个库。已补 cancel 并加注释。

### 关于测试基建的两条新教训（这轮踩了两次）

1. **`with TestClient(app)` 会跑 lifespan**：种规则（把 `task_stuck.for_sec` 重置成 300）、起后台循环 →
   污染后续用例（批量跑时 `test_obs_alerts` 成片失败，单独跑全绿）。需要"只测路由/鉴权"时**不要带 `with`**
   （不带 with 就不跑 lifespan）。
2. 前一轮"成片假失败"的完整原因 = 上面两条叠加：**没 cancel 的循环** + **共享 `data/test_sessions.db`**。

## 六点六、token 采集口径：决定 B 与落地进度（2026-10-17）

用户选了 **B：加 `run_id` + `run_kind`，`task_id` 只留给 worker 任务**。

**已落地（摄入侧 + 两个入口家族）**

| 内容 | 说明 |
|---|---|
| 表结构 | `obs_llm_usage` / `obs_llm_usage_archive` 新增 `run_id TEXT DEFAULT ''`、`run_kind TEXT DEFAULT 'task'`；索引 `idx_obs_llm_run(run_kind, run_id)` |
| 幂等键 | `run_kind='task'` **沿用老格式** `{task_id}:{seq}`（升级后 worker 重报仍命中老行，不翻倍）；其它 `{run_kind}:{run_id}:{seq}` |
| 写入 API | `record_usage(..., run_kind=, run_id=, attr=)`；非任务运行的归属由 `attr` 传入（**仅内部调用**，不走 HTTP 上报口） |
| 读取 API | `task_usage()` 只出 task 行；新增 `run_usage(run_kind, run_id)`、`usage_by_kind(since)`；`summarize(run_kind=)` 可按类型出图 |
| 采集封装 | 新模块 `src/observability/master_usage.py`（`MasterRun` / `new_run_id` / `wrap_generator`），复用 worker 侧同一个 `UsageCollector` + `_usage_payload` |
| 已接线（全部） | ① **chat 三个本机直连入口**（`agent_fn` / 非流式 `run_agent` / SSE `agent_worker`）；② **IM 直连**（`im/handler_async.py::_direct_exec`）；③ **事件流水线 / autopilot 三个 DIRECT 入口**（`api/event_pipeline.py` 主线、`api/routes/events.py` 事件对话重跑、`runtime/scheduler.py::_execute_autopilot_direct`）；④ **VOD Bot**（`im/vod_handler_async.py`）。原 worker 侧采集不变 |
| devops/流水线的特殊口径 | 这类运行**有 `agent_tasks` 行**（pipeline 自己建）→ `for_task()` 直接按 `run_kind='task'` 记：归属从 agent_tasks **自动反查**（不用传 attr），幂等键 `{task_id}:{seq}` 与 worker 侧**完全一致** → 同一任务先本机跑、后重跑到 worker 也不会重复计。只有无 task 行的 `d_<seq>` 兜底路径才落 `run_kind='devops'` + attr |
| 口径修正 | 总览/rollup：**有 `repo_key` 的行进仓库维度；没有的（例如非仓库会话的 chat）只进 `global`** —— 之前 rollup 直接把这些行丢掉了，全局 token 会漏 |
| 安全性 | 非任务运行的 `_mu` 全部**判空后再 finish**（`_mu` 在 try 内创建 / 只在 DIRECT 分支创建，前置步骤失败时不能炸在收尾行上）；finish 一律放在用户可见动作之后（回复落库、SSE 结束信号、飞书卡片） |

**踩到的两个坑（已修）**

1. **索引建在加列之前**：初始化里那批 `CREATE INDEX` 跑在列迁移之前，老库上 `CREATE INDEX ... (run_kind, run_id)`
   会因为列不存在而失败（实测：新列有了、索引没建成）→ 索引创建挪到 `_ensure_column` 之后再补一次。
2. **采集器记录形态**：`_usage_payload` 吃的是 `input`/`output`/`cache_read`/`reasoning` + `_source_suffix`
   （不是 `input_tokens` 那套），单测夹具必须照着采集器的真实输出写，否则字段全 0 而"看起来写成功了"。

**证据**：`tests/test_obs_llm_usage.py` 25 例全过（新增 5：task 行保持老幂等键、非任务行分型落库+幂等、
无主体不落库、MasterRun 写 chat 行并保留 `hermes:title_generation` 分账、`new_run_id`）；
真机 MySQL：两表新列与 `idx_obs_llm_run` 就位，历史 9 行按 `run_kind='task'` 正常；chat 相关 8 个测试文件 61 例全过。

## 七、建议的处理顺序（等你拍板）

1. **（建议立刻）** 已修的 4 项提交（含回归用例）。
2. **（建议下一步）** 事件视图重设计 —— 见 `observability-events-view-redesign.md`（含分页/刷新/列语义）。
3. **token 采集补齐**：确定 chat/devops/IM 的归属口径（scene_id 还是 synthetic task_id）后动手；
   这是"用量数据是否可信"的前提，越早越好。
4. **留存策略**：`obs_alert_events` 定 90 天；`obs_llm_usage` 明确"永不删（台账）"或"归档到年度表"。
5. **告警求值降载**：时间下界 + SQL 侧聚合（顺手把锁内 SQL 条数从 59 降到个位数）。
6. 小项：`trace.timeline` 不再每请求回填（或加 `trace_id` 索引）、`obs_log` 那 2 行格式归正、
   循环失败计数暴露到 `/metrics` 与管理台。