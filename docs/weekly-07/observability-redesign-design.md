# Grape 可观测性（Observability）重构设计方案

> 状态：**待评审**
> 范围：grape 主服务（`src/` + 前端 `static/`）
> 起因：现有「设备监控」页只覆盖设备/引擎健康，需要升级为覆盖 **指标 / 日志 / 追踪 / 告警** 的可观测性模块
> 关联调研：观测云（GuanceCloud）开源/闭源能力盘点（见 §10 对照）
> 约束：grape 是**可自建部署的产品**，不能依赖商业 SaaS；多机部署下数据可见性必须明确

---

## 1. 背景与目标

### 1.1 现状问题

主服务侧边栏「设备中心」下有两个入口：

| 导航项 | 实际内容 | 问题 |
|---|---|---|
| Agent 运行时（runtimes.js, 2219 行） | 引擎/Worker 卡片、注册、技能、标签、场景绑定 | 职责清晰，保留 |
| **设备监控**（observability.js, 1602 行） | 4 Tab：概览(Worker+引擎) / 任务 / 异常任务 / 错误日志 | **名实不符**：叫"设备监控"，实际已有任务/日志内容；且缺指标、缺追踪、缺告警 |

同时后端已经积攒了一批未产品化的可观测能力（见 §2），**大量能力已经在跑，但用户看不到**。

### 1.2 目标

1. **名实一致**：`设备监控` → `可观测性`，并把设备/主机监控降级为其中的一个子视图
2. **三支柱成型**：指标、日志、追踪在其内可查、可下钻、可关联
3. **可定位问题**：从「哪个仓库/场景/任务/引擎出问题」出发，能一路点到根因（日志、失败原因、时间线）
4. **告警闭环**：规则配置 → 触发记录 → 通知 → 静默，全部在产品内完成（不再依赖外部 Prometheus 文件）
5. **多机部署可用**：主控 + N 个 worker 的数据在同一处可见，且明确每类数据的存放位置与上报路径

### 1.3 非目标（明确不做）

- 不自研列存/时序存储引擎（不学 GuanceDB）
- 不引入 ClickHouse / Elasticsearch 等重组件（自建产品的部署成本红线）
- 不做全量 OpenTelemetry 分布式链路（改造面过大）；改用「**以任务为骨架的轻量追踪**」（§6.4）
- 不做 RUM / 移动端 / 浏览器会话重放
- **不在用户面暴露平台基础设施**（数据库连接池、库表名、主机拓扑、平台级队列全貌）—— 详见 §5.3 可见性边界

---

## 2. 现状盘点（已有能力，带证据）

### 2.1 后端

| 能力 | 位置 | 说明 |
|---|---|---|
| Prometheus 指标 | `src/observability/__init__.py`（596 行） | 独立 Registry，30+ 指标，独立端口 `METRICS_ADDR=127.0.0.1:9090` |
| HTTP 中间件 | 同上 `metrics_middleware` | `grape_http_requests_total{method,route,status}`、`duration_seconds`、`in_flight_requests` |
| 任务生命周期指标 | 同上 | enqueued/dispatched/terminal/failed、`queue_wait_seconds`、`run_seconds`、`total_seconds`、`in_progress`、`queued`、`running`、**`stuck`** |
| LLM 用量指标 | 同上 `record_llm_usage` | `grape_llm_request_total`、`grape_llm_tokens_total{provider,model}` |
| MySQL 连接池指标 | 同上 `collect_mysql_metrics` | threads_connected/running/cached、aborted_connects、max_used_connections —— ⚠️ **平台基础设施，不进用户面（§5.3）**，归 admin 健康中心 |
| 引擎在线指标 | 同上 | `grape_runtime_online{mode}` |
| 业务采样器 | 同上 `BusinessSampler` | 定时采样（含 `grape_task_queued/running/stuck`） |
| 告警规则（文件态） | `deploy/alerts.yml`（83 行，8 条规则） | 任务卡住 / 失败率>50% / 全部引擎离线 / DB 池>80% / 连接中止 …**仅 Prometheus 文件，产品内不可见** |
| Ops 概览 API | `src/api/routes/dashboard.py:55` `/ops/overview` | 任务队列深度、运行中、卡住检测、引擎在线 |
| Ops 时间线 API | 同上 `:816` `/ops/timeline` | 事件时间线（按仓库） |
| 错误日志 API | 同上 `:612` `/ops/error-logs` + `:587` `/sources` | **扫文件**（主控 `devops-bot.log` + `data/logs/workers/*.log`），limit 100，无索引 |
| A2A 健康 | 同上 `:928` `/ops/a2a-health` | server_base_url 可达性 |
| 引擎探测 | `src/observability/engine_probe.py`（302 行）+ `/ops/probe-now` | 主动 CLI 探测，结果落 `agent_runtimes.probe_*` |
| 健康分 | `src/runtime/health_scorer.py` + 表 `runtime_health_scores`（51 行） | health_score / failure_rate / consecutive_failures / 窗口天数 |
| 请求延迟明细 | 表 `api_latency_log`（743 行） | request_id / method / path / status / **duration_ms / repo_key / user_id / source** |
| 主机遥测 | `src/api/main.py:370` `_server_metrics_loop`（15s） | 主控 CPU/内存/磁盘/tmp/带宽/load → 写 `runtime_credentials` |
| 进程内事件总线 | `src/observability/events.py`（74 行） | pub/sub，用于解耦通知/指标/活动记录 |
| 任务执行明细 | 表 `task_messages`（**49898 行**） | 每步事件：step_start / tool_use / text / step_finish，**已是任务级追踪的原始数据** |
| 事件与结果 | 表 `events`（360 行） | seq / agent_type / runtime / skill / status / duration / agent_logs / result |
| 实时健康 | `main.py:1009` `/health/realtime` | 参考 multica |
| 进程内埋点 | `main.py:757` 写 `api_latency_log` | 每个请求落一条 |

### 2.2 前端

- `observability.js`：4 Tab（概览 / 任务 / 异常任务 / 错误日志），KPI 卡片行 + Worker 卡片 + 引擎表（搜索、设备筛选、分页、10s 自动刷新、局部更新）
- 文案：`title: "📊 系统监控"`；导航 `app.nav.observability: "设备监控"`（**命名三处不一致**）
- 代码注释明确写着：`// const mt = metricsText || '';  // metrics 数据当前未使用` —— **指标数据已抓取但未使用**

### 2.3 关键结论

> grape 的可观测性**不是从零开始**，而是"采集层已具备 70%，消费层缺失"：
> 指标有（Prometheus）、日志有（文件扫描）、追踪原料有（task_messages 49898 行）、告警规则有（文件），
> **但都没有统一的产品入口、没有关联、没有留存、没有下钻。**

---

## 3. 差距分析

### 3.1 对照观测云能力模型

| 观测云能力 | grape 现状 | 差距等级 |
|---|---|---|
| Agent 采集（DataKit，650+ 技术栈） | 主机遥测 15s + 引擎探测 | 🟡 中（够用，维度待补） |
| 指标存储与查询（GuanceDB） | 进程内 Prometheus，**重启即丢** | 🔴 高 |
| 日志采集与检索（含 Pipeline 加工） | 文件扫描，limit 100，无索引 | 🔴 高 |
| 链路追踪（APM） | 无 trace_id，但有 task 级 step 流 | 🔴 高 |
| 统一查询语言（DQL） | 无 | ⚪ 不追（见 §4） |
| 仪表板/控制台 | 4 Tab，**未消费指标** | 🟡 中 |
| 告警引擎（规则/通知/静默/升级） | 只有 Prometheus 规则文件 | 🔴 高 |
| SLO / 事件中心 | health_scorer + events 表 | 🟡 中 |
| AI（Copilot / AI 可观测） | 无 | ⚪ 不追（P3 可选） |
| RUM / Profiling / 移动端 | 无 | ⚪ 不追 |

### 3.2 具体缺口清单（逐条可验收）

> **本节为一次调研（P0 前）的快照；缺口清单已在 §10.5 二次深度调研（2026-10-17）中重做**，
> 口径改为"能力矩阵（✅/🟡/❌/⏸）+ 规划阶段"，本表保留用于回溯当时判断。

| # | 缺口 | 影响 | 归属阶段 |
|---|---|---|---|
| G1 | 指标体系**无仓库/场景/引擎维度**（现只有 `source`、`runtime_mode`） | 无法回答"这个仓库今天失败率多少" | P0 |
| G2 | 前端**不消费** `/metrics` | 指标采了没人看 | P0 |
| G3 | 指标**无留存**（进程内 registry，重启清零） | 看不到趋势、无法做同比 | P0 |
| G4 | 日志**无索引/无检索**，且远端 worker 日志主控看不到（只能读本机文件） | 排障靠 grep 服务器文件 | P0 |
| G5 | **无 trace_id 贯通**（服务端请求 → 任务 → worker → agent step） | 无法"一次请求看全链路" | P1 |
| G6 | 告警**无产品内闭环**（规则改一次要动服务器文件、触发无记录、无静默） | 告警不可运营 | P0 |
| G7 | 主机遥测**无历史**（只存最新值到 `runtime_credentials`） | 看不到资源曲线 | P1 |
| G8 | 名称三处不一致（导航"设备监控" / 页面"系统监控" / 模块 observability） | 认知混乱 | P0 |
| G9 | 指标 ↔ 日志 ↔ 任务 ↔ 事件 **无关联跳转** | 排障要人肉串联 | P1 |
| G10 | LLM token 用量**无产品视图**（指标已采） | 成本不可见 | P2 |
| G11 | 现有指标里**混有平台基础设施维度**（MySQL 连接池等），若无边界直接上用户面 = 信息泄露 | 安全/合规风险 | P0 |

---

## 4. 设计原则与取舍

| 原则 | 说明 |
|---|---|
| **复用优先** | 已跑的 Prometheus 指标、ops API、引擎探测、健康分、`task_messages` 全部复用，不自起炉灶 |
| **零新增重组件** | 不进 ClickHouse/ES/Loki。存储在 **MySQL（已有）+ 本地文件滚动 + 内存环形缓冲** 三档内解决 |
| **任务为骨架** | grape 的核心可观测对象是「任务」，追踪模型围绕 task 而非通用 span |
| **主控为汇聚点** | 多机部署时主控是唯一数据平面出口；worker 通过既有心跳/上报通道带数据上来（不引入新 agent） |
| **渐进可关** | 新增采集/留存均带开关（env），默认开但可一键降级为现状 |
| **UI 先看效果** | 先出静态骨架 + 真实数据，确认视觉与交互后再定稿（对你的既有偏好） |
| **不暴露裸 ID** | 界面上用可读标签（`#1234 · coding · repo`），task_id 只在复制按钮里 |
| **受众分层** | 用户面只放"用户资产"的观测数据；DB/平台基础设施归 admin 健康中心（§5.3）。**过滤在后端强制**，不靠前端隐藏 |

**明确取舍**：
- ❌ 不做 DQL 类统一查询语言 —— 收益/成本比低，改用「**结构化筛选器 + 保存视图**」
- ❌ 不做全链路 OTel —— 改造 5 个引擎后端 + worker 协议，面太大；改做任务级时间线
- ✅ 指标留存用**「MySQL 分钟级聚合表 + 内存环形缓冲」**（成本可控、跨机天然可见）

---

## 5. 信息架构

### 5.1 导航调整（可观测性升为一级分类，子项收在侧栏）

> ✅ **已定稿（用户确认）**：可观测性作为一级分类（与「设备中心」平级），下辖 **3 个侧栏子项**（总览 / 设备与引擎 / 任务与日志）；告警不进侧栏。
> ⏳ 待讨论：**各分区内部的展示内容**（本方案 §5.2 为初稿，等讨论定稿后再开工）。

**设计约束**：可观测性升级为**一级分类**（与「设备中心」平级），分区入口放回侧栏（侧栏直点是更高频的操作），但**项数从 5 压到 3**，控制侧栏增量。

**变更前**（3 行，可观测性藏在设备中心里，名字还叫"设备监控"）：

```
设备中心                   ← 分组标题
 ├ ⚡ Agent 运行时
 └ 📊 设备监控             ← 二级项，名实不符
```

**变更后**（6 行）：

```
设备中心                   ← 分组标题（只剩管理入口）
 └ ⚡ Agent 运行时             管理视角：引擎注册 / 技能 / 标签 / 场景绑定

可观测性                   ← 一级分类，与设备中心平级
 ├ 📊 总览                (observability/overview)   巡检入口
 ├ 💻 设备与引擎           (observability/devices)    资源与在线状态
 └ ▶ 任务与日志            (observability/inspect)    ← 排障工作台（见下）

设置
 ├ ⎘ 仓库设置
 └ 🏢 组织管理
```

**5 → 3 的合并逻辑**（按"用户来干什么"合并，不是按数据源合并）：

| 原分区 | 去向 | 理由 |
|---|---|---|
| 总览 | **① 总览** | 保留 |
| 设备与引擎 | **② 设备与引擎** | 保留（对象独立，日常巡检必看） |
| 任务与链路 + 日志 | **③ 任务与日志** | 合成"排障工作台"：任务列表 → 点任务 → **执行时间线 + 该任务日志**同屏；顶部另有全局日志检索框。二者本就是同一个排障动线的两步，分开反而要点两次 |
| 告警 | **不进侧栏** | ①「总览」顶部做**告警横幅**（firing 数 + 最近一条，点击展开告警面板）；② 告警面板内再分「触发记录 / 规则 / 静默」子视图。告警配置是低频操作，不必占侧栏一格 |

**净增量**：该区域从 3 行 → 6 行（+3）。若仍嫌多，两个可选压缩：
- 把 `Agent 运行时` 移到「设置」分组（它是引擎配置页），可观测性独占原设备中心位置 → 净增 +2
- 或退回单个「可观测性」入口（不推荐，你已否掉）

**将来要拆回 5 项或加子项**，改侧栏是几行 HTML 的事，不影响后端与页面分区结构。

**命名统一**为 **可观测性 / Observability**（导航分类名、页面标题、模块名），清掉现有三处不一致（导航"设备监控" / 页面"系统监控" / 模块 `observability`）。

### 5.2 页面结构（3 分区，与侧栏子项一致）

| 分区 | 侧栏名 | 内容 | 来源 |
|---|---|---|---|
| 1 | **总览** Overview | 健康总分 + **告警横幅** + 核心 KPI（队列/成功率/P95/引擎在线/我的设备资源）+ 近期异常 Top；每个条目可点进对应分区 —— **图表级详设见 `observability-overview-page-design.md`** | 新增聚合 API |
| 2 | **设备与引擎** Devices | 现有设备监控全部内容（Worker 卡片 / 引擎表 / 主机 CPU·内存·磁盘·带宽 / 探测 / A2A）**+ 资源历史曲线** | 现有 ops/overview + 新增历史 |
| 3 | **任务与日志** Inspect | 上：任务列表（筛：仓库/场景/引擎/状态/时间）→ 点任务展开**执行时间线**（enqueue→dispatch→engine start→每步→terminal）**+ 该任务日志**；下：全局日志检索（级别/来源/仓库/关键字/时间范围） | 现有 tasks + `task_messages` + 错误日志 API 升级 |
| — | **告警面板**（非侧栏） | 从总览横幅进入：触发记录 / 规则（启停·阈值） / 静默 / 通知渠道 | 新增 |

> **分区内子视图（已定稿）**：分区由侧栏驱动、页面内不重复分区 Tab；分区内部的子视图：
> - **设备与引擎** —— 单视图（Worker 卡片 / 引擎表 / 资源 / 探测 / A2A），故不画子 Tab
> - **任务与日志** —— 4 个子 Tab：**任务 / 事件 / 异常任务 / 错误日志**
>   （异常任务是任务列表的一个视图：运行超时 + 失败，因此归本分区，不归设备分区；
>   **事件**是 §6.9 的统一事件视图，多来源 + 下钻，P1-4 已实现）
> - 只有一个子视图的分区不渲染单条 Tab 条

**跨页深链**（你的偏好：功能入口放详情页内；也是侧栏之外的第二条路径）：
- 引擎详情 → 「该引擎日志 / 该引擎近 1h 任务」→ `/observability/inspect?engine=<id>`
- 任务详情 → 「该任务的执行时间线 / 日志」→ `/observability/inspect?task=<id>`
- 仓库页 → 「该仓库可观测性」→ `/observability/overview?repo=<repo>`
- 告警记录 → 直达关联任务/引擎
- 侧栏子项与页面分区一一对应，URL 带分区 + 过滤条件，可分享/可收藏/刷新不丢

### 5.3 可见性边界（重要：这是**面向用户**的页面）

> 结论：**数据库 / 平台基础设施信息不进用户面**。可观测性只展示"用户自己资产"的观测数据；平台自身健康指标归 admin 健康中心。

**三层受众划分**：

| 层 | 面向 | 内容 | 位置 | 权限 |
|---|---|---|---|---|
| **L1 用户面** | 仓库成员 / 普通用户 | 自己的任务、自己有权的设备与引擎、自己仓库的日志、与自己资产相关的告警 | 主服务 `/observability`（3000） | `require_org_member` + **按仓库/用户强制过滤** |
| **L2 平台面** | 平台管理员 | MySQL 连接池/慢查询、DB 容量、全局限流与队列、全租户健康、平台主机资源 | admin 健康中心（8081，独立项目） | admin only |
| **L3 外部面（可选）** | 自建部署的运维 | 原始 Prometheus 指标 + `deploy/alerts.yml` | 独立端口 `METRICS_ADDR`（默认仅 `127.0.0.1:9090`，不对外） | 内网/抓取白名单 |

**明确"不出现在用户面"的清单**：

| 类别 | 说明 | 去向 |
|---|---|---|
| MySQL 连接池指标（`grape_db_threads_*`、`aborted_connects`、`max_used_connections`） | 平台基础设施，暴露即泄露技术栈与容量 | L2 admin |
| 任何库名/表名/IP/端口/主机名 | 内部拓扑 | 一律不出现在 L1（含错误文案） |
| 其他租户/其他仓库的 worker、引擎、任务、日志 | 越权数据 | 后端强制过滤，越权返回 404/空 |
| 全局任务队列全貌（跨仓库） | 部分泄露他人负载 | 用户面只给"自己可见范围"内的队列数 |
| 平台级限流/熔断状态 | 运维细节 | L2 |
| 原始 `/metrics` 全量输出 | 含上述所有 | L3（仅内网） |

**保留在用户面**（用户资产相关，且需按可见范围过滤）：

| 内容 | 过滤口径 |
|---|---|
| 任务队列/成功率/P95/卡住/失败 | 限当前用户可见仓库（`repo` 参数 + 后端鉴权） |
| 引擎与设备 | 仅自己有权的（个人设备 / 本组织 / 自己仓库绑定） |
| 资源曲线（CPU/内存/磁盘/带宽） | 仅自己的设备 |
| 日志 | 仅自己仓库 / 自己任务，且过 **redaction**（token/密码/密钥） |
| 告警 | 仅与可见仓库/自己设备相关的规则与触发 |

**实施要点（防越权）**：
1. **过滤在后端强制**，不是前端隐藏；所有 `/api/obs/*` 先解析调用者可见范围（复用现有 `require_org_member` + 仓库可见性口径）
2. 越权对象访问返回 **404**（不返回 403，避免探测存在性）
3. 日志/结果文本统一过既有 redaction（`_redact`），防止 token 从观测数据侧泄露
4. admin 侧如需 DB/平台指标，走 admin 自己的鉴权与页面，**不复用**用户面接口

---

### 5.4 能力归属：用户面 / 管理台 / 双端（2026-10-17 新增）

> 起因：二次调研列出的缺口（§10.5）里，有些放进 **grape-admin 管理台**更合适。
> 本节把每一项的归属**逐条定下来**，避免"两边都做一遍"或"该在管理台的塞进了用户面"。

**两端的定位（先对齐）**

| | 主服务·用户面（:3000） | 管理台 grape-admin（:8081） |
|---|---|---|
| 受众 | 仓库成员 / 研发团队 | 平台运营 / 管理员 |
| 视角 | **我的**（可见仓库 / 自己的设备 / 自己的任务） | **全局**（不按仓库过滤） |
| 已有 | 可观测性（总览 / 设备与引擎 / 任务与日志）+ 告警横幅与面板 | 健康中心（任务列表 / 设备监控 / 基础设施监控）、告警中心、设备/引擎/Agent/用户/组织/仓库管理、全局统计 |
| 数据 | `/api/obs/*`，**强制按可见范围过滤** | **已直连主控库**（`GRAPE_DB_URL` → `grape_test_db`，健康中心已有先例）→ 读 `obs_*` 无需新接口 |
| 前端 | 纯 HTML + JS（`static/js/pages/observability.js`） | Vue3 + Vite（`frontend/src/views/*.vue`） |

**归属五原则（按顺序判定）**

1. 含**跨租户 / 平台基础设施**信息（库、主机拓扑、全平台队列、他人数据）→ **只能管理台**（§5.3 已有清单）
2. 是**平台运营动作**（定义规则、配置策略、定目标基线、分摊、审计）→ **管理台**
3. 是**用户自助消费**（我的仓库 / 任务 / 设备）→ **用户面**
4. **两端都有消费方** → **双端**：口径与数据同源（同一批 `obs_*` 表、同一聚合函数），
   用户面=**我的范围**，管理台=**全局视图**（明确标注"全局"、可看到跨仓库汇总与排行）；**页面与组件不共享**
5. 拿不准、且用户面价值不明显 → **先只进管理台**（用户偏好：用户面侧栏要精简）

**逐项归属**

| 能力 | 用户面（:3000） | 管理台（:8081） | 依据 / 说明 |
|---|---|---|---|
| 设备遥测与资源曲线（已有） | ✅ 仅自己的设备 | ✅ 全局设备与基础设施监控（已有） | 现状即双端，口径一致 |
| 任务与日志检索（已有） | ✅ 仅自己仓库/任务 | ✅ 全局任务列表（健康中心已有）+ 跨租户日志检索走管理台 | 日志跨租户属运营动作（原则 1/2） |
| 告警（已有：横幅 + 面板 + 静默 + 飞书） | ✅ 可见仓库的规则与触发、自助静默 | ✅ 告警中心（已有，全局） | 现状即双端 |
| **告警治理增强**（级别/升级/重复抑制/聚合/通知时段）〔P1-1〕 | 🟡 **只读显示级别**；保留启停/阈值/自助静默（我的仓库） | ✅ **策略定义主场**：级别分级、升级规则、重复抑制间隔、聚合模式与周期、通知时段、**批量静默策略/维护窗口** | 升级/聚合/时段是**通知策略**＝平台运营动作（原则 2）；用户自助只需"启停+阈值+静默" |
| **数据缺失（Data Gap）检测**〔P1-3〕 | ✅ 我的仓库/引擎"该报数却没报" | ✅ **哪些 worker/引擎/仓库静默**（平台运维视角）+ 检测阈值配置 | 双端（原则 4）：用户关心自己的，平台需要全局视图 |
| **突变检测**〔P1-3〕 | ✅ 我的仓库成功率环比突变 | ✅ 全局异常仓库排行 + 灵敏度参数 | 同上 |
| **`obs_metric_rollup` 完整版**〔P1-2〕 | ⬜ 无页面（纯数据底座） | 🟡 **留存/基数白名单等策略配置** | 数据面在主服务，配置面在管理台 |
| **统一事件视图**〔P1-4〕 | ✅ 我的仓库/设备事件流（含**首次发生/持续时长**）+ 跳时间线日志 | ✅ **全局事件流**：跨租户 + 平台级来源（平台告警、`audit`） | 双端；`audit` 来源只进管理台（原则 1） |
| **SLO / 错误预算**〔P2-1〕 | ✅ 我的仓库 SLO 达成 + 预算剩余 + 燃尽图（**只读**） | ✅ **目标设定主场**（v1 平台统一设定，保证口径一致）+ 全局达成率总览 + 按仓库/引擎明细 + 平台级 SLI（引擎可用率） | 目标基线＝运营动作（原则 2）；用户面只读展示（回答 §12 Q6） |
| **交付效能 / DORA**〔P2-2〕 | ✅ 自己仓库的四指标趋势（**不做跨仓库排行**） | ✅ **全局视图**：所有仓库/组织排行、环比、异常仓库名单 | 双端（原则 4）；**排行只放管理台**（避免用户面横向比较心理） |
| **变更事件叠加**〔P2-3〕 | ✅ 我的仓库图上标记（PR 合并/提交/部署） | ✅ 全局变更流水 + 影响面（哪些仓库同期异常） | 双端 |
| **定期巡检报告**〔P2-4〕 | ✅ 仓库/个人日报 → 仓库成员 | ✅ 平台日报 → 平台运营/管理员 | 双端，两份报告内容不同（不发同一份给两个受众） |
| **保存视图 / 自定义面板**〔P2-5〕 | ✅ 我的视图（个人） | ✅ 运营视图（全局） | **各自实现**，不共享组件（受众与数据范围不同，共享反而耦合） |
| **基数保护**（维度白名单/上限）〔P2-5〕 | ⬜ 无入口 | ✅ 配置入口（平台级阈值） | 平台配置项 |
| **可用性拨测**〔P3〕 | ✅ **用户给自己部署的服务**建拨测（HTTP/TCP，结果看我的） | ✅ 全局拨测总览 + **平台自身端点**（如 www.grapedev.cn、各 worker 端口） | 双端；探测执行放 **worker/引擎节点**（就近探测，避免主控单点） |
| **日志解析管道**〔P3〕 | ⬜ 无入口 | ✅ 解析规则配置（正则/JSON → tag/field），两端共享解析结果 | 平台配置（原则 2） |
| **LLM 内容级观测**（prompt/completion 检索）〔P3〕 | 🟡 自己的任务详情内（数据本就在任务里） | 🟡 跨租户检索（**合规/排障用途，必须记审计**，建议默认关闭） | 高敏感：跨租户检索只在管理台且留痕 |
| **审计事件进事件视图**〔P3〕 | ⬜ 不出现（本来就是平台动作） | ✅ `audit_logs` 已存在，接入管理台事件流 | 原则 1 |
| **值班 / 升级轮值**〔P3〕 | ⬜ | ✅ 管理台（平台 on-call、通知对象分组） | 原则 2 |
| **成本 / 定价**（用户暂缓） | 🟡 我的仓库/任务消耗（暂缓） | 🟡 全局成本总览 + 按组织/仓库分摊（暂缓） | 双端；成本敏感 → 用户面只显示自己范围内 |

**双端口径约定（重要，防"两个真相"）**

1. **数据同源**：两端都读同一批 `obs_*` 表；**聚合口径必须共享同一份实现**
   （放主服务 `src/observability/`，管理台读库时复用同一 SQL/口径，不各写一套）
2. **管理台=全局视图**：页面明确标注"全局（不过滤仓库）"，避免与用户面数字对不上时产生误解
3. **鉴权不共享**：管理台用它自己的鉴权体系，**不复用**用户面 `/api/obs/*` 接口（§5.3 实施要点 4）

**实现落点（数据面在主服务，配置面在管理台）**

| 性质 | 落点 | 例子 |
|---|---|---|
| **数据面**（计算/采集/求值） | **主服务**（:3000 + worker） | `obs_metric_rollup` 写入、SLO 计算、事件聚合、Data Gap 检测、拨测执行 |
| **配置面**（策略/目标/参数） | **管理台**（写主控库的配置表） | 告警策略（级别/升级/聚合）、SLO 目标、检测阈值与灵敏度、留存与基数白名单、解析管道 |
| **消费面**（页面） | **各自前端**（不共享组件） | 用户面 `observability.js`；管理台 `views/*.vue` |

**定期巡检报告**建议放管理台（平台日报）**和**用户面（仓库日报）—— 但**分两份**，不发同一份内容给两个受众。

---

## 6. 数据层设计

### 6.1 实体与关联模型

```
仓库 repo_key ──┐
场景 scene_id ──┤
               ├──→ 任务 task_id ──→ 引擎实例 runtime_id ──→ 设备 device_name ──→ 主机 host
事件 seq ──────┘         │                                      │
                         └── task_messages(步/工具/输出)          └── 资源遥测(CPU/内存/磁盘/带宽)
```

**贯穿键（新增）**：`trace_id`
- 生成：事件入队时生成（`tr_` + 短随机），写 `agent_tasks.trace_id`
- 传播：服务端日志 → 任务下发 payload → worker 执行日志 → `task_messages.trace_id`
- 用途：一次流水线（vod→issue→coding→qa→sre）的所有任务共享同一 `pipeline_trace_id`，可串成一条链路

### 6.2 指标（Metrics）

**保留**：Prometheus 文本端点（`/metrics`）+ `deploy/alerts.yml` 兼容外部 Prometheus/Grafana（自建用户可选用）。

**增强**：
1. **补维度**（G1）：任务指标增加 `repo`（归一化到 owner/repo，**高基数风险 → 用白名单/截断保护**）、`scene`、`engine_type` 标签；HTTP 指标增加 `repo`（从请求上下文取）
2. **产品内聚合 API**（G2）：新增 `/api/obs/metrics/summary?range=1h&repo=...`，后端**解析自采指标或直接读聚合表**，返回给前端渲染（前端不解析 Prometheus 文本）
3. **留存**（G3）：新增聚合表

```sql
-- 分钟级聚合（写入方：BusinessSampler 扩展，1min 一次 upsert）
CREATE TABLE obs_metric_rollup (
  bucket_min   DATETIME NOT NULL,      -- 分钟桶
  metric       VARCHAR(64) NOT NULL,   -- task_enqueued / task_failed / task_duration_p95 ...
  dim_key      VARCHAR(191) NOT NULL DEFAULT '',  -- 维度拼接: repo=o/r|scene=s1|engine=opencode
  value        DOUBLE NOT NULL,
  cnt          INT DEFAULT 0,
  PRIMARY KEY (bucket_min, metric, dim_key),
  KEY idx_metric_bucket (metric, bucket_min)
);
```
- 保留：分钟级 7 天 → 小时级 30 天（复用 `table_maintenance.py` 的清理机制）
- 好处：**跨机天然可见**（都在主控库）、重启不丢、可直接出趋势图


**实施状态（2026-10-17，P1-2 已实现）与实测差异**

模块 `src/observability/metric_rollup.py`；表 `obs_metric_rollup`；
写入方 = `main.py` 的 60s 循环；读取方 = 总览趋势 + `GET /api/obs/metrics/series`（L1/L2）。

| 项 | 设计 | 实际 |
|---|---|---|
| 表结构 | `(bucket_min, metric, dim_key, value, cnt)` | `(resolution, bucket_start, metric, dim_key, repo_key, value, cnt, updated_at)`：加 `resolution`（分钟/小时同表）、加 `repo_key` 冗余（用户面按可见仓库过滤）、`bucket_start` 存 ISO(UTC) 文本 |
| 写入频率 | BusinessSampler 1min upsert | **独立 60s 循环 + 追赶式补齐**（重启/停机后自动补），每 tick 限量（分钟 ≤180、小时 ≤48），`asyncio.to_thread` 不阻塞事件循环 |
| 回填顺序 | — | **从新到旧**：先补最近的桶（用户看的是当下趋势），历史缺口在后续 tick 补齐 |
| 小时级 | 由分钟聚合 | **直接从原始表重算**（精确）：p95 没法用分钟值拼出来，重算比聚合近似更可信 |
| 主机资源 | 也进本表 | ❌ **仍走 `obs_runtime_samples`**（60s/7 天，资源曲线已在用）；本表**只做业务维度**，避免两处重复写 |
| `value`/`cnt` | value=值，cnt=次数 | 同上，但明确：**比率类 `cnt`=分母**（供加权平均），计数类 `cnt`=计数，p95 类 `cnt`=样本数 |
| 指标集 | 示例若干 | 14 个：`task_enqueued/done/failed/cancelled`、`task_success_rate`、`task_queue_wait_avg/p95`、`task_duration_avg/p95`、`token_input/output/cache/total`、`runtimes_online`（仅 global） |
| 留存 | 分钟 7 天 → 小时 30 天 | 一致（`cleanup()` 挂在 rollup 循环里，约每小时一次） |
| 维度可见性 | 按可见范围过滤 | 表里同时存 `repo:<key>` 与 `global`；**`global` 只在 L2 暴露**（用户面接口不接受 global 维度，也不聚合出全局数） |

**两个实测踩到的坑（都已修，值得记住）**

1. **复合 UNIQUE 的 UPSERT 没有被方言翻译**：`mysql_compat` 的 `ON CONFLICT(<单列>)` 正则只吃单列，
   复合键（`ON CONFLICT(resolution, bucket_start, metric, dim_key)`）漏翻 → MySQL 1064。
   已把正则放宽为接受列清单（MySQL 侧本来也不需要列清单），并在 `test_mysql_compat.py` 补了用例。
2. **同一函数作用域里复用同名导入别名会把前一个覆盖掉**：`main.py` 的 lifespan 里设备采样循环
   已有 `from src.observability import rollup as _obs_rollup`，我新增的循环用了同一个别名 →
   闭包在调用时解析到"后者"（设备 rollup 模块）→ `module has no attribute 'catch_up'`。
   真机日志才暴露（单测覆盖不到 lifespan）。教训：同一作用域里给不同模块起**不同的别名**。

**接口**

- L1 `GET /api/obs/metrics/series?metric=...&repo=&hours=&resolution=minute|hour`
  —— 只覆盖调用者可见仓库（`dim_keys` 只含自己的仓库，**不含 global**）；越权 repo → 404；未知指标/非法分辨率 → 400
- L2 `GET /api/admin/obs/metrics/series?scope=global|repo|all&repo=&...` —— 全局（含 `global` 维度）/单仓/全仓
- 总览 `GET /api/obs/overview` 的趋势图：**优先读 rollup**，无数据回退直查，`charts.source` 标注 `rollup`/`live`
  （不引入前端改动：payload 形状不变）

**为什么这是 SLO 的前提**：P2-1 的"分钟桶比例法"要按分钟判断"好桶/坏桶"，rollup 的分钟桶 + `cnt`
正好提供这个底座（`task_success_rate`/`task_done` 的 cnt 就是分母）。


4. **主机资源历史**（G7）：主机遥测 15s 采样 → 同样落 `obs_metric_rollup`（`metric=host_cpu, dim_key=device=<name>`），前端画 1h/6h/24h 曲线（用 canvas 轻量绘制，不引图表库）

### 6.3 日志（Logs）

**现状问题**：主控 `devops-bot.log`（JSON 结构化）+ `data/logs/workers/*.log`，API 每次扫文件（limit 100），**远端 worker 的日志主控根本读不到**。

**设计**：
1. **统一结构化字段**（`src/config.py` 日志格式器已有 JSON 基础，补字段）：
   `ts / level / component / msg / trace_id / task_id / repo_key / scene_id / runtime_id / user_id / status / duration_ms`
2. **落库（近期热数据）**：
```sql
CREATE TABLE obs_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  ts DATETIME(3) NOT NULL,
  level VARCHAR(8) NOT NULL,         -- ERROR/WARNING/INFO
  component VARCHAR(64) DEFAULT '',
  source VARCHAR(64) DEFAULT '',     -- master | worker-<cred_id>
  trace_id VARCHAR(40) DEFAULT '',
  task_id VARCHAR(40) DEFAULT '',
  repo_key VARCHAR(191) DEFAULT '',
  runtime_id INT DEFAULT 0,
  msg VARCHAR(1024) DEFAULT '',
  extra_json TEXT,
  KEY idx_ts (ts), KEY idx_level_ts (level, ts),
  KEY idx_task (task_id), KEY idx_trace (trace_id), KEY idx_repo_ts (repo_key, ts)
);
```
   - 只落 **WARNING/ERROR**（INFO 走文件）→ 控制量级（当前 ERROR/WARNING 量很小）
   - 保留 14 天，`table_maintenance` 清理
3. **远端 worker 日志上报**（G4 关键）：worker 侧 ERROR/WARNING 经既有上报通道批量推给主控（新增 `POST /api/obs/logs/ingest`，worker 已有 client 与 token 体系）
4. **检索能力**：级别 / 时间范围 / 来源 / 仓库 / 任务 / trace / 关键字（`msg LIKE`，量小可接受；后续量大再上全文索引）
5. **文件日志保留**：作为兜底与原始归档，API 降级路径不变

**实施状态（2026-10-17）与设计差异记录**：

| 项 | 设计 | 实际 |
|---|---|---|
| 表结构 | §6.3 的 DDL | 一致，**多加一列 `dedup_key`（UNIQUE）**：worker 批量上报天然会重试，靠它保证幂等（同 `obs_llm_usage` 的做法） |
| worker 上报 | 新增 `POST /api/obs/logs/ingest`，worker 侧改造 | **端点已按设计实现**（rta_ 凭据鉴权、source 取设备名），但主路径**复用了既有心跳 `log_lines`**：worker 本来就在心跳里上报最近 WARNING+ 行 → **worker 侧零改动**即可跨机可见；新端点留给后续批量/主动上报 |
| 主控日志落库 | "统一结构化字段 → 落库" | 用 logging bridge：root handler → 有界队列（2000）→ 后台 daemon 批量写（≤100 条/次）；**队满丢弃并计数**，`OBS_LOG_DB=0` 整体关闭（risk R2：不影响主链路） |
| 脱敏 | L1 redaction | **入库前**打码（token/密码/连接串/Authorization），非凭据内容不动（**IP 保留**：设备页本来就展示，打掉反而没法排障）；用户面查询再兜一层 |
| 可见性 | L1 仅自己仓库/任务 | `repo_key` 必须在可见集合内；**`repo_key` 为空的主控/进程日志不进用户面**（归 L2/L3）—— 主控日志里含库名/表名/他人路径，属 §5.3 排除项 |
| 前端 | 日志分区 | 「任务与日志 › 错误日志」改为库检索 + 关键字框 + 来源下拉（facets 来自库）；库不可用时**降级文件扫描**并在计数旁标注 |
| 留存 | 14 天 | `logs.cleanup()`，由 60s 采样循环按小时限流调用 |

**实测（2026-10-17）**：worker 侧 WARNING 经心跳落库（某 worker 的 `RuntimeClient ... Cannot connect`）；
主控 bridge 落库并**入库前打码**（`token=***`）；INFO 不落库（0 泄漏）；用户面按 26 个可见仓库过滤、
越权仓库 404。

### 6.4 追踪（Traces）—— 以任务为骨架

不引入 OTel SDK，用**已有 `task_messages`（49898 行）** 重建任务时间线：

```
Trace: tr_xxx  (流水线级)
 └─ [task] vod      ├ enqueue 10:00:01 (queue_wait 2s)
                    ├ dispatch 10:00:03 → engine:opencode@worker-0005
                    ├ engine_start 10:00:05
                    ├ step 1..N (tool_use: shell/read/…)   ← task_messages
                    └ terminal 10:02:10 (completed, 127s, tokens 12.3k)
 └─ [task] issue    …
 └─ [task] coding   …
```

**实现要点**：
1. `agent_tasks` 增列：`trace_id`、`pipeline_trace_id`（同一 issue/scene 流程共享）
2. 时间线 API：`GET /api/obs/trace/{trace_id}` → 聚合 tasks + task_messages + events + 日志命中
3. 前端时间线组件：横向时间轴 + 展开步骤（工具调用参数/输出摘要），失败点红色高亮 + 失败原因
4. **与日志互跳**：时间线任一步 → 过滤该时刻 ±30s 的日志

**实施状态（2026-10-17）与设计差异记录**：

| 项 | 设计 | 实际 |
|---|---|---|
| id 生成 | `agent_tasks` 增两列，由写入方填 | 列已加，但**不改三处 INSERT**（event_pipeline×2 / runtime.service）：两个 id 都由 (task_id / repo+issue) **确定性算出**（`tr_<sha1[:16]>`），启动时一次性回填 + 查询时惰性补齐 —— 历史任务立刻有轨迹（实测 315 条全部补齐），新任务自动成立 |
| 时间线 API | `GET /api/obs/trace/{trace_id}` | 已实现，且**同一个端点接受任务号**（`t_xxx`）→ 返回该任务的**整条流水线**（前端从任务列表点进来即可看全流程）；另有 `GET /api/obs/trace?repo=&issue_id=` 直接按 issue 看流程 |
| 聚合内容 | tasks + task_messages + events + 日志命中 | 全部实现：每任务给出 enqueue→dispatch→terminal、**排队等待/耗时**、`引擎@设备`、token 总量（来自 `obs_llm_usage`）、失败原因、步骤（`task_messages`，截断 400 字符、单任务上限 200 步）、事件（`event_seq` 关联 events）、内联日志命中（按 task_id） |
| 可见范围 | L1 按可见范围过滤 | 只返回调用者可见仓库的任务；过滤后为空 → 路由 **404**（哈希 id 不可枚举，空即不存在） |
| 与日志互跳 | 时间线 → 该时刻 ±30s 日志 | 双向：时间线每任务 → 「查看日志」（日志 tab 带 `task_id` 过滤 chip，可一键清除）；任务日志窗口 → 「时间线」按钮 |
| 前端 | 横向时间轴 + 展开步骤 + 失败红 | 弹窗式：任务卡片（左色条红/绿/琥珀）+ 轨道时间 + `<details>` 可展开步骤（工具入参/输出摘要）+ 失败原因红色 + 内联日志命中 |

**实测（2026-10-17）**：真机一条 issue `#1` 的流水线 —— 4 个任务 `vod → issue → coding → qa` 全部
`completed`，引擎分别 `hermes@某 worker / hwcloud@某 worker / hermes@某 worker / opencode@某 worker`，
耗时 153~532s、排队 0~2s、token 分别 692k / 60k / 4.41M / 748k，事件 若干连续事件（issue_opened + 3×a2a_event）。

### 6.5 设备 / 主机遥测（并入 Tab2）

- 保留现有 15s 采集（主控 `_server_metrics_loop`、worker 心跳）
- 新增：写入 `obs_metric_rollup`（`host_cpu/host_mem/host_disk/host_tmp/host_bw/host_load`，`dim_key=device=<name>`）
- 新增：设备详情抽屉 → 资源曲线（1h/6h/24h）+ 该设备上的引擎 + 该设备近 1h 任务
- 现有"设备监控"的 Worker 卡片/引擎表/探测/A2A 全部保留并迁入此 Tab

### 6.6 留存与跨机部署可见性（你的关注点）

| 数据 | 存放位置 | 跨机可见？ | 可见层 | 说明 |
|---|---|---|---|---|
| 指标（原始） | 各进程内存（Prometheus registry） | ❌ 仅本机 `/metrics` | **L3** | 可选用外部 Prometheus 抓 |
| 指标（聚合，业务维度） | **主控 MySQL `obs_metric_rollup`** | ✅ | **L1**（按可见范围过滤） | 唯一长期趋势来源 |
| 指标（聚合，平台维度如 DB 池） | —— **不落用户面** | — | **L2** | 归 admin 健康中心，用户面接口不提供 |
| 日志（热） | **主控 MySQL `obs_log`** | ✅ | **L1**（仅自己仓库/任务 + redaction） | worker 上报后可见 |
| 日志（全） | **各自机器文件** | ❌ | L3 | 兜底归档，排障可 SSH/或后续加下载入口 |
| 追踪 | 主控 MySQL（tasks/messages/events） | ✅ | **L1**（按可见范围过滤） | 已天然集中 |
| 设备遥测（最新值） | 主控 MySQL `runtime_credentials` | ✅ | **L1**（仅自己的设备） | 现状 |
| 设备遥测（历史） | 主控 MySQL `obs_runtime_samples`（60s/7 天） | ✅ | **L1**（仅自己的设备） | 已实现（资源曲线的数据源） |
| 业务指标（分钟/小时聚合，P1-2） | 主控 MySQL **`obs_metric_rollup`**（分钟 7 天 / 小时 30 天） | ✅ | **L1 只出可见仓库**；`global` 维度仅 L2 | 新增（§6.2） |
| 告警事件（P0/P1-1） | 主控 MySQL `obs_alert_events`（**留存 90 天，只清已恢复的**） | ✅ | L1（可见仓库 ∪ 可见设备，仅 `level='user'`）/ L2 全局 | 留存策略见审计（2026-10-17 拍板） |
| LLM 用量台账 | 主控 MySQL `obs_llm_usage`（**保留 1 年，更早搬 `obs_llm_usage_archive`**） | ✅ | L1（仅自己）/ L2 全局 | 年度归档见审计（2026-10-17 拍板） |
| 后台循环健康（自监控） | 进程内 + Prometheus（`grape_obs_loop_*`） | ✅ | `/metrics` + **L2 `GET /api/admin/obs/loops`** | 新增（审计 #4） |
| 告警规则/触发 | 主控 MySQL（新增 2 表） | ✅ | **L1**（L2 另有一套平台级规则） | 新增 |

**结论**：除"原始指标"与"全量文件日志"外，**全部集中在主控库**，多机部署时运维只需看主控。
**同时**：用户面与平台面在同一份存储上做**读取时的可见范围过滤**——存储集中不等于人人可见（§5.3）。

---

### 6.7 SLO / 错误预算（规划 P2-1，本次调研新增）

**为什么要做**：我们现在给用户的是一堆"当前值"，没有**结论**。SLO 把"健康"变成可承诺、可否决的一句话：
"本月任务成功率 ≥ 99% —— 当前 99.4%，错误预算剩余 63%"。

**三类 SLI（只做三类，不做通用 DSL）**

| SLI | 定义 | 数据来源 | 类型 |
|---|---|---|---|
| 任务成功率 | 成功任务数 / 任务总数 | `agent_tasks`/rollup | 比例 |
| 任务排队时延 | `enqueue → dispatch` 的 P95 秒数 ≤ 阈值 | `obs_metric_rollup`（已有该字段原料） | 阈值/时延 |
| 引擎可用率 | 引擎在线时长 / 总时长 | `obs_runtime_samples` + 心跳 | 比例 |

**数据模型（3 张表，沿用现有规范）**

```
obs_slo(id, name, sli_type, scope_type[repo|global|engine], scope_key, target,
        window_days, threshold_json, enabled, created_by, created_at)
obs_slo_snapshots(id, slo_id, bucket_start, window_start, window_end,
                  good, total, value, budget_total, budget_consumed,
                  state[ok|warning|breached], computed_at)   -- 按小时算，唯一键 (slo_id, bucket_start)
```

**扣减口径（比观测云更简单、且等价）**：观测云按"异常事件覆盖时长**合并**后扣除"。
我们**按分钟桶比例法**：`坏桶数 / 总桶数` —— 同一分钟无论触发多少次都只算一桶，
**重叠天然合并**，且直接复用 `obs_metric_rollup`，不需要额外的事件时长合并逻辑。
错误预算 = `(1 - target) × 总桶数`；燃尽曲线 = 逐桶累计消耗。

**SLO 违约如何通知**：**不新建通知链路** —— 违约写一条事件（`source='slo'`）到统一事件视图（§6.9），
复用告警的静默/通知渠道。这与观测云"事件中心只聚合不产生、告警策略统一投递"的分层一致。

**实施状态（2026-10-18，已实施）**：模块 `src/observability/slo.py` + 表 `obs_slo`/`obs_slo_snapshots`；
循环挂在现有 30s 告警循环里（算 SLI → 落小时快照 → 同步违约事件 → 清 90 天前快照）；
违约写 `source='slo'` 进统一事件视图（`rule_id=0`，仓库范围 `level=user`、平台范围 `level=platform`），
恢复自动 resolved；用户面只出可算的两条（引擎可用率属平台目标，L1 给 404）。
**三处偏离设计稿（记录原因）**：
1. 桶粒度取**小时**不是分钟 —— 分钟明细只留 7 天，而窗口默认 30 天；方法仍是桶比例法（等价）。
2. 可用率数据源另立 `obs_runtime_samples.online` 列（按心跳新鲜度逐点记）——
   `health_score` 来自 `runtime_health_scores` 是"任务健康度"不是在线，dev 库 7454 行实测全为 0；
   且**可用率只从该 SLO 建立时刻起算**（建列前的历史行是"未知"，当离线会把可用率长期按 0 算，
   真机实测当天误报 0% 违约）。
3. 预算消耗 ≥75% 记 `warning`（设计只给了 ok/warning/breached 三态，阈值由本仓定）。

**API / UI**：`GET|POST /api/obs/slo`、`GET /api/obs/slo/{id}`（含燃尽序列）；
总览顶部增「健康结论」卡（3 个 SLO 的达成态 + 预算剩余），点击展开燃尽图。

**边界**：仓库范围 SLO = **L1 用户面**；引擎可用率这类平台目标 = L2（`admin` 健康中心）。

---

### 6.8 交付效能 / DORA（规划 P2-2，差异化最强）

**为什么值得**：通用平台拿不到"issue → 任务流水线 → PR 合并"的语义。我们**已经**有这条链路
（`issue_state` 5 个阶段、`events.pull_request_merged` 8 条、任务的 git 提交、`pipeline_trace_id`）。
把它做成研发团队看得懂的**交付效能**，是"内建可观测性"最不可替代的一块。

| DORA 指标 | 我们的算法 | 现成原料 |
|---|---|---|
| 部署频率 | 单位时间 `pull_request_merged` 数（按仓库/周） | `events` |
| 变更前置时间 | `issue_opened` → 首个 `pull_request_merged` 的平均/中位时长 | `events.created_at` |
| 变更失败率 | 关联 issue 的任务最终失败（或 qa 未过）占比 | `agent_tasks.status` + `issue_state.stage` |
| MTTR | 告警/失败事件**首次发生 → 恢复**的中位时长 | `obs_alert_events`（fired/resolved 时间） |

**诚实记录的缺口**：`issue_state` 只有 `updated_at`，样本里 `created_at == updated_at` →
**没有"阶段进入时间"**，前置时间无法按阶段拆解。两条路：
① 从 `events.a2a_event`（107 条，带 seq/时间/agent_type）**重建**阶段迁移时间；
② 给 `issue_state` 增 `stage_entered_at`（新写入点填，历史回填）。
选 ①（零写入点改动，同 `trace_id` 的套路）。

**字段对齐**：对外导出/命名向 OTel **`cicd.pipeline.run.*`**（spans 已 RC）靠 —— 这是我们对齐行业语言的入口
（GenAI 约定尚未稳定，暂不追，见 §10.5）。

---

#> **现状与后续（2026-10-17）**：本节描述的统一事件视图已按 P1-4 落地（6 来源归一 + 下钻 + L1/L2 端点），
> 但落地后审计发现**信息结构需要重做**（8 列表格承载不了异构事件；另有 3 个显示 bug 已修）。
> 重设计方案见 **`observability-events-view-redesign.md`**（时间轴流 + 合并过滤 + 游标分页 + 自动刷新）；
> 审计全过程与其它发现见 **`observability-audit-2026-10-17.md`**。本节保留为"来源与字段口径"的定义。

## 6.9 统一事件视图（规划 P1-4）

观测云事件中心的定位是"**只聚合、不产生**"：来源打标（`df_source`），并额外记录
**首次发生时间**与**已持续时长**（`df_issue_start_time / df_issue_duration`）。
我们照搬这个概念，但**不新建表**——按来源联合查询（架构上保持分表，避免大迁移）：

| 来源 `source` | 物理表 | 关键字段 |
|---|---|---|
| `monitor` | `obs_alert_events` | rule_id / dimension / fired_at / resolved_at / state |
| `slo` | `obs_alert_events`（`source='slo'`，P2-1 写入） | 同左 |
| `change` | `events`（`pull_request_merged` 等，P2-3 扩展） | event_type / repo_key / issue_id |
| `task` | `agent_tasks`（失败/超时，异常任务视图已有） | status / task_id / repo_key |
| `audit` | `audit_logs`（L2 可选，P3） | actor / action |

统一输出字段：`source / level / title / repo_key / issue_id / task_id / trace_id /
started_at / duration_sec / state`；**首次发生与持续时长**由"同 (source, dimension) 的
未恢复事件"回溯得到（`obs_alert_events` 已有该语义；`change`/`task` 类为瞬时事件，duration=0）。

**与已有能力的关系**：告警面板退化为"事件视图的一个来源过滤器"；时间线/日志通过
`task_id`/`trace_id` 与事件**双向互跳**（P1-4 的核心价值：从"发生了什么"到"为什么"）。

**实施状态（2026-10-17，P1-4 已实现）与实测差异**

模块 `src/observability/events_view.py`；用户面 `GET /api/obs/events`，管理台 `GET /api/admin/obs/events`。

| 项 | 设计 | 实际 |
|---|---|---|
| 来源 | monitor / slo / change / task / audit | **monitor / gap / mutation**（都在 `obs_alert_events`，按 `source` 区分）/ **task**（卡住+失败）/ **change**（PR 合并）/ **audit**（**仅 L2**）。`slo` 待 P2-1 |
| 物理表 | 分表聚合 | 与设计一致（不新建表）；**告警来源复用 `alerts.list_events`**（它已处理"可见仓库 ∪ 可见设备"），**任务来源复用 `dashboard.get_ops_overview`**（同一套 repo/org 过滤） |
| 首次发生 / 持续时长 | 回溯得到 | 告警 `first_fired_at`（数据缺失事件里它就是静默起点）/ 任务 `started_at` / 变更 `started_at`；`duration_sec` + `ongoing`（未结束 → 用当前时间） |
| `total` 语义 | — | **本页条数**（各来源各取 limit 后合并截断），不是数据库总数 |
| 处置建议 | "下一步" | `next_step` ∈ device/timeline/logs/issue，前端渲染成下钻链接（**后端不写文案**，只给动作） |
| 告警面板 | "退化为来源过滤器" | **保留**为 monitor 来源的快捷面板（含规则/静默/通知渠道管理，事件视图不需要这些）；两者数据同源 |

**实测发现的一个越权面（已修）**：`dashboard.get_ops_overview` 的任务过滤是**组织级**（不是"我的可见仓库"），
直接拿它当用户面数据源会把**同组织里别人仓库**的失败任务带进事件视图。
所以用户面在 `_task_events` 之后**再按可见仓库收敛一次**（`_visible_filter`）；管理台全局视角不做这层收敛。
**教训**：复用别的口径时，先确认它的过滤维度是"组织"还是"可见范围"——两者不是一回事。


---

### 6.9.1 两条硬规矩（真机事故换来的）

1. **SQL 里不写字面 `%`**（`LIKE 'runtime:%'` 这种）：pymysql 带参执行时会做一次 Python `%` 格式化，
   字面 `%` 会被当格式符 → 抛错；调用方若 try 吞掉就是**静默丢数据**（用户面事件视图曾因此丢掉全部
   告警类来源）。用 `substr(x,1,8)='runtime:'` / `instr()` 代替。兼容层已统一把字面 `%` 逃逸成 `%%` 兜底，
   但"别在 SQL 里写字面 %"仍是首选写法。
2. **`I18N.t()` 只传叶子键**：命中命名空间对象时真实 i18n 会**原样返回对象** → 页面显示 `[object Object]`；
   缺键时返回 key 本身（truthy），所以 `t(k) || 兜底` 永不生效。回退统一用 `_obsTOr(key, fallback)`
   （值 === key 视为未命中）。

## 6.10 数据缺失（Data Gap）与突变检测（P1-3，2026-10-17 已实现）

**为什么单列**：静默失效最危险 —— 没有指标、没有错误，只是**数据不再来了**，一切看起来正常。
观测云把 Data Gap 单列为事件级别不是偶然。模块：`src/observability/gap.py`。

**两类探测**（规则 `kind='gap'`，`metric` 指定目标；与指标规则同表同生命周期，`source='gap'`）

| 规则 | 判定 | 依据 | 默认 |
|---|---|---|---|
| `runtime_silent`（`gap_runtime`） | 设备/引擎超过 `threshold` 秒没有心跳 | `runtime_credentials.last_seen_at` | **开**（阈值 900s） |
| `repo_silent`（`gap_repo`） | 近 `window_sec` 有任务活动的仓库，最近 `threshold` 秒**完全没有任务活动** | `agent_tasks` 的 created/started/completed | **关**（低活跃仓库天然长期无任务，开启前先看历史频率） |

**两条抑制规则（防"噪音源"，测试钉死）**

1. **从未上报过**（`last_seen_at` 为空）→ 不算缺失（那是没装成/没用过）
2. 超过 **7 天**没上报 → 视为**已退役凭证**，不再产生事件（否则一条死凭证据会永久刷告警）；
   但 **L2 静默名单仍列出它们**（`beyond_retire_window=true`），供运维清理

**事件语义**：`first_fired_at` = **静默起点**（最后一次数据时间 + 阈值）→ "已持续"就是静默时长；
`value` = 静默秒数（前端按分钟/小时/天展示）。数据恢复 → `resolved`。

**突变检测（环比，统计法不上 ML）**：派生指标 `task_success_rate_mutation`
= 本窗口成功率 − 上一窗口成功率（窗口各 30 分钟，与规则 `window_sec` 一致）；
种子规则 `task_success_rate_drop`（`lt -0.2`，即跌 20 个百分点）。
**两个窗口各需 ≥3 个终态任务，样本不足则不出指标**（该维度本轮跳过）——
否则 1~2 个任务的窗口会把噪声当突变。`_event_source()` 按指标名后缀判为 `mutation`。

**可见性（重要）**：设备维度事件（`dim_key='runtime:<id>'`）**没有 repo_key**，
用户面按"**看得见该设备**"过滤（`list_events` 的可见性 = 可见仓库 ∪ 可见设备，
设备口径复用设备列表的 `_cred_visibility`）；静默同理（越权设备的事件静默不了 → 404 语义）。
平台级（`level='platform'`）的缺失事件一样不进用户面（§5.3）。

**接口**：用户面 `/api/obs/alerts` 照旧（事件带 `source` 字段 + `dim_key` 做"对象"列）；
**L2 新增** `GET /api/admin/obs/gaps`（全局静默名单：设备/仓库 + 静默时长 + 退役标记，
`include_retired=false` 可只看会产生事件的）；阈值就是 `kind=gap` 的"静默多少秒算缺失"，
走同一个策略写入口 `PUT /api/admin/obs/alerts/rules/{id}/policy` 的 `threshold` 字段。

---

## 7. 后端 API 设计

| 方法 | 路径 | 说明 | 阶段 |
|---|---|---|---|
| GET | `/api/obs/summary?repo=&range=1h` | 总览：健康分/告警数/核心 KPI/趋势数组 | P0 |
| GET | `/api/obs/metrics/series?metric=&dim=&range=` | 指标曲线（从 rollup） | P0 |
| GET | `/api/obs/logs?level=&source=&repo=&task_id=&trace_id=&q=&since=&limit=` | 日志检索（分页游标） | P0 |
| POST | `/api/obs/logs/ingest` | 远端 worker 上报（worker token 鉴权） | P0 |
| GET | `/api/obs/tasks?...` | 任务列表（多维筛选，复用现有结构） | P1 |
| GET | `/api/obs/trace/{trace_id}` | 任务链路时间线 | P1 |
| GET | `/api/obs/devices` | 设备列表 + 最新资源 + 引擎 | P1 |
| GET | `/api/obs/devices/{device}/series` | 设备资源历史 | P1 |
| GET/POST/PUT | `/api/obs/alert-rules` | 告警规则 CRUD | P0 |
| GET | `/api/obs/alerts` | 触发记录（含状态：firing/resolved/silenced） | P0 |
| POST | `/api/obs/alerts/{id}/silence` | 静默 | P1 |
| GET | `/api/obs/llm-usage?repo=&range=` | LLM token/成本视图 | P2 |

**统一鉴权与过滤约定（对所有 `/api/obs/*` 生效，§5.3）**：
- 入口统一 `Depends(require_org_member)`；**先解析调用者可见范围**（可见仓库集合 + 自己的设备集合），再拼查询条件
- 越权对象（他人仓库/设备/任务/trace）一律 **404**，不返回 403（避免探测存在性）
- 响应文本（日志 msg、失败原因、结果）统一过 redaction
- **不提供**任何平台基础设施接口（DB/连接池/主机拓扑/全租户数据）——这些归 admin 健康中心

**兼容**：`deploy/alerts.yml` 保留（外部 Prometheus 用户），产品内规则表与之一一对应（内置 8 条作为种子）。其中平台级规则（DB 池、连接中止）**默认不在用户面展示**，仅供 L2/L3。

---

## 8. 前端设计

- **页面**：`observability.js` 保持单文件（现 1602 行），按 Tab 拆函数；新增 Tab 组件式渲染
- **视觉**：沿用现有设计语言（`--b/--c/--t*` 变量、KPI 卡片行、`tw` 表格、`bt` 按钮）；趋势图用轻量 canvas（不引 ECharts）
- **交互**：
  - 自动刷新（10s）保留，"实时"指示保留
  - 筛选状态**入 URL**（`?tab=logs&repo=...`）→ 可分享、可深链、刷新不丢
  - 空态 / 加载态 / 失败态三态齐全
- **i18n**：zh/en 同步补 `locales/{zh,en}/observability.json`；导航名统一
- **文案**：不出现裸 task_id/trace_id；展示用 `#issue · agent · repo`，ID 放复制按钮

---

## 9. 告警中心设计

### 9.1 规则模型

```sql
CREATE TABLE obs_alert_rules (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(64) UNIQUE NOT NULL,
  metric VARCHAR(64) NOT NULL,          -- 取自指标名
  comparator VARCHAR(4) NOT NULL,       -- gt/lt/gte/lte
  threshold DOUBLE NOT NULL,
  window_sec INT DEFAULT 300,
  for_sec INT DEFAULT 300,              -- 持续满足时长（抑制抖动）
  severity VARCHAR(8) DEFAULT 'warning',-- info/warning/critical
  dim_filter VARCHAR(191) DEFAULT '',   -- 可选: repo=o/r
  enabled TINYINT DEFAULT 1,
  notify_channels VARCHAR(191) DEFAULT '', -- feishu/im/email
  created_by INT, created_at DATETIME, updated_at DATETIME
);

CREATE TABLE obs_alert_events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  rule_id INT NOT NULL, rule_name VARCHAR(64),
  severity VARCHAR(8), status VARCHAR(16),  -- firing/resolved/silenced
  metric VARCHAR(64), value DOUBLE, threshold DOUBLE,
  dim_key VARCHAR(191) DEFAULT '',
  first_fired_at DATETIME, last_seen_at DATETIME, resolved_at DATETIME,
  silenced_until DATETIME NULL,
  link_task_id VARCHAR(40) DEFAULT '', link_trace_id VARCHAR(40) DEFAULT '',
  KEY idx_status_time (status, last_seen_at), KEY idx_rule (rule_id)
);
```

### 9.2 求值引擎

- **进程内**：每 30s 对启用规则求值（`main.py` lifespan 内 `_obs_alert_loop`，`asyncio.to_thread`）；
  用户面规则按**仓库维度**求值（每个有活动的仓库一条事件），平台级规则按全局维度求值
- **触发**：达到阈值且持续 `for_sec` → 写 `obs_alert_events`（firing）→ 推送到通知渠道
- **恢复**：条件不满足 → 标 resolved（不重复通知）
- **静默**：`silenced_until` 内不通知，仍记录（用户面可自助静默自己的事件）
- **内置规则**：从 `deploy/alerts.yml` 平移为种子（**7 条**：3 条用户面 `task_stuck` /
  `task_failure_rate` / `task_queue_backlog`，4 条平台级 `all_runtimes_offline` /
  `db_pool_usage` / `db_aborted_connects` / `http_error_rate_5xx`），`seed_source` 记原 alert 名

**实施状态（2026-10-17）与设计差异记录**：

| 项 | 设计 | 实际 |
|---|---|---|
| 种子规则数 | 8 条 | **7 条**（`deploy/alerts.yml` 实际只有 7 条，按文件为准） |
| 用户面规则 | 未细分 | 只有 **3 条**进用户面（按仓库维度）；"全部引擎离线"按 alerts.yml 的语义属平台级，不进用户面 |
| 通知投递 | "经既有 IM 通道（飞书）推送" | **已接通**：`feishu` 渠道 = 用**收件人本人**的 chat bot 发私聊（复用任务通知那条链路 `_resolve_user_chat_bot` + `send_text_to_user`），收件人取该仓库成员（`user_repos` ∩ 已绑定 `feishu_open_id`），上限 20 人；另有 `webhook` 渠道（`OBS_ALERT_WEBHOOK_URL`）。**空渠道=只记录不推送**；静默中的事件不推送 |
| 规则编辑 | 启停·阈值 | 启停/改阈值/通知渠道（0 不推送 / 1 飞书 / 2 飞书+webhook）都可改；全字段 CRUD 待做 |
| 时序分流 | 复用 `BusinessSampler` | 独立 30s 循环（`BusinessSampler` 是 scrape-time TTL 缓存，不是定时线程，无法承担"无人访问也要告警"）；通知在主事件循环里发（求值跑在线程里，IM 客户端是 async） |
| 自助验证 | — | 面板提供「测试飞书通知」：给自己本人发一条，就地显示 `message_id` 或**可操作的原因**（未绑定飞书 / bot 未连接 / 接口失败） |
| 在线验证 | — | 真机实测：临时放宽阈值 → 30s 内落 11 条 firing 事件（每仓一条）→ 还原后 30s 内全部转 resolved；测试通知实测 `ok=true` + 真实 `message_id` |

#### 接飞书途中修掉的阻塞性 bug（IM 租约心跳时区）

`bot_lease.heartbeat()` 的 MySQL 分支用 `CAST(NOW() AS CHAR)` 写 `grape_im_instance.last_heartbeat`
（= **MySQL 服务器本地时间 CST**），而读侧 `list_active_instances()` 用 **UTC** 算 cutoff
（SQLite 分支的 `datetime('now')` 也是 UTC）。两者差 8 小时 → 存进去的值永远大于 cutoff →
**所有历史实例行永久"存活"** → 一致性哈希环被幽灵实例占满 → 真正在跑的实例永远算不到归属 →
`try_acquire` 从不执行 → **bot 一直 IDLE 不连接**（表现：飞书助手不回消息、告警通知 `reason=bot_not_in_memory`）。

修复：心跳时间统一由 Python 侧生成 UTC 并作为参数写入（两后端一致，与读侧对齐）；
同时清掉表里的历史脏行让环立刻恢复。教训：**同一字段的写入与读取必须使用同一时间基准**，
不要混用数据库本地时间函数与 Python UTC。

---

### 9.3 策略层：级别 / 重复抑制 / 聚合 / 通知时段 / 升级（P1-1，2026-10-17 已实现）

> 对应二次调研的"告警治理三层"缺口（§10.5）与归属结论（§5.4）：**策略字段与求值在主服务，
> 定义入口在管理台**。模块：`src/observability/alert_policy.py`（纯函数 + 进程内聚合缓冲）。

**字段（`obs_alert_rules` 新增 5 列；`obs_alert_events` 新增 4 列状态）**

| 字段 | 语义 |
|---|---|
| `severity` | 级别扩为五档 `critical > severe > important > warning > info`（默认 warning；**非法值归一为 warning 并记日志**，不让脏配置炸求值器） |
| `repeat_sec` | `0` = 同一开放事件只通知一次（**旧行为，默认**）；`>0` = 未恢复时每满 N 秒复推一次（"还在告警"） |
| `aggregate_mode` + `aggregate_window_sec` | `none` 立即发（默认）；`rule` 同一规则自**首条**待发事件起算窗口，窗口结束发**一条汇总**；`period` 与**时钟对齐**窗口（固定节拍）后发汇总。窗口为 0 时按 300s |
| `notify_window_json` | 空=全天；`{"days":[1..7],"start":"09:00","end":"19:00"}`（`days` 省略=每天，`start>end` 表示跨零点）。窗口外**只记录**（事件 `notify_deferred=1`），窗口内**补推一次** |
| `escalate_json` | 空=不升级；`{"after_sec":1800,"severity":"critical","channels":"feishu"}` —— 开放事件持续超时且**未升级过** → 升级通知一次（级别抬高、可换渠道），`escalated_at` 落库 |

事件侧状态：`notify_count` / `last_notified_at`（重复抑制依据）/ `escalated_at` / `notify_deferred`。

**语义要点（易踩坑，已用测试钉死）**

1. `repeat_sec>0` 时求值器会**重新提名**开放事件（`repeat_due`）—— 否则策略只"抑制"不"复推"，
   形同虚设（**真机演练发现的缺口**，已补）；提名后仍由 `decide_notify` 把关静默/时段/聚合
2. 聚合缓冲是**进程内**的（求值器所在进程）：进程重启会丢**未发送**的汇总，事件本体不丢（可接受）
3. 汇总收件人 = 涉及仓库成员的**并集**（上限 20）
4. 时区固定 **UTC+8**（不依赖系统 tzdata）；`notify_window_json` 入库时归一成分钟数（`{"start":540}`）
5. 通知顺序：**静默 → 通知时段 → 重复抑制 → 聚合 → 投递**

**写入口（管理台 T-A1/T-A2 用的唯一入口，校验只有一份）**

- `GET /api/admin/obs/alerts/rules` —— 全部规则（**含平台级**，全局不过滤仓库）+ 档位枚举
- `PUT /api/admin/obs/alerts/rules/{id}/policy` —— 写策略字段；**非法配置 400 且不写库**、规则不存在 404、写操作记审计

**用户面（:3000）**：事件表与规则表**只读显示级别**（`严重/较严重/重要/警告/提示`），并标注
`聚合 / 升级策略 / 已升级 / 时段外` 标记，让用户理解为什么会收到合并或升级通知；
**启停/阈值/自助静默仍在用户面，策略定义仍在管理台**（§5.4）。

**测试**：`tests/test_obs_alert_policy.py` 16 例（纯单元：级别归一、时段含跨零点、升级、归一化校验、
disposition、repeat_due、聚合窗口与去重）+ `tests/test_obs_alerts.py` 30 例（含 L2 接口、升级、
时段补推、repeat 复推、聚合 flush）。

---

## 10. 与观测云对照：我们取什么、舍什么

| 观测云做法 | grape 取舍 | 理由 |
|---|---|---|
| 开源采集端 + 闭源平台变现 | **全栈自建，全部可自部署** | grape 是自建产品，用户不该被逼付费 |
| 自研存储引擎（GuanceDB） | ❌ 用 MySQL 聚合表 + 文件 | 量级差 3~4 个数量级，MySQL 足够 |
| 统一查询语言 DQL | ❌ 结构化筛选器 + 保存视图 | 学习成本 vs 收益不划算 |
| 650+ 技术栈采集器 | ❌ 只采 grape 自身（进程/任务/引擎/主机） | 不是通用监控产品 |
| OTel/Datadog 探针拼装 | ⚠️ 仅**借鉴概念**（span/trace_id），不引 SDK | 避免 worker 协议大改 |
| 告警引擎产品化 | ✅ **重点补齐**（规则/触发/静默/通知） | 我们最落后的环节 |
| SLO / 事件中心 | ✅ 用现有 health_scorer + events 演进 | 已有原料 |
| AI Copilot（Obsy） | 🟡 P3 可选：用现有 Chat Agent 接 `obs_*` 数据做"问诊" | 锦上添花 |
| RUM / Profiling / 移动端 / 会话重放 | ❌ 不适用 | grape 无 C 端场景 |

**一句话定位**：观测云是"通用可观测性平台"，grape 要做的是"**Agent 编排平台的内建可观测性**" —— 不追求技术栈广度，追求**与任务/引擎/场景的深度绑定**（这是通用平台做不到的）。

### 10.5 二次深度调研（2026-10-17）：能力矩阵与差距

> 全文见 **`observability-guance-deep-research-v2.md`**（逐模块深挖：告警治理三层、SLO 扣减口径、
> 事件中心"只聚合不产生"、可用性拨测、LLM 监测走 Langfuse 集成等）。

**本次新增的两个行业信号**：

| 标准 | 状态 | 结论 |
|---|---|---|
| OTel **CI/CD** 语义约定（`cicd.pipeline.run.*`、`cicd.pipeline.task.run.*`） | spans **RC**、metrics Mixed | **优先对齐**：我们就是编排平台，issue→流水线→PR 合并天然是 CI/CD 语义，且标准更成熟 |
| OTel **GenAI** 语义约定（`gen_ai.*`） | **Development**（未稳定） | 语义我们已对齐（input/output/cache/reasoning + model），**暂不追字段名**，留映射层 |

**差距矩阵（口径：✅ 已有 ｜ 🟡 部分 ｜ ❌ 没有；⏸ 明确不做见调研文档 §七）**

| 能力 | 现状 | 差距要点 | 规划 |
|---|---|---|---|
| 设备遥测 | ✅ | — | — |
| 日志 | 🟡 | 字段派生管道、留存分层 | P3 |
| 追踪 | 🟡 | span 树（父/子/并行） | P3 |
| LLM 用量 | 🟡 | 内容级检索、输出质量；**成本＝用户暂缓** | P3 |
| 告警检测层 | 🟡 | 突变/区间/**数据缺失(Data Gap)** —— 突变与 Data Gap **已落地**（§6.10）；**区间检测**仍未做 | P1-3 ✅ |
| 告警告警层 | 🟡 | **级别分级/升级/重复抑制/聚合/通知时段** | **P1-1** |
| 告警治理层 | 🟡 | 批量静默策略/维护窗口 | P1-1 |
| **SLO / 错误预算** | ❌ | 最大概念缺口（SLI+目标+扣减合并）→ §6.7 | **P2-1** |
| 事件中心 | ✅ | 统一事件视图（五来源 + 首次发生/持续时长 + `next_step` 下钻）**已落地**（§6.9）；告警面板保留为 monitor 快捷面板 | P1-4 ✅ |
| 可视化自定义 | 🟡 | 保存视图/自定义面板 | P2-5 |
| 可用性拨测 | 🟡 | 面向**用户自己的服务**（HTTP/TCP 定时拨测） | P3 |
| 定期巡检报告 | ❌ | 日/周健康摘要 → 飞书 | P2-4 |
| 变更/发布关联 | ❌ | 变更事件叠加到指标图（我们已有 `pull_request_merged` 等原料） | **P2-3** |
| **CI/CD & DORA 交付效能** | ❌ | **差异化最强**：部署频率/前置时间/变更失败率/MTTR | **P2-2** |
| **后台任务自监控**（采集/聚合/告警循环的健康） | ✅ | `loops.py`：/metrics + L2 端点，连续失败可见 | 审计 #4 ✅ |
| 指标留存完整版 | ✅ | `obs_metric_rollup`（分钟 7 天 / 小时 30 天，总览趋势已改读它） | P1-2 ✅ |
| 基数/采样控制 | ❌ | 维度白名单 + 上限截断 | P2 |
| 审计入事件 | 🟡 | `audit_logs` 已有，未进事件视图 | P3 |

**修订两个一次调研的结论**：① "SLO 用 health_scorer 演进" → 低估工作量，**升级为独立模型**（§6.7）；
② 观测云 LLM 监测是**接 Langfuse**（外部框架），我们"每引擎账本适配器"的自建路线在私有化场景更合适 —— 保持。

> **⚠️ 上述缺口不都进主服务用户面**：每一项的**归属（用户面 :3000 / 管理台 :8081 / 双端）**
> 已在 **§5.4** 逐条定下 —— 结论是"数据面在主服务、**配置面与全局视图在管理台**"。

**结构性优势（别丢）**：① 语义深度（任务↔issue↔stage↔引擎↔token↔提交）；
② 流水线本身是可观测单元（`pipeline_trace_id` 已在做）；③ 跨机零依赖（无新组件、无外部 SaaS）。

---

## 11. 实施路线

> **实施进度（2026-10-17）**
> - ✅ **导航与结构**（P0 第 1 项）：一级分类 + 3 分区 + 分区路由 URL —— 见 §5.1/§5.2
> - ✅ **LLM 用量台账 + 四引擎采集**（P2 那条的 P0 采集部分，提前做）：`obs_llm_usage`
>   + 每引擎账本适配器，已真机验证（见 `observability-llm-usage-design.md`）
> - ✅ **总览分区（部分）**：`/api/obs/overview` + 可见范围过滤（越权 404）+ KPI 5 卡
>   + 任务量与结果 / 模型占比 / Token 消耗三张图，ECharts 本地 vendored
>   —— 见 `observability-overview-page-design.md §9`（含未做清单）
> - ✅ **告警中心（用户面最小可用）**：`obs_alert_rules`/`obs_alert_events` 两表 + 从
>   `deploy/alerts.yml` 平移的 **7 条种子**（3 条用户面 / 4 条平台级）+ 30s 求值器
>   （`for_sec` 抑制抖动、开放事件唯一、恢复转 resolved 不重复通知）+ 总览**告警横幅**与
>   告警面板（触发记录 / 规则 / 静默）+ 静默。**平台级规则与事件不进用户面**（§5.3）。
>   通知投递**已接飞书私聊**（用收件人本人的 chat bot 发；另有 `webhook` 渠道），面板提供
>   「测试飞书通知」自助验证；3 条用户面规则已开启飞书通知。详见 §9.2 差异表及其后的
>   「租约心跳时区 bug」小节（该 bug 修好前 bot 从未连上，通知实际发不出去）。
> - ✅ **设备分区迁移 + 资源曲线**：现有设备内容已在新分区渲染（子 Tab 收敛为单视图）；
>   新增 `obs_runtime_samples` 采样表（60s 一次，7 天留存）+ `/api/obs/runtimes/history`
>   + 资源趋势折线图（CPU/内存/磁盘，可按引擎筛选）。可见性口径与设备列表同源。
> - ✅ **日志增强 `obs_log`**：热数据落库（只落 W/E）+ 主控日志异步落库（有界队列、可 env 关）+
>   worker 复用既有心跳 `log_lines` 通道（**worker 侧零改动**，另有 `POST /api/obs/logs/ingest`
>   供批量上报）+ `GET /api/obs/logs`（级别/来源/仓库/任务/关键字/时间范围，**按可见范围过滤**）+
>   14 天留存清理 + 「任务与日志 › 错误日志」改为库检索（库不可用时降级文件扫描）。
>   入库前脱敏（token/密码/连接串）；**无仓库维度的主控/进程日志不进用户面**（归 L2/L3）。详见 §6.3。
> - ⬜ 指标聚合与留存（`obs_metric_rollup` 完整版；当前只有设备资源采样）、
>   告警的 trace/任务下钻、规则全字段 CRUD
> - ✅ **追踪（`trace_id` 贯通 + 任务时间线，§6.4）**：`agent_tasks` 增 `trace_id`/`pipeline_trace_id`
>   （确定性生成 + 启动回填 + 惰性补齐，315 条历史任务全盖上）+ `GET /api/obs/trace/{trace|task_id}`
>   （含 enqueue→dispatch→terminal、排队/耗时、`引擎@设备`、token、失败原因、步骤、事件、日志命中）
>   + `GET /api/obs/trace?repo=&issue_id=`（按 issue 看流程）+ 前端时间线弹窗 + **与日志双向互跳**。
> - ✅ **告警策略层（P1-1 的**主服务侧**，即 D1）**：`obs_alert_rules` 增 `severity`（五档）/
>   `repeat_sec` / `aggregate_mode` / `aggregate_window_sec` / `notify_window_json` / `escalate_json`，
>   `obs_alert_events` 增 `notify_count` / `last_notified_at` / `escalated_at` / `notify_deferred`；
>   新模块 `src/observability/alert_policy.py`（级别归一 / 通知时段含跨零点 / 重复抑制 /
>   聚合缓冲（rule 与 period 两种窗口）/ 升级）+ 求值器与投递接线（`flush_aggregates` 挂 30s 循环）+
>   **L2 写入口** `GET|PUT /api/admin/obs/alerts/rules[/{id}/policy]`（`require_admin` + 审计）。
>   用户面事件/规则表只读显示级别与 `聚合/升级/时段外` 标记。详见 §9.3。
>   ⬜ **管理台侧 UI（T-A1/T-A2）待做** —— 契约见 `grape-admin/docs/observability-admin-tasks.md`；
>   ⬜ 余下：批量静默策略/维护窗口（`obs_alert_silence` 表 + 管理台页）、规则全字段 CRUD。
> - ✅ **数据缺失（Data Gap）+ 突变检测（P1-3）**：新模块 `src/observability/gap.py`；
>   规则增 `kind`（metric/gap）、事件增 `source`（monitor/gap/mutation）；种子 +3 条
>   （`runtime_silent` 开 / `repo_silent` **默认关** / `task_success_rate_drop`）；
>   两类抑制（从未上报不算缺失、超 7 天视为已退役只进 L2 名单）；突变指标需两窗口各 ≥3 样本；
>   用户面按"看得见该设备"过滤设备维度事件；**L2 新增** `GET /api/admin/obs/gaps` 全局静默名单。
>   详见 §6.10。⬜ 余下：管理台 T-A5 页面（接口已就绪）、`obs_metric_rollup` 版突变（现在直查任务表）
> - ✅ **统一事件视图 + 下钻（P1-4 / §6.9）**：新模块 `src/observability/events_view.py`，
>   把 monitor/gap/mutation（`obs_alert_events`）、task（卡住+失败，复用 ops_overview 口径）、
>   change（PR 合并）归一成同一组字段（`started_at`/`duration_sec`/`ongoing`/`state`/`next_step`/`scope`）；
>   用户面 `GET /api/obs/events`（可见仓库 ∪ 可见设备，平台级与 audit 一律不出），
>   管理台 `GET /api/admin/obs/events`（全局 + 含 audit）；
>   前端「任务与日志 › 事件」子 Tab：来源/状态筛选 + 四类下钻（时间线 / 日志 / 需求 / 设备）。
>   **实测修掉一个越权面**：ops_overview 是组织级过滤，用户面必须再按可见仓库收敛（详见 §6.9）。
>   ⬜ 余下：管理台 T-A4 页面（接口已就绪）、前端"事件→时间线"的 issue 维度直跳（现按 task 跳）
> - ✅ **`obs_metric_rollup` 完整版（P1-2 / §6.2）**：新模块 `src/observability/metric_rollup.py` +
>   表（`resolution` 分钟/小时同表、`repo_key` 冗余）+ 60s **追赶式补齐**循环（从新到旧、限量、
>   `to_thread` 不阻塞）+ 留存清理（分钟 7 天 / 小时 30 天）+ 14 个业务指标（任务量/结果/成功率、
>   排队等待与耗时 avg+p95、四种 token、`runtimes_online`）。
>   总览趋势**改读 rollup**（`charts.source=rollup`，无数据回退 live，前端零改动）；
>   新增 L1 `GET /api/obs/metrics/series`（只出可见仓库、**不含 global**）与
>   L2 `GET /api/admin/obs/metrics/series`（global/单仓/全仓）。
>   顺带修 `mysql_compat`：**复合 UNIQUE 的 `ON CONFLICT(...)` 漏翻**（真实 1064 事故）。
>   P1 四项全部落地 ✅（余下均为管理台侧页面，接口已就绪）
>
> 即：**P0 全部落地**；**P1 四项也全部落地**（告警策略层 / 指标聚合 rollup / 数据缺失与突变 / 统一事件视图）。

**⚠️ 路线已于 2026-10-17 二次调研后重新规划**（见 §10.5 与
`observability-guance-deep-research-v2.md`）：原 P1/P2 草案作废，改为下面的
**P1 治理层 → P2 差异化 → P3 按需**。**定价/成本：用户明确暂缓，仅保留 `cost_usd` 字段与设计位**。

### P1（治理层与数据质量，1~1.5 周）

> 落点列：**主**=主服务用户面（:3000）｜**台**=grape-admin 管理台（:8081）｜**双**=两端（管理台全局视图）

| # | 交付 | 落点 | 关键点 | 预估 |
|---|---|---|---|---|
| P1-1 | **告警治理增强** | **台为主，主只读**（**主服务侧数据面已落地**，见表尾进度；管理台 UI 待做） | 事件级别分级（Critical→Info）、重复抑制间隔、聚合（按规则/周期）、自定义通知时段、告警升级、批量静默策略；用户面只读显示级别 + 保留启停/阈值/自助静默 | 3d（主服务侧 1.5d 已完成；余下批量静默策略 + 管理台 UI） |
> **2026-10-17 审计与重设计**：`observability-audit-2026-10-17.md`（1 个静默丢数据缺陷 + 1 类 SQL 方言地雷 +
> 3 个显示 bug 已修；4 处欠考虑待拍板；性能/影响面实测）、`observability-events-view-redesign.md`（事件视图重设计方案）。
> **降载/留存/自监控三项已于同日落地**（见审计 §六点五）：告警求值 59 条 SQL→19 条、13.6ms（口径逐项钉死等价）；
> 事件留存 90 天、台账年度归档；循环健康暴露到 `/metrics` 与 L2。
> **token 采集已补齐（2026-10-17~18）**：归属口径取 **B**（`run_id`/`run_kind`；`task_id` 只留给 worker 任务），
> 四类入口全覆盖 —— chat×3（agent_fn / run_agent / agent_worker）+ IM 直连 + 事件流水线/autopilot×3 + VOD Bot；
> 另补 `engine_id`（哪台引擎跑的，task 行取 `agent_tasks.runtime_id`、主控侧取 runtime id）。
> 语法：`src/observability/master_usage.py`（复用 worker 侧同一个 `UsageCollector`）。

### P1（治理层与数据质量）—— **四项全部落地（2026-10-17）**

| # | 交付 | 状态 |
|---|---|---|
| P1-1 | 告警治理增强（策略层） | ✅ 主服务侧已落地（级别/重复抑制/聚合/通知时段/升级）；⬜ 管理台 UI + 批量静默 |
| P1-2 | `obs_metric_rollup` 完整版 | ✅ 已落地（分钟/小时同表 + 追赶式补齐 + 留存清理 + 14 指标） |
| P1-3 | 数据缺失 + 突变检测 | ✅ 已落地（L1/L2 接口 + 事件视图接入）；⬜ 管理台 T-A5 页面 |
| P1-4 | 统一事件视图 + 下钻 | ✅ 数据面已落地（来源聚合/首次发生/持续时长/四类下钻）；⬜ 管理台 T-A4 页面 |

| P1-2 | **`obs_metric_rollup` 完整版** | **主（数据面）** | 分钟聚合表 + 采样器写入 + 清理；总览/趋势改读 rollup；**SLO 与交付效能的底座**；留策略配置入口在管理台 | 2d（**已完成**，见 §6.2） |
| P1-3 | **数据缺失（Data Gap）+ 突变检测** | **双**（**数据面已落地**，见 §6.10；管理台 T-A5 页面待做） | "该报数却没报"单列为事件；成功率环比突变（统计法，不上 ML）；管理台看全局静默名单（`GET /api/admin/obs/gaps` 已就绪）+ 配置阈值 | 1d（已完成） |
| P1-4 | **统一事件视图 + 告警下钻** | **双**（**数据面已落地**，见 §6.9；`audit` 来源只在管理台） | §6.9 来源聚合（含**首次发生/持续时长**）+ 事件↔时间线↔日志互跳 + 处置建议 | 1.5d（已完成） |

### P2（差异化，2 周）

| # | 交付 | 落点 | 关键点 | 预估 |
|---|---|---|---|---|
| P2-1 | **SLO / 错误预算**（§6.7） | **双**（目标设定在管理台） | 三类 SLI（任务成功率/排队时延 P95/引擎可用率）+ 分钟桶比例法扣减 + 燃尽曲线；用户面只读达成态 | 4d |
| P2-2 | **交付效能 / DORA**（§6.8） | **双**（**排行只在管理台**） | 四指标；阶段迁移从 `events.a2a_event` 重建；命名向 OTel `cicd.*` 对齐 | 3d |
| P2-3 | **变更事件叠加** | **双** | PR 合并/提交做成变更事件，叠在成功率与时延图上 | 2d |
| P2-4 | **定期巡检报告** | **双（两份）** | 仓库/个人日报 → 仓库成员；平台日报 → 平台运营 | 2d |
| P2-5 | **保存视图 / 自定义面板** + 基数保护 | **双（各自实现）** | 用户面个人视图；管理台运营视图；基数白名单配置在管理台 | 2d |

### P3（按需，不做也行）

可用性拨测（**双**：用户给自己服务配，管理台全局 + 平台自身端点）；日志解析管道（**台**配置）；
LLM 内容级观测（**双**，跨租户检索只在管理台且记审计）；审计事件进事件视图（**台**）；
值班/升级轮值（**台**）；**成本定价（用户暂缓，双）**。

**量级参考**：P1 ≈ 7.5 人日；P2 ≈ 13 人日。**其中落管理台的量（P1-1 为主 + P2-1 设定面 + P2-5 配置面）
约 6~7 人日，在 grape-admin 仓实施**（详见 §5.4 实现落点）。

---

## 12. 风险与开放问题

| # | 风险/问题 | 应对 |
|---|---|---|
| R1 | 指标加 `repo` 标签导致**基数爆炸** | 白名单 + 上限截断（超限归 `other`），P2 专项保护 |
| R2 | `obs_log` 写入影响主链路性能 | 只落 W/W + 批量异步写 + 表维护清理；可 env 关闭 |
| R3 | worker 日志上报增加网络开销 | 只报 W/W + 批量 + 失败不阻塞任务 |
| R4 | 远端 worker 老版本不支持上报 | 上报接口幂等且向后兼容；老 worker 静默降级 |
| R5 | 与 admin 健康中心（8081）职责重叠 | **明确分工**：主服务可观测性 = 按仓库/场景的业务视角（L1）；admin 健康中心 = 跨租户平台运维 + 基础设施视角（L2） |
| R6 | 前端单文件过大（observability.js 已 1602 行） | P1 按分区拆分为 `static/js/pages/obs/*.js` |
| R7 | **越权数据泄露**（观测接口天然跨租户读取） | 后端强制可见范围过滤 + 越权 404 + redaction；单测覆盖越权用例（§5.3） |

**已决**：
- **DB/平台基础设施信息不进用户面** → 归 admin 健康中心（§5.3）。原「概览」设计里不再出现连接池/库表/主机拓扑。

**待你确认的开放问题**：
1. **Q1**：指标留存选 **MySQL 聚合表**（推荐，跨机可见/零新组件）还是引入可选 Prometheus 外挂（功能强但要部署）？— 已按推荐写入
2. **Q2**：日志落库范围只落 **WARNING/ERROR**（推荐，量小）还是全量 INFO？— 已按推荐写入
3. **Q3**：告警通知渠道先用 **IM 渠道（已有飞书）** 还是同时接邮件（`email.json` 已具备）？— 已按"先 IM"写入
4. **Q4**：侧栏项数 → **已决：进侧栏，压到 3 项**（总览 / 设备与引擎 / 任务与日志；告警并入总览的横幅+面板）。原 5 项方案中的「任务与链路」「日志」合并为「任务与日志」；若日后想拆回，加一行 HTML 即可。
5. **Q5**：P0 是否拆成两个 PR 交付（第一个：导航升级 + 边界 + 指标 + 总览；第二个：日志 + 告警）？— 已按本地多次提交（未推送）落地

**二次调研（2026-10-17）新增待定**：

6. **Q6（SLO 目标值）** → **已由 §5.4 归属分析拍定**：v1 由**管理台统一设定**（平台口径一致），
   用户面只读展示达成态与预算剩余；将来若开放团队自设仓库目标，再加"用户面可覆写"。
7. **Q7（报告收件人）** → **已由 §5.4 拍定**：**分两份** —— 仓库/个人日报给仓库成员（用户面发起），
   平台日报给平台运营/管理员（管理台发起）；不发同一份给两个受众。
8. **Q8（DORA 边界）** → **已由 §5.4 拍定**：用户面**只给自己仓库**、不做排行；
   **跨仓库/组织排行只放管理台**。
9. **Q9（P1 顺序）** → **已定**：先做 **P1-3 数据缺失检测**（用户 2026-10-17 指定"接着开 P1-3"）—— **已完成**；
   其后 **P1-1 主服务侧也已落地**；**P1-2 / P1-4 亦已落地（2026-10-17）→ P1 四项全部完成**。
   剩余：管理台侧页面（T-A1/A2/A4/A5，接口均已就绪，契约见 grape-admin 文档）、
   P2 四项（SLO / DORA / 变更叠加 / 巡检报告）、P3 按需。

---

## 13. 验收标准

1. 侧栏出现**一级分类「可观测性」**（与「设备中心」平级），下辖 **3 个子项**（总览 / 设备与引擎 / 任务与日志），点击直达对应分区；全文案统一，无「设备监控」残留
2. 总览页在**真实数据**下展示：任务队列/成功率/P95、引擎在线、我的设备资源、当前告警数
3. 能从「仓库 → 任务 → 时间线 → 日志」一路点通，全程不出现裸 ID
4. 重启主服务后**趋势数据不丢**（验证聚合表留存生效）
5. 多机部署下：在 worker 上制造的 ERROR 能在主控日志 Tab 检索到（验证上报链路）
6. 告警：手工把阈值调到必然触发 → 产品内出现 firing 记录且有通知；恢复后自动 resolved
7. 关闭新增开关（env）可完整降级到现状，不影响主流程
8. 相关单测/集成测试通过（指标聚合、日志落库与检索、告警求值、trace 聚合）
9. **边界验证（安全）**：用户 A 无法通过任何 `/api/obs/*` 看到用户 B 的仓库/设备/任务/日志（越权返回 404）；页面与接口响应中**不出现**库名/表名/内网 IP/连接池等平台信息
10. 侧栏与页面在 1280px 宽度下不出现折行/溢出；空态、加载态、失败态齐全

---

## 附：与现有代码的映射（实施索引）

| 新增/改造 | 落点 |
|---|---|
| **侧栏调整（一级分类 + 3 子项）** | `src/api/static/index.html`（新增「可观测性」分组标题 + 3 个子项；原 `nav-observability` 从设备中心分组移出并改名） |
| **导航/路由注册** | `src/api/static/js/app.js`（页面注册与 `observability/<section>` 路由）、`js/i18n.js` |
| 指标维度与聚合写入 | `src/observability/__init__.py`（`BusinessSampler` 扩展） |
| 聚合表/日志表/告警表 DDL | `src/api/db_setup.py` |
| 新 API 路由 | `src/api/routes/observability.py`（新建，挂 `main.py`） |
| **可见范围过滤（统一入口）** | 复用 `src/api/deps.py` 的 `require_org_member` + 仓库可见性口径，抽 `resolve_visible_scope(user)` 供所有 `/api/obs/*` 调用 |
| 日志格式补字段 | `src/config.py` 日志格式器 |
| worker 日志上报 | `src/runtime/agent.py` + `src/runtime/client.py` |
| trace_id 生成/传播 | `src/api/event_pipeline.py`、`src/api/pipeline_executor.py`、`agent_tasks` 增列 |
| 告警求值 | 复用 `BusinessSampler` 线程 + 新模块 `src/observability/alerting.py` |
| 清理 | `src/runtime/table_maintenance.py`（挂 `obs_*` 表） |
| 前端 | `src/api/static/js/pages/observability.js`（+ P1 拆分 `obs/*.js`） |
| i18n | `locales/{zh,en}/observability.json` + `app.json`（导航名与分类名） |
| **DB/平台指标去向** | admin 健康中心（独立项目 grape-admin），主服务不新增平台指标接口 |