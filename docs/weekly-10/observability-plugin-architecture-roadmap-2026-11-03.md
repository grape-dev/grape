# 可观测性插件化重构 · 设计路线（2026-11-03，重建于 docs/weekly-10/）

> 状态：**方案稿（待评审后实施）**
> 背景：运行驾驶舱重构，可观测性作为**数据域核心能力**插件化——原生看板为第一消费者，同一套 API 支撑对接观测云 / Grafana / Datadog 等第三方平台。
> 前置：observability-redesign-design.md（§7 API 基线）、observability-user-insights-redesign-2026-10-20.md（本目录）
> 本文档回答"怎么做"：API 分层设计 → 插件架构 → 对接三方平台的适配层 → 实施路线。
> 注：原文档写于 2026-11-03 因仓库 checkout 丢失，本版按原内容重建。

---

## 1. 核心设计判断

**为什么插件化是正确方向**：

1. 现状是"API 与页面强耦合"——`/api/obs/*` 22 个端点按驾驶舱的卡片形状设计（overview 返回的就是某张图的拼装数据），换看板 = 改 API，接三方 = 没门
2. 数据域的价值在**查询语义**（"给我这个仓 7 天的失败率序列"），不在展示形状——第三方平台和原生看板要的是同一份语义
3. 业界收敛的交换格式已存在（OTLP / PromQL / OpenTelemetry 语义约定），不必自造协议，**对内原生 API、对外标准协议**，中间加一层适配

**一句话架构**：

```
数据源(usage/events/tasks/logs) → 查询语义层(核心) → 适配层 → { 原生看板 | OTLP→观测云/Grafana/Datadog | 告警引擎 }
```

---

## 2. API 设计：三层结构

### 2.1 第 1 层：领域查询 API（Query Semantics，核心资产）

**原则**：按"查询意图"而非"页面卡片"组织；返回**结构化序列/聚合**而非拼装视图；时间与维度参数全 API 统一。

统一约定（所有 `/api/obs/q/*` 生效）：

| 约定项 | 设计 |
|---|---|
| 时间参数 | `range=1h\|6h\|24h\|7d\|30d` + `from/to`（ISO8601，二选一）；响应必带 `bucket_size`（自动降采样：1h→1min, 7d→1h, 30d→1d） |
| 维度过滤 | 统一 `dim` 参数集：`repo / user / agent / engine / scene / status`，格式 `repo=grape-dev/grape`，多值逗号分隔 |
| 响应骨架 | 统一 `{ series: [{name, labels{}, points: [{t, v}]}], bucket_size, range, total? }`——任何指标查询返回同构数据，看板和三方适配都只解析一种骨架 |
| 鉴权 | 沿用现有：先解析调用者可见范围再拼查询；越权 404；响应过 redaction（§7 基线不变） |
| 分页 | 序列类不分页（bucket 降采样天然限量）；日志/事件类用游标 `next_cursor` |

**领域查询族**（把现有 22 端点收编归并为 5 族，旧端点保留 6 个月兼容期）：

```
GET /api/obs/q/metrics        指标族: metric ∈ {task_total, task_success_rate, task_p95,
                              queue_depth, engine_online, llm_tokens, llm_cost,
                              engine_health, stuck_count, error_count, user_activity}
                              + dim 过滤 → 统一序列骨架
GET /api/obs/q/entities       实体族: type ∈ {repo, user, engine, device, task}
                              列表+当前状态(复用 overview/devices/tasks 数据,但只做实体查询)
GET /api/obs/q/trace/{id}     链路族: 任务执行时间线(现 /trace 归并)
GET /api/obs/q/logs           日志族: 检索(现 /logs 归并, 游标分页)
GET /api/obs/q/events         事件族: 统一事件视图(现 /events 归并)
```

告警与 LLM 归因不进查询族（前者是控制面、后者形态成熟）：`/api/obs/alerts*`、`/api/obs/tokens/*` 原样保留。

### 2.2 第 2 层：原生看板 API（Board Composition）

看板 = 查询族之上的**编排**，不带新数据语义：

```
GET  /api/obs/boards                    看板列表(预置 + 自建)
GET  /api/obs/boards/{id}               看板定义: widgets[] 每个含 {query: <第1层完整参数>, viz: {type, title, unit, color_by}}
POST /api/obs/boards                    自建看板(存 DB, 用户级)
PUT  /api/obs/boards/{id}               编辑
POST /api/obs/boards/{id}/render        渲染: 后端按 widget 逐个执行第1层查询并组装返回
                                        (或前端拿 board 定义自行调第1层——两个模式都支持)
```

**关键点**：widget 的 `query` 字段就是第 1 层 API 的参数集——看板配置即查询集，**没有为看板单独发明的数据接口**。预置看板（运行驾驶舱三页）作为 seed 数据落库，原生页面渲染的就是 board 定义。

### 2.3 第 3 层：导出/对接 API（Interop）

**方向 A——拉模式（第三方主动来拉，首选）**：实现 **Prometheus 兼容 exposition + OTLP HTTP 接收端**两个标准口：

1. `GET /metrics/obs`（独立端口已有 METRICS_ADDR，扩展内容）：领域指标以 **Prometheus 文本格式**暴露——Grafana/Prometheus/VictoriaMetrics 零改造接入，Datadog 有 Prometheus 采集器
2. 领域指标按 OTel 语义约定命名映射：`grape_llm_tokens_total` → 附 `gen_ai.usage.*` 标签约定；后续观测云（OTLP 原生）、Datadog（OTLP 接收）直接对接

**方向 B——推模式（grape 主动推给三方，插件位）**：

```
POST /api/admin/obs/sinks          数据出口(汇)注册: {type: otlp|prometheus_remote_write|datadog|guance, endpoint, auth_ref, filters}
GET  /api/admin/obs/sinks          出口列表 + 健康状态(最近推送成功/失败)
POST /api/admin/obs/sinks/{id}/test  连通性测试
```

Sink 内部实现为**插件接口**（见 §3），filter 支持"只推某些 repo/某些指标"，成本可控。

---

## 3. 插件架构（代码组织）

```
src/observability/
├── query/                  # 第1层: 领域查询引擎(无 FastAPI 依赖, 纯函数: 参数→序列)
│   ├── metrics.py          # 每个指标一个 resolver, 注册表模式
│   ├── entities.py / trace.py / logs.py / events.py
│   └── registry.py         # METRIC_REGISTRY = {name: resolver}  ← 插件注册点①
├── api/                    # 第1层的 HTTP 薄壳(FastAPI 路由只做参数解析+鉴权+调 resolver)
├── boards/                 # 第2层: 看板定义模型 + 预置 seed
├── export/                 # 第3层: 出口适配器
│   ├── base.py             # Sink 抽象: transform(batch) + push() + health()
│   ├── prometheus.py       # 拉模式 exposition
│   ├── otlp.py             # OTLP HTTP 转换(protobuf/json)
│   ├── sinks/              # 推模式插件目录 ← 插件注册点②
│   │   ├── datadog_sink.py / guance_sink.py / remote_write_sink.py
│   └── registry.py         # SINK_REGISTRY = {type: SinkClass}
└── alerts/                 # 告警引擎(现有, 不动)
```

**插件机制约定**（不引入插件框架，用注册表 + entrypoint 约定，最小成本）：

- 新指标 = 一个 resolver 函数注册进 `METRIC_REGISTRY`，自动出现在 `/q/metrics` 的 metric 枚举、看板 widget 可选项、prometheus exposition 里
- 新出口 = `SINK_REGISTRY` 注册一个实现 `base.Sink` 的类，自动出现在 admin sinks API 的 type 枚举里
- **每个 resolver 必须自带单测**（输入 dim 组合 → 断言序列骨架），这是数据域质量底线

## 4. 与三方平台对接的具体形态

| 平台 | 接入方式 | 我方工作量 | 说明 |
|---|---|---|---|
| **Grafana** | 拉模式 Prometheus exposition | 最小 | Grafana 配 Prometheus 数据源指向 `/metrics/obs`，dashboard 按标签建 |
| **观测云** | 推模式 OTLP sink（或 DataKit 采集我方 exposition） | 小 | 观测云 OTLP 原生；其 AI Agent 视图吃 OTel GenAI 语义约定 |
| **Datadog** | 推模式：OTLP endpoint 或 agent 采集 exposition | 小 | Datadog OTLP intake 直接可用 |
| **任意 OTLP 后端**（Jaeger/Tempo/Langfuse 等） | 推模式 OTLP sink 通用 | 一次实现全家受益 | 这是对"数据域核心能力"的兑现——OTLP 是业界最大公约数 |

**不建议做的**：为每家平台写私有协议适配（Datadog API、观测云私有格式）——OTLP + Prometheus 两个标准口已覆盖全部主流平台，私有协议是维护负担。

## 5. 安全与可见性（沿用 §7 基线，新增两条）

1. 拉模式 exposition：绑定 METRICS_ADDR 内网端口 + IP 白名单（现有模式），**不暴露用户级维度**（只暴露平台聚合指标，用户/仓库维度走带鉴权的 OTLP sink 按需推）
2. Sink 凭据：`auth_ref` 引用 user_credentials 体系，不明文存 endpoint 配置里；推送日志脱敏复用现有 redaction

## 6. 实施路线（四阶段，每阶段独立可验收）

| 阶段 | 内容 | 验收（强断言） |
|---|---|---|
| **A. 查询语义层**（1 周） | `src/observability/query/` 骨架 + metrics resolver 注册表 + 收编现有 overview/tokens/runtimes 数据源为前 8 个 resolver + `/api/obs/q/metrics` 上线 | 同一查询经旧端点与新端点返回的数值一致（双跑对账脚本）；序列骨架字段完备 |
| **B. 原生看板重构**（1 周） | boards 模型 + 预置看板 seed（把驾驶舱三页翻成 board 定义）+ 前端渲染改为消费 board 定义 | 驾驶舱三页视觉不变（对用户无感），但全部数据来自 `/q/*`；board JSON 可导出/导入 |
| **C. 拉模式出口**（3 天） | prometheus exposition + 指标 OTel 命名映射 + Grafana 对接验证文档 | 真实 Grafana（或 curl 文本格式断言）拉到数据且标签维度正确 |
| **D. 推模式插件**（1 周） | Sink 抽象 + otlp 通用 sink + 观测云/Datadog 各一个 sink + admin sinks API | sink.test 连通 + 测试推送在三方平台控制台可见数据（或 OTLP collector 收到 span） |

**依赖与风险**：

- A 阶段的 resolver 双跑对账是整个重构的安全网——旧 API 不删（6 个月兼容），新旧数值对不上就是 resolver 写错
- B 阶段"视觉不变"约束防止重构期间用户面回归
- 旧 22 端点在 D 完成后进废弃流程（openapi 标 deprecated + 前端全量切到 /q/*）
- 用户维度洞察（user_activity 指标）随本阶段并入 metric 注册表，参与洞察设计的 P1 排期合并

## 7. 与既有设计的关系

- 重构文档 §7 的 API 表是本设计的起点——其中 P0/P1 端点多数直接归并为查询族，语义不变、路径收敛
- 告警中心、SLO、数据缺失检测（已实现部分）原样保留，归入查询族消费的下游
- 三级归因 tokens API 形态成熟，独立保留不动
- 本次不引入新存储（MySQL + 缓存够用），ClickHouse 化仍是数据量触发的远期项
