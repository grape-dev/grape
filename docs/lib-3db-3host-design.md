# Grape 三库三机 · lib 模式收口设计方案

> 版本：v2（2026-11-14 复核：全页数字按实测重算，口径与复算命令见**附录 D**）
> 数字复核基线：代码 commit `c824067b8`（2026-11-14 11:07 并入 `arch-weekly-11`，双主同 HEAD）。
> **不要用「本文 HEAD」判断数字时效** —— 本文件自身改动不改变基线；每条数字的复算命令在附录 D。
> 本文只做设计，不含执行。执行前需按 §9 逐条拍板。

---

## 1. 目标与不变量

### 1.1 目标态（Definition of Done）

| 维度 | 目标 |
|---|---|
| 库 | `grape_dev_mgmt` / `grape_dev_runtime` / `grape_dev_insight` **各在一台独立主机**，互不跨库引用 |
| 访问 | 每个进程**只直连本域库**；跨域读走 `*_lib()` 命名查询白名单；跨域写走「目标域 public 原语 + 幂等意图 + 排水补偿」 |
| 视图 | 三库里**零跨域视图**（`information_schema.VIEWS` 中不存在指向其它 schema 的定义） |
| 补偿 | 三个域各自有排水单元（mgmt `:00` / runtime `:02` / insight `:04`）+ 每日对账归档 |
| 可回滚 | 任一时刻可通过环境开关退回「同实例 + 视图」形态，且不需要改代码 |

### 1.2 硬不变量（任何阶段不得破坏）

1. **同域写必须同事务**：`foreign_only=True` 守卫保证本域写不走 lib 连接（否则丢原子性）。
2. **双主同 HEAD**：0007 / 0001 的 `<deploy-root>` 任一时刻同 commit；nginx `ip_hash` 按 IP 钉机，两台行为必须一致。
3. **只展示一次的副作用禁止入意图队列**：建凭据 token、吊销凭据必须**同步直连**（重放会造孤儿/留窗口）。
4. **组合写收成一个原语**：探测即快照（`save_agent_runtimes`）、账号级联（`purge_user_rows`）、吊销级联（`purge_runtime_credential`）不得拆成多条意图。
5. **意图表有上限**：`applied` 行按 7 天归档，`dead` 行必须人工处理，不得静默堆积。

---

## 2. 现状盘点（实测，非估算）

### 2.1 库与视图

| 项 | 实测 |
|---|---|
| 三库位置 | **全部在 `<dev-db-host>` 同一实例**（`grape_dev_mgmt` / `grape_dev_runtime` / `grape_dev_insight`） |
| 跨域视图 | **252 个**：mgmt 46（→runtime 36、→insight 10）；runtime 91（→mgmt 81、→insight 10）；**insight 115（→mgmt 81、→runtime 34）**。旧口径「137」只算了 mgmt+runtime，漏掉 insight 的 115 个——insight 要迁回独立主机，这 115 个同样必须退役 |
| 视图可更新性 | mgmt 侧 runtime 表均为 `IS_UPDATABLE=YES` → 今天跨域写"能用"全靠视图 |
| insight 库 | `<dev-db-host>` = **17 基表 + 115 视图（共 132）/ api_latency_log 6423 行**（实库，本轮实测）；`<insight-copy-host>` = 14 表 / 5125 行（旧副本，2026-11-13 实测、本轮未复核） |
| runtime 副本 | `<runtime-copy-host>` 存在一份 `grape_dev_runtime`（**45 表**、无 `xwrite_log`；历史值、本轮未复核，且该副本无视图），与 `<dev-db-host>` 是否同步**未验证** |
| 多份基表 | `takeover_sessions`（mgmt 基表 0 行 + runtime 空表 0 行）、`grape_t_channel_installation`（runtime 11 行 + mgmt 0 行）、`grape_t_channel_lease`（mgmt 12 行 + runtime 11 行） |

### 2.2 代码改造进度

> **2026-11-14 重测口径（本节数字均以此为准）**：旧版本把 P2 记作「读路径完成」并把剩余读记作
> 114 处、视图记作（已作废的）137 个，两处都与实测不符——P2 只完成机制与约 31 处接线，
> 视图实为 252 个（含 insight 115）。判据：`p4_read_converter.py` 重扫 +
> 逐候选函数内 lib 句柄判定（converted / bare），历史时点用 `git worktree` 回放。

| 项 | 数量 |
|---|---|
| 跨域写改造 | **已完成**（P3-4b/5a/5b/5c/5d/5f/5g/5h 全批次；**33 个提交**，`2f70a0d3e..a3a18f49c`） |
| 读改造已落地 | **202 处接线点**（其中「81 处」是 11-14 00:23–01:15 的 **11 个提交**补的，`670f7709b..3f241628a`，**属 P4 期，不属 P2**） |
| P2 实际产出 | **约 31 处接线**（`436e3299f` 收尾时实测：导入 31 / None 守卫 36）；同期重扫仍有 **208 处裸跨域读**。计划给 P2 的验收「跨域读引用降为 0」**未达成**，P2 应记作**部分完成（仅机制+原语面）** |
| 重扫剩余跨域读 | **114 处 / 20 张表**（最大 `agent_tasks` 25）= **104 处裸读（真欠账）** + **10 处已接线但本地兜底 SQL 仍在（口径噪声，全在 `insight/routes/dashboard.py`）**；**auto 仅 16（自检绿 8）**，manual **98**（消费形态 68 + 动态 SQL 30） |
| 剩余裸读宿主分布 | insight **52** ／ mgmt **40** ／ runtime **11** ／ `src/api` **1**；热点：`insight/routes/dashboard.py` 17、`mgmt/user/db.py` 13、`mgmt/routes/settings.py` 11、`mgmt/routes/skills.py` 8 |
| 可自动改写 | **auto 16 / 自检绿 8** —— 改写器红利已耗尽，其余须手工 |
| manual 待设计 | **98 处**：动态 SQL 30 + 非 fetch 消费 68 |
| 计划估算作废 | 旧计划 P2 的「runtime 190+100 处 / mgmt 97 处」是 P0 边界扫描按 **SQL 文本** 估的，与真实**调用点**（227~238）口径不同，**不要再用它算进度** |
| 验证器 | `p4_verify_all.py`（三环境跑，FAIL/UNCOVERED 分离）；当前 FAIL=0（PASS 67+31），UNCOVERED 7（均为参数构造不出，非遗漏） |
| A3 门禁 | `scripts/check_lib_query_names.py`：419 文件，mgmt 14 函数+35 查询 / 注册 41+35；runtime 25+65 / 注册 68+70 → **PASS** |

### 2.3 运行面

| 项 | 状态 |
|---|---|
| 四进程 | `grape-{server,event,worker,insight}-server`，双主 8 端口 200 |
| lib 开关 | 双主四进程均已开（`GRAPE_DOMAIN_LIB=1` + 三域 DSN；**本域读开关=0**、外域=1） |
| insight 进程 | **已开 lib 开关**（`GRAPE_DOMAIN_LIB=1` + 域内读=0、外域=1）；早期版本曾记载"未开"，已过期 |
| 排水单元 | 双主各 **3 个排水 timer** active，错峰 `:00 / :02 / :04` |
| 对账 | `grape-xwrite-reconcile.timer` 每日 03:20，归档 7 天前 applied（**与 3 个排水合计 4 个 timer/机**） |
| 意图量 | mgmt **5593** / runtime 84 / insight 1（全 `applied`；`pending`、`failed`、`dead` 均为 0）。mgmt 远大于早期记录的 45，原因是 `unique_key` 改造后**按调用记账**（15s 心跳等热路径），非异常，由每日归档收敛 |

---

## 3. 库访问主体清单（五类）

### 3.1 主体（进程）

| 主体 | 进程域 | 直连库 | 跨域读 | 跨域写 |
|---|---|---|---|---|
| `grape-server` | mgmt | mgmt | runtime / insight（lib 读） | runtime / insight（原语+意图） |
| `grape-event-server` | runtime | runtime | mgmt / insight | mgmt / insight |
| `grape-worker-server` | runtime | runtime | mgmt / insight | mgmt / insight |
| `grape-insight-server` | insight | insight | mgmt / runtime | mgmt / runtime |
| 排水进程 ×3 | 各自域 | 本域（读意图） | 目标域（执行补偿） | — |
| 对账进程 | 无 | 三域（只读+归档） | — | — |

### 3.2 资源（域库）

`mgmt`（**91 基表 + 46 视图**）· `runtime`（**52 基表 + 91 视图**）· `insight`（**17 基表 + 115 视图，共 132**）

### 3.3 操作

| 操作 | 允许主体 | 通道 | 幂等性要求 |
|---|---|---|---|
| 本域读写 | 本域进程 | 直连（同事务） | 由业务 SQL 保证 |
| 跨域读 | 任意进程 | `*_lib().read_one/read_all`（白名单） | 读天然幂等 |
| 跨域写（普通） | 任意进程 | `submit()` + 意图 + 排水 | **必须可重放** |
| 跨域写（一次性副作用） | 任意进程 | 同步直连原语 | 不可重放，禁止入队 |
| 组合写（多表/级联） | 任意进程 | 单个组合原语（独立事务连接） | 按快照参数重放 |

### 3.4 关系范围（隔离边界）

- 域间**无外键**（跨库外键不存在）；关联只靠全局唯一标识（uuid），不靠裸自增 id。
- 跨域读**只能**走白名单查询名；未知查询名 `KeyError`（不给远端开任意 SQL 口子）。
- 跨域写**只能**走已登记原语；意图表按域独立，不得跨域写意图行。

### 3.5 策略与审计

- 每笔跨域写留痕：`grape_t_xwrite_log(action, target_domain, payload_json, status, attempts, last_error)`。
- 排水失败 → `failed`（≤5 次）→ `dead`（人工）；对账作业每日汇总，`dead>0` 时 timer 标红。
- 归档口径：`status='applied' AND updated_at < now-7d`，分批 500 删除。

---

## 4. 剩余工作分解（六个工作流）

### W1 读路径 lib 化（104 处裸读；另 10 处兜底噪声可顺带清）

**前置**：无（可立即开始）。
**步骤**：
1. 重跑 `p4_read_converter.py` → 真实候选清单（旧计划已失效，69/89 锚点不再匹配）。
2. 重跑 `p4_read_rewriter.py` → auto/manual 分类 + AST 自检。
3. auto 批次：临时副本编译 → 落地 → 登记 → 整文件编译 → `p4_verify_all.py`（0001 三环境）→ FAIL=0 才 push+重启。
4. 每批上限 20 处（实测产能），按表推进，单轮只碰一张表。

**验收断言**：
- `p4_verify_all.py --domain {runtime,mgmt}` 在 `grape-server` / `grape-worker-server` 两个环境均 **FAIL=0**；
- 代码里引用的查询名 ⊆ 注册表（脚本：`used_names - registry_names == ∅`）；
- 双机 `py_compile` 全绿 + 8 端口 200 + **4 个 timer active**（3 排水 + 1 对账）。

### W2 动态 SQL 与非常规消费（98 处 = 改写器归为「动态 SQL」30 + 非 fetch 消费 68）

**前置**：口径裁定（§9-D1 / D2）。
**分类与做法**：

| 形态 | 数量 | 建议做法 |
|---|---|---|
| 动态列名（`{mapping_col}`） | **1** | **按列枚举**：列集合有限（feishu_open_id / gitcode_user / gitee_user…），每列登记一条查询 |
| 动态 IN 列表（`IN ({ph})`） | **23** | **逐 key 扇出**：`read_all(单 key 查询, (k,))` 循环 + Python 合并；语义等价，往返增加但仓库数有限 |
| 字符串拼接（`+ _filter +`） | **4** | 先把过滤片段解析成固定形态再登记查询，或改用 `where` 白名单校验（同 `query_events`） |
| ⚠️ 被误判的静态多行 SQL | **2** | 实为多行字符串字面量（非动态），B1 重分类时剔除，不计入工作量 |
| `for r in c.fetchall()` / `in c.fetchall()` 等非 fetch 赋值 | **68** | 三分类：(a) 单行/多行 → 套用现有 `read_one/read_all`；(b) 聚合（COUNT/SUM）→ 登记聚合查询；(c) 子查询 JOIN → 拆成多次读 + Python 连接（参照 `config_store._rt_int` 已验证的做法） |

**验收断言**：每类各有 1 个样板的双通道等价证明（含跨域 JOIN 拆分后的**行集全等**断言，模板见 S10）。

### W3 多份基表裁定与去重

| 表/副本 | 现状 | 裁定（待确认） | 动作 |
|---|---|---|---|
| `grape_dev_insight@<insight-copy-host>` | 14 表 / 5125 行，代码零引用 | **退役** | 备份后 DROP DATABASE；保留 `<dev-db-host>` 为唯一实库 |
| `grape_dev_runtime@<runtime-copy-host>` | 45 表（历史值，未复核）/ 无 `xwrite_log`，同步性未验证 | **先比对后退役** | 逐表 `CHECKSUM TABLE` + 行数比对；差异为空 → 退役，否则以 `<dev-db-host>` 为准回灌 |
| `takeover_sessions` | **两库均 0 行**（mgmt 为基表、runtime 为同名空表；本轮实测） | **owner=mgmt**（已裁定） | P4 时 DROP runtime 空表 |
| `grape_t_channel_installation` | runtime 11 行、mgmt 0 行 | **owner=runtime**（已裁定） | P4 时 DROP mgmt 空表 |
| `grape_t_channel_lease` | mgmt 12 行 / runtime 11 行，**同 10 个 key 的 owner 与时间戳全部不同**；runtime `updated_at` 更新（01:12 vs 17:22），mgmt 含 `__sim_*` 测试行 | **owner=runtime（已裁定）** | P4 删 mgmt 副本；⚠️ mgmt 进程若读 lease 会读到陈旧数据（潜在 bug，见 §7） |

**验收断言**：`SELECT COUNT(*)` 与 `CHECKSUM TABLE` 在裁定前后一致；退役库有可恢复备份（`mysqldump` + 校验和）。

### W4 视图退役（**252 个**：mgmt 46 / runtime 91 / insight 115）

**前置**：W1/W2 完成，且对应域的读点全部 lib 化。
**步骤**（按域分批；**退役顺序由该域读点清零情况决定，不是按视图数量**：
mgmt 46 个等 mgmt 侧 40 处裸读清零、runtime 91 个等 runtime 侧 11 处清零、
**insight 115 个等 insight 侧 52 处清零**——insight 是残留最多的一侧，必须放最后）：
1. 每批 10-20 个：`information_schema.VIEWS` 取定义 → 归档为 `.sql`（可重建）→ `DROP VIEW`。
2. 每批后：三环境 `p4_verify_all.py` + 8 端口 + 15 分钟日志观察（`forbidden` / `domain 不可用` / `lib failed`）。
3. 任一断言失败 → 用归档 SQL 立即重建该批视图（回滚窗口 < 1 分钟）。

**验收断言**：
- `SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA IN (...) AND VIEW_DEFINITION LIKE '%grape_dev_%'` = **0**；
- 24 小时内无 `domain '...' 连接不可用` 告警；
- 一轮真实流水线（VOD→Issue→Coding→QA→SRE）跑通。

### W5 物理迁机（三库三机）

**前置**：W4 完成（否则跨主机视图必然失效）。
**顺序**（按依赖度从低到高）：

| 步骤 | 动作 | 回滚 |
|---|---|---|
| 1 | **insight → 独立主机**：`mysqldump` 导出 → 目标机导入 → 校验（行数 + CHECKSUM）→ 改 `DEVOPS_DB_HOST` + `GRAPE_DSN_INSIGHT` | 改回 `<dev-db-host>` 地址（配置项，非代码） |
| 2 | **runtime → 独立主机**：同上；额外验证三个排水单元与 IM 表（`GRAPE_IM_DB_NAME` 需同步改） | 同上 |
| 3 | mgmt 留在原机（或反向迁出） | 同上 |

**验收断言**（每步）：
- 目标机 `SELECT COUNT(*)` 与源一致；抽样 200 行 `CHECKSUM` 一致；
- 四进程 8 端口 200；
- **4 个 timer active**（3 排水 + 1 对账），且各域意图 `dead=0`；
- 跨主机 `p4_verify_all.py` FAIL=0（此时 lib 已跨网，延迟上升需观察 P99）。

**风险**：跨主机延迟（原同实例 <1ms → 跨网 0.5-2ms）。组合写与心跳路径受影响最大，需在迁机前做一次**延迟压测**（§7）。

### W6 运维闭环

| 项 | 内容 |
|---|---|
| 监控 | 每域 `xwrite_log` 的 `pending/failed/dead` 计数、最老未完成意图年龄 → 指标暴露（`grape_xwrite_*`） |
| 告警 | `dead>0` 或 `oldest_pending>15min` 告警 |
| 归档 | 每日 03:20，`applied` >7 天；`dead` 不自动删（人工 `requeue`） |
| 演练 | 每季度一次「停一个域库 5 分钟」，验证排水补回与告警 |
| 文档 | 每批改造的提交、验证脚本、双通道结果入 `workdoc/grape/` |

---

## 5. 批次计划（按实测产能）

| 批次 | 内容 | 处数 | 预估轮次 |
|---|---|---|---|
| B1 | 重扫 + 重分类（auto/manual 真实数字） | — | 1 |
| B2 | manual/裸读剩余（按表，每轮 ≤20；104 裸 + 10 兜底噪声） | ~104 | 6 |
| B3 | 动态列名枚举（1）+ 拼接收敛（4）+ 2 条误判剔除 | 7 | 1 |
| B4 | 动态 IN 扇出（23） | 23 | 2 |
| B5 | 非 fetch 消费（68，三类） | 68 | 4-5 |
| B6 | 多份基表裁定（5 项） | — | 2 |
| B7 | 视图退役（252，分批） | — | 5-7 |
| B8 | 迁机（insight → runtime → mgmt） | — | 3 |
| B9 | 监控/告警/演练 | — | 2 |

---

## 6. 验收矩阵（可执行）

| 编号 | 断言 | 命令/位置 |
|---|---|---|
| A1 | 无跨域视图 | `SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA IN ('grape_dev_mgmt','grape_dev_runtime','grape_dev_insight') AND VIEW_DEFINITION LIKE '%grape_dev_%'` → 0。**必须带库过滤**：全实例不过滤会统计到其它库中指向 `grape_dev_*` 的视图（现值 544），永远到不了 0 |
| A2 | 读路径等价 | `p4_verify_all.py` 三环境 FAIL=0 |
| A3 | 查询名闭合 | 代码引用名 ⊆ 注册表 |
| A4 | 写路径等价 | 各批次 `p35*_verify.py`（local/lib 双模）+ 意图 applied |
| A5 | 补偿可重放 | 伪造 `failed` 意图 → 排水 → applied |
| A6 | 双主一致 | 同 HEAD + 改动文件 md5 一致 + 8 端口 200 |
| A7 | 死信为零 | 对账 `dead=0`，`oldest_pending` < 15min |
| A8 | 业务回归 | 真实流水线一轮全绿 |

---

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 跨主机延迟 | 组合写/心跳变慢 | 迁机前压测；必要时把心跳改为批量聚合 |
| 排水单元与主进程争连接 | 连接数打满 | 每域连接池上限 + 排水并发=1 |
| 视图退役遗漏读点 | 功能静默失效（读返回空） | 分批 + 每批双通道 + 归档 SQL 可秒回滚 |
| 多份基表残留 | 数据分裂（写入 A 读 B） | W3 先裁定再迁机；`CHECKSUM` 断言 |
| 意图表膨胀 | 排水变慢 | 日归档 + 索引 `(status, next_retry_at)` |
| 0001 线上节点被误重启 | 用户可见错误 | 验证节点只读；重启统一由 0007 执行 |

---

## 8. 回滚设计

| 层级 | 手段 | 生效时间 |
|---|---|---|
| 代码 | `git revert <commit>` + 双机 reset + 重启 | ~2 分钟 |
| 读路径 | 回落已在代码里（lib 不可用 → 本地 SQL）；只需关 `GRAPE_DOMAIN_LIB` | 重启即生效 |
| 视图 | 归档 `.sql` 重建 | <1 分钟 |
| 迁机 | 改回 `DEVOPS_DB_HOST` / `GRAPE_DSN_*` | 重启即生效 |
| 数据 | 迁机前 `mysqldump` + 校验和 | 按库恢复 |

---

## 9. 待拍板事项

| 编号 | 事项 | 选项 |
|---|---|---|
| D1 | 动态列名口径 | (a) 按列枚举登记（推荐） (b) 通用 `read_column(col, ...)` 白名单校验 |
| D2 | 动态 IN 列表口径 | (a) 逐 key 扇出 + Python 合并（推荐） (b) 支持"占位符个数模板" `..._in1..inN` |
| D3 | `<insight-copy-host>` insight 副本 | (a) 备份后退役（推荐） (b) 保留观察 |
| D4 | `<runtime-copy-host>` runtime 副本 | (a) 先 CHECKSUM 比对再退役（推荐） (b) 直接退役 |
| D5 | ~~`grape_t_channel_lease` 双份数据~~ | **已裁定：runtime 权威**（证据见 §2.1）；待补：mgmt 侧陈旧读点的行为影响 |
| D6 | 迁机顺序 | (a) insight → runtime → mgmt（推荐，依赖最少） (b) 其它 |
| D7 | 跨主机延迟预算 | 需给 P99 目标（建议 < 20ms），决定是否改心跳聚合 |
| D8 | 视图退役节奏 | (a) 每批 10-20 个 + 15 分钟观察（推荐） (b) 一次性全删 |

---

## 10. 与"业界做法"的对标

| 做法 | 本项目采纳 | 说明 |
|---|---|---|
| 每服务独占库 + 应用层跨服务调用（微服务） | **部分**：独占库 ✓；跨域调用用 lib 原语而非 HTTP | 避免引入服务网格；延迟可控 |
| Outbox / 事务性发件箱 | **✓**（`xwrite_log` = outbox） | 幂等键 + 重试 + 死信，标准形态 |
| 双写（dual-write） | **✗ 明确拒绝** | 双写无原子性，故障时两边不一致且难以对账 |
| Saga 补偿 | **部分**：写侧用 outbox 补偿；读侧不做 | 读无副作用，无需 saga |
| 视图作为过渡层 | **✓ 过渡用，最终退役** | 过渡期让改造可分片上线 |
| 数据所有权（single writer） | **✓**（W3 裁定 owner） | 多份基表是本项目最大技术债 |

---

## 附录 A：本轮已完成的写改造清单（摘要）

- P3-4b：场景 nodes / deploy_url（4 处，`scene_nodes_write.py`）
- P3-5a：`managed_repos` 3 处；P3-5b：知识库 7 处；P3-5c：用户映射 10 处（含 `unique_key` 键语义修复）
- P3-5d：`autopilot_tasks` 2 处；P3-5f：`skill` 11 + 凭据 5 + 心跳 2
- P3-5g：mgmt→runtime 会话/级联 13 处（`purge_user_rows` 等组合原语）
- P3-5h：杂项 7 处
- P3-5f 收尾：`change_credential_scope` / `delete_environment` / `migrate_repo` 按域分派 / `global_bot_token_swap`
- 新增 infra：`db_registry.transaction()`（组合写独立事务连接）、`cross_write.unique_key/submit_if_lib`、`mgmt_lib/runtime_lib(foreign_only)`、命名查询白名单

## 附录 B：关键脚本

### B.1 已入库（可复现、可门禁）

| 脚本 | 用途 |
|---|---|
| `scripts/xwrite_drain.py` | 三分区排水（**51 个 handler**：runtime 侧 34、mgmt 侧 17） |
| `scripts/xwrite_reconcile.py` | 对账 / 归档 / 死信重投 |
| `scripts/check_lib_query_names.py` | **A3 门禁**：代码引用的跨域读查询名 ⊆ 目标域 lib 注册表（AST 静态比对，退出码 0/1/2） |
| `scripts/domain_view_freshness.py` / `scripts/sync_domain_views.py` | 跨库视图列新鲜度检查与重建（视图退役期使用） |

### B.2 未入库（运行机本地脚本，需先还纳入库才能作为出口条件）

下列脚本是改造期在运维机上直接运行的（运维机本地目录下的单文件，不在仓库内）。
它们**不在本分支**，因此 W1/W2 的 `p4_verify_all.py` 出口条件在仓库内**不可复现**；
进入 B2 前必须先还纳（登记路径 + 依赖 + 调用口径），否则每轮验收只能由掌握该脚本的人手工完成。

| 运行机本地脚本 | 用途 | 还纳状态 |
|---|---|---|
| `p4_view_inventory.py` | 视图清单 × 目标域 × 代码引用归属 | 待还纳 |
| `p4_read_converter.py` | 读点候选清单（只读） | 待还纳 |
| `p4_read_rewriter.py` | 生成可审阅补丁（AST 自检） | 待还纳 |
| `p4_verify_all.py` | 双通道验证（三环境，FAIL/UNCOVERED 分离） | 待还纳（**B2 入口阻塞**） |
| `p4pilot_verify.py` | 手工参数的双通道样本 | 待还纳 |

## 附录 C：B1 基线同步与 QA 复核结论（Issue #2462）

基线同步：`arch-weekly-11`（`798fd75`）合入 `arch-weekly-11-ljt`，本分支据此继续开发；
同步后 `docs/lib-3db-3host-design.md` 与 `src/kernel/{domain_read,cross_write,db_registry}.py` 均在位。

对合入代码的逐项复核（证据为文件行号，非转述）：

| 编号 | 复核项 | 结论 | 证据 |
|---|---|---|---|
| F1 | 意图表命名 `grape_t_` 规范 | ✅ 已整改 | `src/kernel/cross_write.py:22` = `grape_t_xwrite_log`；`scripts/view_owner_known_duplicates.txt:14` 同步登记 |
| F2 | lib 写原语的 MySQL 方言（`%s` / `ON DUPLICATE KEY` / `NOW()`）在 sqlite 复用路径下不可用 | ✅ 已整改（按收紧可用性） | `src/domains/{mgmt,runtime}/public/__init__.py` 的 `available()` 改为 `db_registry.uses_own_dsn(<domain>)`；`src/domains/contracts/local/*.py` 同口径 |
| F3 | 提交物携带内网地址 / 本机绝对路径 | ⚠️ 本轮补整改 | `deploy/systemd/*.service` 已脱敏（`<deploy-root>` + `EnvironmentFile`），但 `docs/lib-3db-3host-design.md` 仍含真实内网 IP 与本机绝对路径 → 本批改为 `<dev-db-host>` / `<insight-copy-host>` / `<runtime-copy-host>` 占位 |
| F4 | 新增常量无来源登记 | ✅ 已整改 | `src/kernel/cross_write.py:23-24` 的 `MAX_ATTEMPTS` / `RETRY_BACKOFF_S` 已带 rationale 注释 |
| I3 | mgmt lib 白名单含凭据列（`webhook_secret` / `bot_gitcode_token` / `gitcode_webhook_token` / `bot_github_token` / `github_webhook_token` / `password_hash`） | ⏳ 待人工裁决，当前无调用方 | `src/domains/mgmt/public/__init__.py:17-30` 登记；`src/` 全量检索无 `fetch_rows/fetch_one/read_one/read_all` 取这些列的调用点 |
| I4 | `insight_lib()` 无对应 `src.domains.insight.public` 模块 | ⏳ 记录备查 | `src/kernel/domain_read.py:62` 定义但无调用方；该域读仍走 `src/domains/contracts/local/insight_local.py` |

---

## 11. GSB 按方法路由与禁止矩阵（Issue #2479）

> 在 §1.2 硬不变量与 §3 访问策略之上，把 GSB 跨域调用从
> `GRAPE_PROVIDER_MODE` 全局一刀切改为「按接口方法 + 默认偏好 remote +
> 禁止矩阵兜底」的并存路由。方案细节：`docs/issue-2479/{spec,plan}.md`。

### 11.1 路由 key（两级并存）

| key 空间 | 形态 | 数量（实测） | 示例 |
|---|---|---|---|
| 契约方法 | `service.method` | 23（mgmt 7 / runtime 9 / insight 7） | `mgmt.get_bot_config` |
| public 数据原语 | `domain.primitive` | 105（mgmt 39 / runtime 66；`available`/`health` 探活口不参与） | `mgmt.get_repo_field` |

key 清单以 kernel 字面量镜像（`src/kernel/gsb/route_config.py::CONTRACT_METHODS/PUBLIC_PRIMITIVES`，
kernel 禁止 import domains），同步用例（`tests/kernel/test_gsb_route.py`）把漂移挡在 CI。

### 11.2 决策链（每次方法调用）

```
1) 禁止矩阵守卫：caller ∈ {runtime, insight} 且 target = mgmt → lib（恒优先，不查配置表）
2) 全局开关：GRAPE_PROVIDER_MODE ≠ remote → lib（存量单机/共库零回归）
3) DB 配置表 gsb_provider_route（version+TTL≤30s 缓存；读失败退回上次已知配置）
4) 内置基线：热路径四项 + 压测超阈值 14 项 → lib（QA Required#1 实测裁决，
   remote 由配置表显式放开）；public 原语 → lib（本期无 remote 传输通道）；
   压测采样错误 5 项（无数据可裁决）→ remote
```

- 配置表与 `gsb_service_registry` 同库；`set_all_known("lib")` 一键全量回滚
  （回滚后行为与未开启 remote 完全一致，无需改代码）。
- `get_service()` 进程级代理句柄复用（QA Required#2）：同服务恒返回同一
  `_RoutedService` 代理，Local/Remote 实例与 HTTP 客户端惰性构造一次后复用
  （per-call 构造成本 A/B：11.491ms → 0.056ms，见 bench-report §3）。
- remote 超时/不可达 **fail-fast**：`GsbRemoteTimeoutError`（`GSB_REMOTE_TIMEOUT`）/
  `GsbRemoteUnreachableError`（`GSB_REMOTE_UNREACHABLE`），不回落 lib；
  metrics status 标签区分 `remote_timeout` / `remote_unreachable` / 数字业务码。
- per-method 超时（配置表 `timeout_ms`）经 `ServiceHttpClient.with_timeout()` →
  `GsbClient.with_timeout()` **线程本地 per-request 下发**（QA Required#3：不改共享
  客户端超时，无粘滞，无假 GSB_REMOTE_TIMEOUT）。

### 11.3 禁止矩阵（二维：调用方进程域 × 目标域）

| 调用方 ↓ \ 目标 → | mgmt | runtime | insight |
|---|---|---|---|
| mgmt（grape-server） | 本域直连 | 允许 remote（按配置表） | 允许 remote（按配置表） |
| runtime（event/worker） | **全部 lib**（mgmt 面：契约 7 方法 + public 40 原语 + 35 命名查询） | 本域直连 | 按配置表 |
| insight | **全部 lib**（同一面：契约 7 + 40 原语 + 35 命名查询）。⚠️ 注：这是**通道能力**；insight 侧自身仍有 **52 处裸读走视图**（W1 未清零），不能理解为"insight 已完成 lib 化" | 按配置表 | 本域直连 |

- 该矩阵在三库三机目标态（各域独立主机）下保持同一形态，不因网络变远而放宽。
- 双守卫：运行时 `matrix_forces_lib()`（`get_service("mgmt")` 在 runtime/insight 进程恒
  Local 实例，不依赖配置表）+ CI 静态扫描 `scripts/check_gsb_route_matrix.py`
  （命中 runtime/insight 引用 `MgmtServiceRemote` / `GsbClient("mgmt")` /
  `ServiceHttpClient("mgmt")` / `_PROVIDERS` 裸访问即红，接入 `scripts/check-all.sh`）。

### 11.4 性能判据与实测（M2 交付）

- 判据：逐接口 lib/remote P95（同进程、同参数、同数据量，各采样 ≥200 次）；
  remote 候选须同时满足 **增量 ≤10ms 且相对增幅 ≤20%**。
- 实测报告：`docs/issue-2479/bench-report.md`（23 契约 + 105 原语逐接口表；
  热路径四项 + 超阈值 14 项基线锁 lib）。loopback+共库拓扑下 remote 固定传输成本
  1.3~4.7ms，绝对增量全部达标、相对增幅不达标 → 按「不影响性能」口径把超阈值
  14 项**内置基线锁 lib**（空表部署与实测结论一致，QA Required#1 裁决），
  生产三库三机拓扑对 lib P95 ≥15ms 的重查询逐接口实测达标后再经配置表逐 key 放开 remote。
- `get_service()` 逐次调用构造成本实测（remote 制式，每次新建代理 + Local/Remote
  实例）~11.5ms/次；进程级代理句柄复用后 ~0.06ms/次（同方法 A/B，
  见 bench-report §3，QA Required#2 修复）。

---

## 附录 D：数字口径与复算命令（单一事实来源）

规则：**本页任何数量都必须能在本附录找到复算命令**；改数字必须同时更新本表（值 + 时点）。
标「本轮实测」= 2026-11-14 复核；标「历史批次」= 来自各批次验证记录，本轮未重算（迁移前必须重算一次）。

| 数量 | 权威值 | 类别 | 复算命令（在仓库根执行，`<tree>` = 目标工作树） |
|---|---|---|---|
| 跨域读候选 | **114 处 / 20 表** | 本轮实测 | `sed 's#/root/wt-arch#<tree>#' /root/p4_read_converter.py > /tmp/c.py && python /tmp/c.py`（只读） |
| 其中裸读 / 兜底噪声 | **104 / 10** | 本轮实测 | `python /root/p4_candidate_verdict.py /root/workdoc/grape/p4-read-convert.json`（AST 判据：候选所在函数内有无 lib 句柄） |
| 已接线点 | **202** | 本轮实测 | `git grep -h 'domain_read import' -- 'src/*.py' \| wc -l` |
| auto / 自检绿 / manual | **16 / 8 / 98** | 本轮实测 | 重跑 `p4_read_rewriter.py` → `p4-rewrite-plan.json` 里 `kind` 与 `selfcheck` 计数 |
| 动态 SQL 细分 | **IN 23 / 列名 1 / 拼接 4 / 误判 2** | 本轮实测（正则粗分，B1 重分类） | 对 `p4-rewrite-plan.json` 中 `kind == 'manual: 动态 SQL'` 的 `sql` 字段做 `IN\\(`、`{...}`、`+` 匹配 |
| 非 fetch 消费 | **68** | 本轮实测 | 同上，`kind == 'manual: 结果消费方式不是 fetchone/fetchall'` |
| 跨域视图 | **252**（46 / 91 / 115） | 本轮实测 | `SELECT table_schema,COUNT(*) FROM information_schema.views WHERE table_schema IN ('grape_dev_mgmt','grape_dev_runtime','grape_dev_insight') AND view_definition LIKE '%grape_dev_%' GROUP BY table_schema` |
| 基表数 | **91 / 52 / 17** | 本轮实测 | `SELECT table_schema,SUM(table_type='BASE TABLE') FROM information_schema.tables WHERE table_schema LIKE 'grape_dev_%' GROUP BY table_schema` |
| api_latency_log 行数 | **6423** | 本轮实测 | `SELECT COUNT(*) FROM grape_dev_insight.api_latency_log` |
| 意图量 | mgmt **5593** / runtime **84** / insight **1** | 本轮实测 | 各库 `SELECT status,COUNT(*) FROM grape_t_xwrite_log GROUP BY status` |
| 排水 / 对账 timer | **3 + 1 = 4** | 本轮实测 | `systemctl list-timers --all \| grep grape-xwrite` |
| drain handler | **51**（runtime 34 + mgmt 17） | 本轮实测 | 统计 `scripts/xwrite_drain.py` 里 `out["…"] =` 赋值键 + `out.update({…})` 字典键（去重） |
| A2 读等价 | **FAIL=0，PASS 67+31，UNCOVERED 7** | 本轮实测 | 在两个进程环境各跑 `p4_verify_all.py`（env 取 `/proc/<pid>/environ`，PYTHONPATH 指当前树） |
| A3 查询名闭合 | **PASS**（419 文件；mgmt 41 函数/35 查询、runtime 68 函数/70 查询） | 本轮实测 | `python scripts/check_lib_query_names.py` |
| 写批次提交数 | **33**（`2f70a0d3e..a3a18f49c`） | 本轮实测 | `git rev-list --count 2f70a0d3e..a3a18f49c` |
| 读批次提交数 | **11**（`670f7709b..3f241628a`） | 本轮实测 | `git rev-list --count 670f7709b..3f241628a` |
| P2 收尾接线 / 裸读 | **31 / 208**（候选 224） | 本轮实测（历史时点回放） | `git worktree add /root/wt-p2end 436e3299f --detach` + 前两条命令 |
| `<insight-copy-host>` 副本 | 14 表 / 5125 行 | **历史批次（未复核）** | 迁移前用 `CHECKSUM TABLE` + `COUNT(*)` 重算，该机 root 口令与 `<dev-db-host>` 不同 |
| `<runtime-copy-host>` 副本 | 45 表 / 无 `xwrite_log` | **历史批次（未复核）** | 同上 |
| P3 各批次处数（5a 3 / 5b 7 / 5c 10 / 5d 2 / 5g 13 / 5h 7 …） | 见附录 A | **历史批次（未复核）** | `/root/workdoc/grape/p35-crosswrite-inventory.md` + 各 `p35*_verify.py` 记录 |