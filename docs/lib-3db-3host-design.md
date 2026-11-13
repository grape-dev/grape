# Grape 三库三机 · lib 模式收口设计方案

> 版本：v1（基于 2026-11-13/14 实测）
> 现状 HEAD：`3f241628a`（分支 `feat/domain-lib-p0-20261113`，双主已同步）
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
2. **双主同 HEAD**：0007 / 0001 的 `/opt/grape` 任一时刻同 commit；nginx `ip_hash` 按 IP 钉机，两台行为必须一致。
3. **只展示一次的副作用禁止入意图队列**：建凭据 token、吊销凭据必须**同步直连**（重放会造孤儿/留窗口）。
4. **组合写收成一个原语**：探测即快照（`save_agent_runtimes`）、账号级联（`purge_user_rows`）、吊销级联（`purge_runtime_credential`）不得拆成多条意图。
5. **意图表有上限**：`applied` 行按 7 天归档，`dead` 行必须人工处理，不得静默堆积。

---

## 2. 现状盘点（实测，非估算）

### 2.1 库与视图

| 项 | 实测 |
|---|---|
| 三库位置 | **全部在 `<dev-db-host>` 同一实例**（`grape_dev_mgmt` / `grape_dev_runtime` / `grape_dev_insight`） |
| 跨域视图 | **137 个**：mgmt 46（→runtime 36、→insight 10）；runtime 91（→mgmt 81、→insight 10） |
| 视图可更新性 | mgmt 侧 runtime 表均为 `IS_UPDATABLE=YES` → 今天跨域写"能用"全靠视图 |
| insight 副本 | `<dev-db-host>` = **126 表 / api_latency_log 5849 行**（实库）；`<insight-copy-host>` = 14 表 / 5125 行（旧副本） |
| runtime 副本 | `<runtime-copy-host>` 存在一份 `grape_dev_runtime`（45 表、无 `xwrite_log`），与 `<dev-db-host>` 是否同步**未验证** |
| 多份基表 | `takeover_sessions`（mgmt 基表 + runtime 空表）、`grape_t_channel_installation`（runtime 11 行 + mgmt 0 行）、`grape_t_channel_lease`（两库都有数据） |

### 2.2 代码改造进度

| 项 | 数量 |
|---|---|
| 跨域写改造 | **已完成**（P3-4b/5a/5b/5c/5d/5f/5g/5h 全批次；47 个提交） |
| 读改造已落地 | **81 处** |
| 重扫剩余跨域读 | **114 处 / 20 张表**（最大 `agent_tasks` 25）；**auto 仅 16（自检绿 8）**，manual **98**（消费形态 68 + 动态 SQL 30） |
| 可自动改写 | **auto 16 / 自检绿 8** —— 改写器红利已耗尽，其余须手工 |
| manual 待设计 | **99 处**：动态 SQL 30 + 非 fetch 消费 69 |
| 验证器 | `p4_verify_all.py`（三环境跑，FAIL/UNCOVERED 分离）；当前 FAIL=0，UNCOVERED 7 |

### 2.3 运行面

| 项 | 状态 |
|---|---|
| 四进程 | `grape-{server,event,worker,insight}-server`，双主 8 端口 200 |
| lib 开关 | 双主四进程均已开（`GRAPE_DOMAIN_LIB=1` + 三域 DSN；**本域读开关=0**、外域=1） |
| 排水单元 | 双主各 3 个 timer active，错峰 `:00 / :02 / :04` |
| 对账 | `grape-xwrite-reconcile.timer` 每日 03:20，归档 7 天前 applied |
| 意图量 | mgmt 45 / runtime 84 / insight 1（全 applied，无 dead） |

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

`mgmt`（84 表，含 36 视图）· `runtime`（45 表，含 81 视图）· `insight`（126 表）

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

### W1 读路径 lib 化（114 处 + 新增）

**前置**：无（可立即开始）。
**步骤**：
1. 重跑 `p4_read_converter.py` → 真实候选清单（旧计划已失效，69/89 锚点不再匹配）。
2. 重跑 `p4_read_rewriter.py` → auto/manual 分类 + AST 自检。
3. auto 批次：临时副本编译 → 落地 → 登记 → 整文件编译 → `p4_verify_all.py`（0001 三环境）→ FAIL=0 才 push+重启。
4. 每批上限 20 处（实测产能），按表推进，单轮只碰一张表。

**验收断言**：
- `p4_verify_all.py --domain {runtime,mgmt}` 在 `grape-server` / `grape-worker-server` 两个环境均 **FAIL=0**；
- 代码里引用的查询名 ⊆ 注册表（脚本：`used_names - registry_names == ∅`）；
- 双机 `py_compile` 全绿 + 8 端口 200 + 三 timer active。

### W2 动态 SQL 与非常规消费（99 处）

**前置**：口径裁定（§9-D1 / D2）。
**分类与做法**：

| 形态 | 数量 | 建议做法 |
|---|---|---|
| 动态列名（`{mapping_col}`） | ~4 | **按列枚举**：列集合有限（feishu_open_id / gitcode_user / gitee_user…），每列登记一条查询 |
| 动态 IN 列表（`IN ({ph})`） | ~26 | **逐 key 扇出**：`read_all(单 key 查询, (k,))` 循环 + Python 合并；语义等价，往返增加但仓库数有限 |
| `for r in c.fetchall()` / `in c.fetchall()` 等非 fetch 赋值 | ~69 | 三分类：(a) 单行/多行 → 套用现有 `read_one/read_all`；(b) 聚合（COUNT/SUM）→ 登记聚合查询；(c) 子查询 JOIN → 拆成多次读 + Python 连接（参照 `config_store._rt_int` 已验证的做法） |

**验收断言**：每类各有 1 个样板的双通道等价证明（含跨域 JOIN 拆分后的**行集全等**断言，模板见 S10）。

### W3 多份基表裁定与去重

| 表/副本 | 现状 | 裁定（待确认） | 动作 |
|---|---|---|---|
| `grape_dev_insight@<insight-copy-host>` | 14 表 / 5125 行，代码零引用 | **退役** | 备份后 DROP DATABASE；保留 `<dev-db-host>` 为唯一实库 |
| `grape_dev_runtime@<runtime-copy-host>` | 45 表 / 无 `xwrite_log`，同步性未验证 | **先比对后退役** | 逐表 `CHECKSUM TABLE` + 行数比对；差异为空 → 退役，否则以 `<dev-db-host>` 为准回灌 |
| `takeover_sessions` | mgmt 有数据可能、runtime 空表 | **owner=mgmt**（已裁定） | P4 时 DROP runtime 空表 |
| `grape_t_channel_installation` | runtime 11 行、mgmt 0 行 | **owner=runtime**（已裁定） | P4 时 DROP mgmt 空表 |
| `grape_t_channel_lease` | mgmt 12 行 / runtime 11 行，**同 10 个 key 的 owner 与时间戳全部不同**；runtime `updated_at` 更新（01:12 vs 17:22），mgmt 含 `__sim_*` 测试行 | **owner=runtime（已裁定）** | P4 删 mgmt 副本；⚠️ mgmt 进程若读 lease 会读到陈旧数据（潜在 bug，见 §7） |

**验收断言**：`SELECT COUNT(*)` 与 `CHECKSUM TABLE` 在裁定前后一致；退役库有可恢复备份（`mysqldump` + 校验和）。

### W4 视图退役（137 个）

**前置**：W1/W2 完成，且对应域的读点全部 lib 化。
**步骤**（按域分批，**先 insight 目标视图，量最小**）：
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
- 三 timer active，且各域意图 `dead=0`；
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
| B2 | auto 剩余（按表，每轮 ≤20） | ~114 | 6 |
| B3 | 动态列名枚举（4） | 4 | 1 |
| B4 | 动态 IN 扇出（26） | 26 | 2 |
| B5 | 非 fetch 消费（69，三类） | 69 | 4-5 |
| B6 | 多份基表裁定（5 项） | — | 2 |
| B7 | 视图退役（137，分批） | — | 5-7 |
| B8 | 迁机（insight → runtime → mgmt） | — | 3 |
| B9 | 监控/告警/演练 | — | 2 |

---

## 6. 验收矩阵（可执行）

| 编号 | 断言 | 命令/位置 |
|---|---|---|
| A1 | 无跨域视图 | `SELECT COUNT(*) FROM information_schema.VIEWS WHERE VIEW_DEFINITION LIKE '%grape_dev_%'` → 0 |
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
| `scripts/xwrite_drain.py` | 三分区排水（50 个 handler） |
| `scripts/xwrite_reconcile.py` | 对账 / 归档 / 死信重投 |
| `scripts/check_lib_query_names.py` | **A3 门禁**：代码引用的跨域读查询名 ⊆ 目标域 lib 注册表（AST 静态比对，退出码 0/1/2） |
| `scripts/domain_view_freshness.py` / `scripts/sync_domain_views.py` | 跨库视图列新鲜度检查与重建（视图退役期使用） |

### B.2 未入库（运行机本地脚本，需先还纳入库才能作为出口条件）

下列脚本是改造期在运维机上直接运行的（`/root/` 下单文件，非仓库内容）。
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
| F3 | 提交物携带内网地址 / 本机绝对路径 | ⚠️ 本轮补整改 | `deploy/systemd/*.service` 已脱敏（`/opt/grape` + `EnvironmentFile`），但 `docs/lib-3db-3host-design.md` 仍含真实内网 IP 与 `/root/` 路径 → 本批改为 `<dev-db-host>` / `<insight-copy-host>` / `<runtime-copy-host>` 占位 |
| F4 | 新增常量无来源登记 | ✅ 已整改 | `src/kernel/cross_write.py:23-24` 的 `MAX_ATTEMPTS` / `RETRY_BACKOFF_S` 已带 rationale 注释 |
| I3 | mgmt lib 白名单含凭据列（`webhook_secret` / `bot_gitcode_token` / `gitcode_webhook_token` / `bot_github_token` / `github_webhook_token` / `password_hash`） | ⏳ 待人工裁决，当前无调用方 | `src/domains/mgmt/public/__init__.py:17-30` 登记；`src/` 全量检索无 `fetch_rows/fetch_one/read_one/read_all` 取这些列的调用点 |
| I4 | `insight_lib()` 无对应 `src.domains.insight.public` 模块 | ⏳ 记录备查 | `src/kernel/domain_read.py:62` 定义但无调用方；该域读仍走 `src/domains/contracts/local/insight_local.py` |