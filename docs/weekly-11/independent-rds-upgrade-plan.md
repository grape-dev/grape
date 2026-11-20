# Grape 三库独立 RDS 实例升级技术方案

| 项 | 内容 |
| --- | --- |
| 版本 | v1.0（草案，待评审） |
| 日期 | 2026-11-13 |
| 适用分支 | `arch-weekly-11-ljt`（HEAD 含 PR #1349 合入结果） |
| 目标环境 | 现网（两机四进程：mgmt / event / worker / insight） |
| 关联文档 | `docs/weekly-09/domain-db-split.md`、`docs/weekly-11/migration-parity-assessment.md`、`docs/weekly-11/domain-view-and-ignore-pitfalls.md`、**`docs/weekly-11/cross-domain-access-channel-analysis.md`（通道优化分析 v1.1，含 lib 读/写通道）**、`cross-domain-write-channel.csv`（30 张跨域写表通道分配） |
| 附表 | `docs/weekly-11/independent-rds-foreign-table-matrix.csv`（70 张外域表全量矩阵） |

> **执行状态（2026-11-14）**：见 `docs/weekly-11/rds-upgrade-execution-status.md`——P0 守卫/看板、P2 内部接口已完成并有测试门禁；P1 归属重定、P3 写分流、P4 实例搬迁的剩余项与阻塞原因逐条记录（含实测数字）。

> 文中主机、账号、口令一律用 `<mgmt-rds>` / `<runtime-rds>` / `<insight-rds>` / `<user>` / `<password>` 占位，实际值走配置中心，不落文档。

---

## 0. 一句话结论

现网现在是「**每进程只连本域库 + 每个库里有一层跨域可更新视图**」的兼容形态：连接是独立的，**逻辑不是独立的**。要升级为三套独立 RDS 实例（实例之间网络隔离、账号互不授权、无跨库视图），需要把 **70 张外域表的 2691 处 SQL 引用**从「读视图」改为「走 GSB 内部接口或 A2A 事件」，并把 **182 个跨域视图**清零。这不是一次配置切换，而是一次按域推进的重构，建议 **P0 → P1 归属重定 → P2 接口补齐 → P3 outbox → P3.5 收尾 → P4 实例 → P5 收敛，约 9–10 周**，以「视图数归零 + 账号无跨库权限 + 代码外域表引用归零」作为唯一验收判据。

---

## 1. 背景与目标

### 1.1 背景

- 现网三个领域库（管理域 / 运行域 / 洞察域）**同处一个 MySQL 实例**，账号按域拆分（`grape_mgmt` / `grape_runtime` / `grape_insight`），但每个库里都为「非本域表」建了**跨库可更新视图**，使任一进程都能"像读自己的表一样"读写其他域的表。
- 这种形态解决了拆分初期的迁移成本，但代价是：**领域边界只是目录约定，数据库层面没有任何强制**。任何一个进程的 SQL 写错表名、任何一次迁移漏改视图，都会直接穿透到别的域；库级别无法做独立的规格、备份、故障域和权限收敛。
- 目标形态是**三个独立 RDS 实例**：各自规格、各自备份、各自故障域，网络与账号都只允许本域访问；跨域只能通过服务接口（同步读）或事件（异步写）。

### 1.2 目标

| 编号 | 目标 | 可测量判据 |
| --- | --- | --- |
| G1 | 三个独立 RDS 实例承载三个域库 | 三实例三 endpoint；任一实例故障不影响其他域的主链路 |
| G2 | 库内**零跨域视图** | `information_schema.views` 计数 = 0（三库） |
| G3 | 账号**零跨域权限** | 每域账号 `SHOW GRANTS` 只含本库；跨库查询直接 1142/1146 |
| G4 | 代码**零外域表引用** | 静态扫描（本方案附录 A 脚本）结果为空；arch 守卫常绿 |
| G5 | 跨域读写全部走服务接口 | 每个跨域读取都有对应 `/internal/*` 端点；跨域写 100% 走 outbox + 补偿 |

### 1.3 非目标（本轮不做）

- 不做分库分表、不做读写分离、不引入消息中间件（A2A 事件表已具备队列语义）。
- 不重写业务逻辑；只改「数据从哪里取、往哪里写」。
- 不改变对外契约（OpenAPI 的 `/api/*` 面不变）；只新增内部 `/internal/*` 端点。
- 不做跨实例强一致（不引入 2PC）；跨域写采用最终一致 + 补偿（沿用现有 `cross_write` 设计）。

### 1.4 术语

| 术语 | 含义 |
| --- | --- |
| 域（domain） | mgmt / runtime / insight 三者之一，代码上对应 `src/domains/<domain>/` |
| 归属域（owner） | 某张表的**唯一**写入方所在域；其余域只能通过接口访问 |
| 跨域视图 | 在 A 域库中为 B 域表建立的视图（本方案要清除的对象） |
| GSB | Grape Service Bus：服务注册/发现 + 内部接口调用（`/internal/gsb/*`） |
| outbox | 每域库内的 `grape_t_xwrite_log`，记录跨域写意图，由补偿循环重试 |
| Provider | `LocalProvider`（进程内直查）/ `RemoteProvider`（经 GSB 调远端 `/internal/*`） |

---

## 2. 现状盘点（全部为 2026-11-13 现网实测）

### 2.1 物理与连接

| 项 | 现状 |
| --- | --- |
| 实例 | 1 个 MySQL 实例，三库同居：`grape_mgmt_db` / `grape_runtime_db` / `grape_insight_db` |
| 账号 | 每域一个账号，但库内视图让「读别域」在 SQL 层合法 |
| 进程 → 库 | mgmt 进程（38081）→ 管理库；event（38082）→ 运行库；worker（38083）→ 运行库（注册服务名 `runtime`）；insight（38084）→ 洞察库 |
| 多域连接开关 | `GRAPE_DOMAIN_LIB` **未设置**（默认 0）、`GRAPE_DSN_*` **未设置** → 进程没有第二套连接，跨域读取**完全依赖视图** |
| 代码里的多域设施 | `src/kernel/db_registry.py`（122 处引用）、`src/kernel/cross_write.py`（58 处引用）、`src/kernel/domain_read.py`（已具备 lib 模式雏形） |

### 2.2 逻辑边界（实测计数）

| 库 | 基表 | 跨域视图 |
| --- | --- | --- |
| `grape_mgmt_db` | 54 | 44 |
| `grape_runtime_db` | 36 | 57 |
| `grape_insight_db` | 12 | 81 |
| **合计** | **102** | **182** |

- 182 个视图覆盖 **91** 张「非本域表」；其中 **70** 张在代码里有跨域 SQL 引用，合计 **2691** 处；剩下 **60 个视图（覆盖 30 张表）代码已无跨域引用 → 可先删，属于快赢。
- 按消费方（读别域表的域）汇总：**api 645 / mgmt 211 / kernel 177 / runtime 171 / insight 101**。
- 结论：**运行域读管理域、管理域读运行域、洞察域读运行域，三条方向都很重**；其中 api（前端后端，跑在 mgmt 进程内）读运行域最密。

### 2.3 GSB 现状

| 项 | 现状 |
| --- | --- |
| 注册服务 | 4 类（mgmt / event / runtime / insight）× 2 台 = 8 条注册记录，全部 online |
| 总线运维端点 | 每进程 2 个：`GET /internal/gsb/services`、`GET /internal/gsb/services/{name}` |
| 业务内部端点 | runtime 9 个（已挂载）、insight 7 个（已挂载）、**mgmt 7 个（定义了但按架构约定不挂载 → 远程 404）** |
| 线上实际调用 | 只有 2 个内部端点被真正调用：`GET /internal/runtimes`（worker 侧 1419 次）、`GET /internal/dashboard/overview`（insight 侧 1419 次）；mgmt 侧 42 次 `/internal/*` 全部 404 |
| 调用方向 | 客户端指标只有 `caller=mgmt → target=runtime / insight`；event / worker / insight 均未发起过内部调用 |

**这段数据说明两件事**：
1. GSB 的**注册/发现**已经在跑，但**业务接口的利用率极低**（16 个已挂载端点里只有 2 个在用）——接口面是"备用"状态。
2. 现网已经存在**因视图依赖而隐藏的契约冲突**：webhook 的仓库配置读取走 `get_service("mgmt")`，打到未挂载的 mgmt 内部路由，42 次 404（当前被 catch 后降级为空 token）。独立化过程必须把这类路径显式补齐。

### 2.4 迁移账本

- 每库各有一本 `grape_t_schema_migrations`（均为本域基表，无跨库视图）：mgmt 记 mgmt 域条目，runtime / insight 各记本域条目。
- 历史上已发生两起号段事故（本仓有守卫测试）：
  - `202611030001`（issue658_role_tags）曾被改写为别的号，导致「已发布迁移改名」红灯；
  - `202611160001` 曾被写成 role_tags，与已盖章的 `takeover-sessions-v1` 同号异名 → 迁移会被永久跳过。
- 独立实例后，**每个实例必须有自己的账本基表**，不再共享视图，否则「谁执行迁移」和「盖没盖章」会继续含糊。

---

## 3. 目标架构

### 3.1 拓扑

```
         ┌─────────────────────────── VPC 内网 ───────────────────────────┐
         │                                                                │
   ┌─────┴──────┐        ┌────────────────┐        ┌────────────────┐     │
   │ <mgmt-rds> │        │ <runtime-rds>  │        │ <insight-rds>  │     │
   │ grape_mgmt │        │ grape_runtime  │        │ grape_insight  │     │
   │ 54 表      │        │ 36 表          │        │ 12 表          │     │
   └─────┬──────┘        └───────┬────────┘        └───────┬────────┘     │
         │ 仅 grape_mgmt         │ 仅 grape_runtime         │ 仅 grape_insight
         │                       │                          │
   ┌─────┴───────────────────────┴──────────────────────────┴─────┐
   │                        GSB（服务总线）                        │
   │  注册/发现：/internal/gsb/services  ·  业务：/internal/*     │
   └─────┬───────────────┬───────────────┬───────────────┬────────┘
         │               │               │               │
     mgmt 进程       event 进程      worker 进程     insight 进程
     (38081)        (38082)         (38083)         (38084)
```

### 3.2 连接与权限模型

| 主体 | 连接 | 权限 |
| --- | --- | --- |
| mgmt 进程 | 只连 `<mgmt-rds>`（DSN_mgmt） | 本库 SELECT/INSERT/UPDATE/DELETE + 迁移 DDL |
| event 进程 | 只连 `<runtime-rds>`（DSN_runtime） | 本库读写 |
| worker 进程 | 只连 `<runtime-rds>`（DSN_runtime） | 本库读写（+ 任务相关表） |
| insight 进程 | 只连 `<insight-rds>`（DSN_insight） | 本库读写 |
| 迁移执行 | 每域独立执行器（同进程启动期） | 只对本域库 DDL |
| 备份/巡检 | 独立账号，按实例 | 只读 |

**硬约束**：
- 任何进程都不得配置第二个域的 DSN（`GRAPE_DSN_*` 只允许本域那一条，其余留空）。
- 任何域账号都不得对别的实例有 `GRANT`（连 `SELECT` 也不给）。
- 数据库层禁止视图跨实例：MySQL 视图不能跨实例，天然满足；**但必须保证"表名唯一归属"**，否则迁移脚本会把视图重新建出来。

### 3.3 跨域访问通道（v1.1 修订：五通道）

> 修订依据：`docs/weekly-11/cross-domain-access-channel-analysis.md`。实测 70 张外域表中 **60 张存在跨域写**，说明主要矛盾不是"通道选哪个"，而是**表归属与代码归属错位**；修正归属后，剩余跨域读只有 7 张表 10 处引用。

| 通道 | 适用 | 一致性 | 约束 |
| --- | --- | --- | --- |
| **① 域内直连**（首选） | 归属重定 / 模块归位后，代码与表同域 | 强一致 | 无跨域动作；这是 30 张表的最终形态 |
| **② 服务接口**（默认读通道） | 跨域只读、配置类、需审计 | 强一致（读 owner 库） | 必须聚合端点，禁止 N+1；`/internal/*` + 服务令牌 |
| **③ lib 只读**（受控例外） | 高频（QPS ≥ 50）+ 多语句/JOIN + 小 payload + 同地域内网 | 强一致 | 见 3.4 六条硬约束；当前 7 张纯跨域读表无一张满足，故不默认启用 |
| **④ lib 同步写**（受控，见 3.5） | 必须同步的跨域写：读己之写 / 并发正确性（租约、绑定）/ 用户即时反馈 | 强（目标域内原子） | 调目标域 `public` 写函数；本地先写 + outbox 兜底；列级写权限、无 DELETE/DDL |
| **⑤ outbox + 补偿**（异步兜底与派生） | 日志/流水/派生数据、可延迟的跨域通知 | 最终一致（≤1 次退避窗口） | 幂等键 + 重试 + 死信 |

**明确禁止**：跨域裸 SQL 直查（绕过 ②③④ 通道）、lib 写任意表（只能调 public 写函数）、在调用方未提交事务内调 lib 写、跨域 JOIN、跨域视图、跨域触发器、跨域外键、`SELECT *`。

### 3.4 lib 只读通道的六条硬约束（L1–L6）

| 编号 | 约束 |
| --- | --- |
| L1 | 只能调 `domains/<d>/public/` 显式导出的函数（`__all__`），禁止 import 域内部模块（新增 R8 守卫） |
| L2 | 只读账号 + **列级授权** `GRANT SELECT (col...) ON <db>.<table>`；禁止 `SELECT *`、禁止 DDL |
| L3 | 函数级限额：`max_rows=500`、`timeout_ms=800`、`max_payload=256KB`，超限抛错不截断 |
| L4 | 每进程最多 2 个 lib 目标域（`GRAPE_LIB_TARGETS` 白名单），pool_size ≤ 5 |
| L5 | 熔断：30s 窗口失败率 ≥ 20% → 熔断 30s，回退服务接口或 503 |
| L6 | 审计：每次调用记 `(caller, target, fn, rows, elapsed_ms, trace_id, channel=lib)` |

### 3.5 lib 同步写通道的六条硬约束（M1–M6）

| 编号 | 约束 |
| --- | --- |
| M1 | 只能调 `domains/<d>/public/` 的**写函数**（函数内 SQL 固定，不接受调用方传表名/列名/SQL 片段） |
| M2 | 写账号：`GRANT INSERT, UPDATE (col...) ON <db>.<table>`；**不给 DELETE、不给 DDL**；删除走目标域端点（可软删） |
| M3 | 幂等：业务唯一键 + `ON DUPLICATE KEY UPDATE`，或条件更新 `WHERE status='old'`；无幂等实现不得进 W2 |
| M4 | 编排：**本地先写 + outbox(pending) → 提交 → 同步 lib 写 → 成功置 applied / 失败留给补偿**；禁止在未提交事务内调 |
| M5 | 限额：单次 ≤ 100 行、≤ 300 ms、每 caller ≤ 50 写/秒；目标域 `lock_wait_timeout` ≤ 3s；超时最多重试 1 次（仅幂等） |
| M6 | 审计：目标域在**同一事务**写审计行（caller、fn、business_key、trace_id、变更摘要）；缺审计行视为未接入 |

---

## 4. 设计决策（ADR 摘要）

| 编号 | 决策 | 备选与被否原因 |
| --- | --- | --- |
| D1 | 跨域视图全部删除，不做"只读视图保留" | 视图无法跨实例；且可更新视图让边界形同虚设 |
| D2 | **允许 lib 只读与 lib 同步写**（均受控），禁止 lib 写任意表与裸 SQL；`GRAPE_DOMAIN_LIB` 改为按目标域白名单 `GRAPE_LIB_TARGETS`，读写函数分别登记 | 读默认走服务接口（聚合端点）；写按"是否必须同步"分流到 W2/W3，避免第二套直连泛滥 |
| D3 | 跨域写统一 outbox + 补偿（沿用 `grape_t_xwrite_log`，每域一份） | 2PC 跨实例代价高；MQ 需新增组件 |
| D4 | 读路径优先"批量聚合端点"，不做 N+1 | 逐条调远端会把 p95 打穿；列表页必须一屏一次调用 |
| D5 | mgmt 开放**只读白名单**内部端点 | 之前"mgmt 完全不开放"导致的 42 次 404 必须消除；只读、限定字段、限定 repo 作用域 |
| D6 | 幂等键 = `(domain, action, business_key, version)` | 补偿重试必须可重放；业务键不足时用事件 seq |
| D7 | 数据搬迁用「全量快照 + binlog 增量」，停机窗口内做终态校验 | 纯停机全量对 100+ 表不可行；纯 DMS 无校验不可信 |
| D8 | 按「洞察 → 运行 → 管理」顺序切换实例 | 洞察只读为主、影响面最小；管理域最后切，回滚代价最低 |
| D9 | 每域独立迁移账本 + 启动自检 | 共享账本会掩盖"某域没执行"的事实 |
| D10 | 独立后保留 7 天旧库只读 | 回滚需要；也用于差异比对 |

---

## 5. 依赖矩阵

### 5.1 读取热点（Top 25，全量见附表 CSV）

| 表 | 归属域 | SQL 引用数 | 消费方（引用数） | 涉及文件 |
| --- | --- | --- | --- | --- |
| `agent_tasks` | runtime | 262 | insight 36 / api 30 / mgmt 5 / kernel 1 | 45 |
| `agent_runtimes` | runtime | 256 | api 38 / mgmt 31 / insight 10 / kernel 3 | 42 |
| `events` | runtime | 162 | api 64 / insight 17 / mgmt 4 | 30 |
| `issue` | runtime | 160 | api 79 / insight 4 / mgmt 2 / kernel 2 | 27 |
| `managed_repos` | mgmt | 157 | api 17 / runtime 13 / kernel 10 / insight 1 | 29 |
| `skill` | runtime | 141 | mgmt 59 / api 27 / kernel 5 | 19 |
| `runtime_credentials` | runtime | 121 | mgmt 22 / insight 20 / api 15 / kernel 1 | 29 |
| `devops_pipeline_scene` | mgmt | 97 | api 34 / runtime 15 / insight 2 | 17 |
| `grape_accounts` | mgmt | 83 | runtime 12 / api 6 / kernel 4 / insight 2 | 21 |
| `agent_shared_knowledge` | mgmt | 81 | api 53 / runtime 8 / kernel 3 | 9 |
| `devops_agent_session` | runtime | 71 | api 26 / mgmt 12 / insight 1 / kernel 1 | 20 |
| `unified_user_mapping` | mgmt | 60 | kernel 24 / runtime 14 / api 6 / insight 1 | 16 |
| `meta` | runtime | 59 | api 27 / mgmt 24 / kernel 8 | 8 |
| `scene_market` | mgmt | 57 | api 24 / runtime 8 | 7 |
| `engine_capabilities` | runtime | 55 | mgmt 12 | 9 |
| `external_identities` | mgmt | 54 | runtime 7 / api 5 / kernel 5 | 12 |
| `user_repos` | mgmt | 49 | kernel 5 / api 4 / runtime 3 / insight 2 | 13 |
| `scene_knowledge_ref` | mgmt | 39 | api 14 / kernel 3 / runtime 2 | 8 |
| `chat_agent_session` | runtime | 37 | kernel 19 / api 13 / mgmt 1 | 5 |
| `user_feedback` | mgmt | 36 | api 22 | 5 |
| `chat_conversations` | runtime | 35 | kernel 14 / api 9 / mgmt 2 | 10 |
| `user_accounts` | mgmt | 31 | api 9 / runtime 1 | 5 |
| `task_messages` | runtime | 28 | api 1 / insight 1 / kernel 1 | 12 |
| `user_credentials` | mgmt | 28 | api 3 | 5 |
| `autopilot_tasks` | mgmt | 28 | runtime 12 | 6 |

> 读法：`SQL 引用数` 是代码里 `FROM/JOIN/INTO/UPDATE/TABLE <表>` 的命中数（粗粒度，用于排优先级，不作为验收口径）；`消费方` 只列出**非归属域**。

### 5.2 三条主要依赖链（决定施工顺序）

1. **运行域 → 管理域**（`runtime` 读 `managed_repos` / `devops_pipeline_scene` / `grape_accounts` / `scene_market` / `agent_shared_knowledge`）
   - 这类读取集中在"取仓库配置、取场景、取账号、取技能市场"，**都是"开始时读一次"的配置类数据**，最适合改成 `get_service("mgmt")` 远程只读。
   - 需要 mgmt 开放只读白名单（见 6.2）。
2. **管理域/api → 运行域**（`api` 读 `agent_tasks` / `agent_runtimes` / `events` / `issue` / `chat_*`；`mgmt` 读 `skill` / `engine_capabilities` / `runtime_credentials`）
   - 这是**列表页与详情页**的主要来源，调用频次最高，必须做成**聚合端点**（一次拿一屏），否则 N+1。
   - runtime 现有 9 个端点已覆盖 tasks / events / runtimes / issue_state / chat，需要补 `skills`、`engine-capabilities`、`runtime-credentials`（只读下发）、`meta`。
3. **洞察域 → 运行域**（`insight` 读 `agent_tasks` / `events` / `runtime_credentials` / `issue`）
   - 洞察是**只读分析面**，最优解不是同步 API，而是**消费 A2A 事件落自己的宽表**（已有 `obs_*` 表族），把"运行时读运行库"变成"事件驱动落库"。
   - 只有看板实时性要求高的少数指标才保留 API 读取。

### 5.3 写入跨域（必须 outbox 化）

现状跨域写集中在 `cross_write` 的 12 类动作（`engine_cap_*`、场景同步、A2A 场景用户、频道路由等），分布在 `scene_sync`、`event_pipeline`、`channel_directory`、`webhook`、`events`、`project_info/builder` 等模块。

独立后：
- 写方在**自己的库**写 `grape_t_xwrite_log`（pending）+ 业务行，同库同事务；
- 补偿 worker 调用目标域内部端点应用（`apply_fn` 必须幂等，UPSERT / 条件 UPDATE / 带幂等键 INSERT）；
- 失败重试 5 次、30s 退避，超限进死信并告警；
- 巡检每 5 分钟 `stats()`，pending 堆积 > 100 或 dead > 0 触发告警。

---

## 6. 接口补齐清单

### 6.1 已挂载可直接复用（16 个）

| 域 | 端点 | 用途 |
| --- | --- | --- |
| runtime | `/internal/tasks`、`/internal/tasks/{id}/messages` | 任务列表/消息（api 读） |
| runtime | `/internal/events`、`/internal/events/{seq}` | 事件流（api/insight 读） |
| runtime | `/internal/runtimes` | 引擎实例（api 读，已在线 1419 次） |
| runtime | `/internal/issues/{repo}/{n}/state` GET/POST | 需求状态（api 读/写） |
| runtime | `/internal/chat/conversations`、`/internal/chat/conversations/{id}/messages` | 会话（api 读） |
| insight | `/internal/logs`、`/internal/llm-usage`、`/internal/latency` | 指标写入（各域 → insight） |
| insight | `/internal/metrics/{repo}`、`/internal/slo/{repo}`、`/internal/alerts/{repo}`、`/internal/dashboard/overview` | 看板读取 |

### 6.2 需新增（按阶段落地）

| 编号 | 端点 | 目标域 | 消费方 | 契约要点 | 阶段 |
| --- | --- | --- | --- | --- | --- |
| N1 | `GET /internal/skills`（列表，支持 `repo_key`/`q`/分页） | runtime | mgmt、api | 返回 `id/name/version/summary/tags/updated_at`；不返回 OBS 明文地址 | P1 |
| N2 | `GET /internal/skills/{id}` | runtime | mgmt | 同上 + `content_url`（短时效签名） | P1 |
| N3 | `GET /internal/engine-capabilities` | runtime | mgmt | 引擎×能力矩阵，一次全量（< 500 行） | P1 |
| N4 | `GET /internal/runtime-credentials/{runtime_id}`（只读、短时效） | runtime | mgmt | **敏感**：仅返回当前可用的凭据元数据，不返回明文；调用方必须带服务令牌 | P1 |
| N5 | `GET /internal/meta/{namespace}` | runtime | mgmt、api | KV 元数据批量读 | P1 |
| N6 | `GET /internal/repos/{repo_key}`（只读聚合） | mgmt | runtime | 仓库配置+凭据状态+webhook 配置一次返回；替代 3 次单点调用 | P1 |
| N7 | `GET /internal/scenes/{repo_key}`（已定义，需**挂载**） | mgmt | runtime | 现有实现已在 `routes/internal.py`，只需在 `attach_internal_routes` 放行只读白名单 | P1 |
| N8 | `GET /internal/accounts/{id}`（只读，限字段） | mgmt | runtime、insight | 只返回 `id/username/display_name/org_id/status` | P1 |
| N9 | `GET /internal/knowledge/{id}` | mgmt | runtime、api | 知识条目（替代 `agent_shared_knowledge` 直读） | P1 |
| N10 | `GET /internal/user-mapping`（批量，按平台+外部 ID） | mgmt | runtime、kernel | 替代 `unified_user_mapping` 直读；批量 ≤ 200 键 | P1 |
| N11 | `POST /internal/tasks/batch`（批量取任务摘要） | runtime | api | 列表页一屏一次；替代 `agent_tasks` 直读 + JOIN | P2 |
| N12 | `POST /internal/stats/tasks`（聚合统计） | runtime | insight | 任务量/完成率/时长聚合，替代洞察侧宽 SQL | P2 |
| N13 | `POST /internal/xwrite/apply`（补偿应用端点） | 各目标域 | 各写方 | 入参 `{action, business_key, payload, idempotency_key}`；必须幂等 | P2 |

### 6.3 契约治理

- 所有内部端点必须写进 OpenAPI（`src/api/openapi.py` 合并 side-process 路由），并且 `operationId` 命名遵守 `domain_resource_action`（如 `runtime_skills_list`）。
- 内部端点只在 `/internal` 前缀下，由 `InternalAuthMiddleware` 校验服务令牌；**不进对外 `/api/*`**。
- 端点变更走契约快照门禁（已有 `tests/api/test_openapi.py`）。

---

## 7. 一致性与补偿

### 7.1 跨域写时序

```
写方事务:
  INSERT/UPDATE 业务行（本库）
  INSERT grape_t_xwrite_log(status=pending, idempotency_key=...)
  COMMIT
补偿 worker（30s 周期）:
  SELECT ... WHERE status IN (pending, failed) AND next_retry_at <= now
  → POST /internal/xwrite/apply（目标域，幂等）
  成功: status=applied;  失败: attempts+1, next_retry_at=now+30s; 超 5 次 → dead + 告警
```

### 7.2 幂等键

`idempotency_key = sha1(f"{target_domain}:{action}:{business_key}:{version}")`
- `business_key` 用业务主键（`task_id`、`repo_key+issue_number`、`rid+tag`）；
- `version` 用业务版本号或事件 `seq`，保证「同一业务对象的同一状态」只应用一次；
- 目标域 `apply_fn` 必须是 UPSERT 或带 `WHERE status='old'` 的条件更新。

### 7.3 读己之写

跨域写后立刻读该数据（如"改场景后立刻刷新场景列表"）：允许**读方在 N 秒内回退到写方端点**（GSB 同步读），不要在写方做双写。写方端点天然最新。

### 7.4 典型边界案例

| 案例 | 现做法 | 独立后 |
| --- | --- | --- |
| 建任务时扣减场景配额 | 同库事务（视图可直接改场景表） | outbox 写 `scene_quota_decrement`；配额超卖用"预占 + 确认"两段 |
| 接管结束写事件 | 直接 INSERT 运行域事件表 | 保持运行域内（同域）；若需通知洞察 → A2A 事件 |
| 引擎能力变更 | `cross_write` 已实现 | 保留，目标端点由 N13 承接 |
| 仓库删除级联 | 视图可跨域 UPDATE | mgmt 发事件 → runtime 消费清理（幂等、可重放） |

---

## 8. 数据搬迁与切换

### 8.1 前置（P4 开始前必须完成）

- 代码侧：G2/G3/G4 判据在**预发环境**已达成（视图 0、外域引用 0）。
- schema 基线：`mysqldump --no-data --skip-add-drop-table` 三库；每条迁移版本与账本一致。
- 容量评估：三库当前行数/大小 → 目标实例规格（建议 mgmt 4C16G、runtime 4C16G、insight 2C8G，存储按 1.5× 当前用量）。

### 8.2 建实例与账号

1. 三个 RDS 实例（同 VPC，不同安全组；仅允许应用机 + 巡检机访问）。
2. 每实例建一个库 + 一个应用账号 + 一个迁移账号 + 一个只读巡检账号。
3. 应用账号权限：`GRANT SELECT,INSERT,UPDATE,DELETE ON <own_db>.* TO <user>`；**不给任何其他库**。
4. 参数基线：`max_connections`（按进程池 × 1.5）、`innodb_buffer_pool_size`（≥ 实例内存 50%）、`slow_query_log=ON`、`long_query_time=1`。

### 8.3 搬迁步骤

| 步 | 动作 | 校验 | 预计耗时 |
| --- | --- | --- | --- |
| 1 | 全量快照 → 目标实例（mydumper/pt 并行） | 表数、行数、`CHECKSUM TABLE` 抽样 | 依数据量（当前三库合计 < 5G，约 20–40 min） |
| 2 | binlog 增量同步（停机前） | 延迟 < 5s | 持续 |
| 3 | 写冻结（维护模式 `/api/maintenance`） | 无写入 | — |
| 4 | 追平增量 + 终态校验（逐表行数 + checksum） | 全表一致 | 10–20 min |
| 5 | 切换 DSN（配置中心 → 各进程 `.env`） | 连接成功、`/api/ping` 200 | 5 min |
| 6 | 解冻、观察 | 错误率、p95、1054/1146 计数 = 0 | 24h |

**停机窗口建议**：周二/周四 02:00–06:00，预计 2–4 小时；分两次（先 insight，再 runtime+mgmt）。

### 8.4 切换顺序

1. **insight**（只读为主，影响面最小）→ 观察 24h
2. **runtime**（event + worker 同实例，需同时切）→ 观察 24h
3. **mgmt**（含 api 前端后端）→ 观察 72h

### 8.5 回滚

- 保留旧实例 7 天（只读账号可查）。
- 回滚 = 配置中心回切 DSN + 重启四进程；旧实例数据在冻结后未再写入，需按 binlog 反向补齐（保留 binlog 7 天）。
- 回滚触发条件：任一域 5xx > 1%（10 分钟窗口）或 p95 增幅 > 50%。

---

## 9. 分阶段实施计划

### P0 立项与冻结（约 1 周）

| 项 | 内容 |
| --- | --- |
| 产出 | ① 本方案评审通过；② 静态扫描脚本与 arch 守卫（新增外域表引用即红）；③ `sync_domain_views.py` 改为**只减不增**（新增视图需 allowlist）；④ 依赖矩阵看板（70 表 × 消费域）；⑤ 删除 60 个「代码已无跨域引用」的视图 |
| 判据 | 存量外域引用全部进 allowlist；新增即红；看板可自动刷新 |
| 风险 | 低。allowlist 太大导致"默认放过"，需每周复盘下降曲线 |

### P1 归属重定 + 模块归位（约 3 周，v1.1 提为第一优先）

| 项 | 内容 |
| --- | --- |
| 范围 | `C1 归属重定` 11 表 + `D 模块归位` 19 表 = **30 张表**（清单见 `cross-domain-disposition.csv`） |
| 动作 | ① 把 api 层直写/直读 runtime、mgmt 表的路由**实现**挪进归属域（对外 `/api/*` URL 与错误码不变，原处仅保留转发）；② kernel 里 `repo_cleanup` / `featured_order` / `domain_read` 等数据维护模块迁进归属域；③ 同步改 import 与测试 |
| 判据 | 这 30 张表在非归属域代码中的引用 = 0；`check-arch` 新增 R8（kernel 不得 import domains / 不得写域表）恒绿；对外契约快照不变 |
| 风险 | 中高。路由搬迁需保证 OpenAPI 快照与错误码不变，靠契约测试兜底 |

### P2 服务接口补齐（约 2–3 周）

| 项 | 内容 |
| --- | --- |
| 范围 | N1–N10（mgmt 只读白名单 + runtime 补齐）；消费方切到聚合端点，消除 N+1 |
| 判据 | 目标表引用 = 0；端点成功率 ≥ 99.9%、p95 ≤ 30 ms（内网） |
| 风险 | 中。mgmt 首次开放内部端点，字段白名单与 repo/org 作用域校验必须做足 |

### P3 跨域写分流（约 2.5 周）

| 项 | 内容 |
| --- | --- |
| 范围 | `C2` 30 张表按语义分三类（清单见 `cross-domain-write-channel.csv`）：**W1 回归属域 4 张**（agent_tasks / agent_type / issue_state / takeover_sessions）、**W2 lib 同步写 16 张**、**W3 outbox 9 张**；`grape_t_schema_migrations` 禁止跨域写 |
| 动作 | ① W2：目标域实现 public 写函数（幂等 + 审计行）+ 列级写账号；调用方按「本地先写 + outbox pending → 同步写 → applied/留给补偿」编排；② W3：实现 outbox 动作与补偿端点 N13；③ W1：把写路径搬进归属域 |
| 判据 | 跨域裸 SQL 写 = 0；W2 全部幂等（重放测试通过）；W2 失败 100% 有 outbox pending（A14）；目标域审计行覆盖 100%（A15）；lib 写 p95 ≤ 50 ms、失败率 < 0.1%（A16） |
| 风险 | 中高。写凭据跨域扩大爆炸半径 → 列级授权 + 函数化 + 审计 + 季度权限复核；跨域死锁 → 固定加锁顺序 + 300ms 超时 |

### P3.5 剩余跨域读接口化（约 1 周）

| 项 | 内容 |
| --- | --- |
| 范围 | `B API 聚合端点` 6 表 + `E 洞察事件/宽表` 1 表 + 7 张纯跨域读表（共 10 处引用） |
| 判据 | 全部走服务接口或事件；lib 通道启用数 = 0（若启用须逐条满足 3.4 并留审计） |

### P4 三实例与搬迁（约 1 周 + 两个停机窗口）

见第 8 章。

### P5 权限收敛与清视图（约 1 周）

| 项 | 内容 |
| --- | --- |
| 动作 | drop 182 个视图；`sync_domain_views.py` 下线；角色账号 REVOKE 跨库（本就无，做验证）；删除 `GRAPE_DOMAIN_LIB` 相关 fallback 代码 |
| 判据 | 视图数 = 0；`SHOW GRANTS` 只含本库；静态外域引用 = 0；24h 无 1142/1146/1054 |
| 风险 | 高（不可逆）。必须备份视图 DDL 与旧实例 |

---

## 10. 验收标准

| 编号 | 判据 | 采集方式 |
| --- | --- | --- |
| A1 | 三库视图数 = 0 | `information_schema.views` |
| A2 | 每域账号无跨库权限 | `SHOW GRANTS FOR <user>` |
| A3 | 代码外域表引用 = 0（allowlist 空） | 附录 A 脚本 + arch 测试 |
| A4 | 跨域端点覆盖率 = 100% | 依赖矩阵每行都有 `endpoint` 字段 |
| A5 | 跨域 API 失败率 < 0.1%（7 天） | `grape_gsb_client_requests_total` 按 status |
| A6 | 补偿：pending < 100、dead = 0、drain 成功率 ≥ 99.9% | `grape_t_xwrite_log` + 巡检 |
| A7 | 错误码 1054/1146/1142 计数 = 0（24h） | 慢日志/错误日志 |
| A8 | p95 延迟增幅 ≤ 15ms（相对基线） | `grape_http_request_duration_seconds` |
| A9 | 单实例故障演练：其余域主链路可用 | 演练记录 |
| A10 | 备份可恢复：任取一天备份恢复到临时实例并校验 | 演练记录 |
| A11 | 若启用 lib：100% 调用带 `channel=lib` 指标与审计；`SELECT *` 次数 = 0 | 指标 + 授权审计 |
| A12 | 不存在 lib 写任意表：所有 lib 写都经 public 写函数 | 静态扫描 + `SHOW GRANTS` 双验证 |
| A13 | W2 写函数 100% 幂等（唯一键或条件更新） | 评审清单 + 重放测试 |
| A14 | W2 写失败 100% 有对应 outbox pending 兜底 | `xwrite_log` 对账 |
| A15 | W2 写 100% 产生目标域审计行 | 审计表计数 vs 调用计数 |
| A16 | lib 写 p95 ≤ 50 ms、失败率 < 0.1% | `channel=lib_write` 指标 |

---

## 11. 风险登记

| 编号 | 风险 | 概率 | 影响 | 缓解 |
| --- | --- | --- | --- | --- |
| R1 | 遗漏隐藏的跨域读取（动态 SQL、f-string 拼表名） | 中 | 切换后 1054 | 静态扫描 + 灰度期间 general_log 采样 + 只读账号探测 |
| R2 | 补偿重复应用 | 中 | 数据错乱 | 幂等键评审 + 目标端 UPSERT + 对账脚本 |
| R3 | 列表页 N+1 导致延迟暴涨 | 高（若不做聚合端点） | 页面超时 | D4：批量端点先行（N11/N12） |
| R4 | mgmt 只读端点越权 | 中 | 数据泄露 | 字段白名单 + repo/org 作用域 + 服务令牌 |
| R5 | 迁移账本分裂 | 低 | 版本混乱 | 每域独立账本 + 启动自检 + 守卫测试 |
| R6 | 停机窗口超时 | 中 | 变更失败 | 预演两次（用生产快照恢复到预发实例） |
| R7 | 回滚后数据不一致 | 中 | 丢写 | 冻结写 + binlog 保留 7 天 + 反向补齐脚本 |

---

## 12. 运维变更

1. **监控**：每实例独立监控（连接数、慢查询、复制延迟）；新增看板「域隔离状态」（视图数、外域引用数、补偿队列）。
2. **备份**：每实例独立全备（每日）+ binlog（7 天）；恢复演练季度一次。
3. **扩容**：按域独立扩规格（这是独立实例的最大收益）。
4. **故障**：单实例故障时，其他域继续服务；受影响域的接口返回 503 + 明确错误码，不做跨域降级。
5. **发布**：迁移执行前必须 `--dry-run` 打印将执行的版本；每域独立执行、独立记账。

---

## 13. 附录

### 附录 A：盘点脚本（静态外域表引用）

```bash
# 1) 归属映射（每库基表）
mysql -h <host> -u<user> -p<pwd> -N -e "
  SELECT table_schema, table_name FROM information_schema.tables
  WHERE table_schema IN ('grape_mgmt_db','grape_runtime_db','grape_insight_db')
    AND table_type='BASE TABLE';" > owner.tsv

# 2) 视图清单（当前跨域可见面）
mysql -h <host> -u<user> -p<pwd> -N -e "
  SELECT table_schema, table_name FROM information_schema.views
  WHERE table_schema IN ('grape_mgmt_db','grape_runtime_db','grape_insight_db');" > views.tsv

# 3) 代码引用（粗粒度，用于排优先级）
python3 scripts/foreign_table_refs.py --src src --owner owner.tsv --out matrix.csv
```

### 附录 B：全量矩阵

见 `docs/weekly-11/independent-rds-foreign-table-matrix.csv`（70 行：表 / 归属域 / SQL 引用数 / 消费方分布 / 涉及文件数）、`docs/weekly-11/cross-domain-disposition.csv`（70 行：跨域读/写引用数 + 写方 + 处置分类 D/C1/C2/B/E/A）与 `docs/weekly-11/cross-domain-write-channel.csv`（30 行：W1/W2/W3/X 通道分配 + 理由）。

### 附录 C：分阶段工期与里程碑

| 阶段 | 工期 | 里程碑 | 交付物 |
| --- | --- | --- | --- |
| P0 | 1 周 | 冻结生效 | 守卫 + allowlist + 看板 + 删 60 个无引用视图 |
| P1 | 3 周 | 30 张表归属归位 | 路由搬迁 + kernel 模块归位 + R8 守卫 |
| P2 | 2–3 周 | 跨域读全部走接口 | N1–N10 + 契约快照 |
| P3 | 2.5 周 | 跨域写按 W1/W2/W3 分流完成 | 16 个 public 写函数 + 9 个 outbox 动作 + 补偿看板 |
| P3.5 | 1 周 | 剩余 7 表接口化 | 10 处引用清零 |
| P4 | 1 周 + 2 窗口 | 三实例上线 | 搬迁记录 + 校验报告 |
| P5 | 1 周 | 视图归零 | 权限收敛 + 回滚包 |

### 附录 D：本轮不做但需立项

- 洞察域宽表化（事件驱动落库），把"看板读运行库"彻底变成"读自己的宽表"。
- 跨实例链路追踪（trace_id 在 GSB 调用中透传已有基础，需补跨实例 span）。
- 数据归档与冷热分层（运行域事件表增长快，独立后按实例单独做分区）。
