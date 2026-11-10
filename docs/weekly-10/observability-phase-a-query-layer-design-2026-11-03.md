# 阶段 A 详细设计：可观测性查询语义层（Query Semantics Layer）

> 状态：**设计稿（待评审后实施）**
> 母文档：`observability-plugin-architecture-roadmap-2026-11-03.md` §2.1 / §6 阶段 A（本目录）
> 范围：仅阶段 A——`src/observability/query/` 查询引擎 + `/api/obs/q/metrics` 上线 + 双跑对账；不含看板重构（B）、出口（C/D）
> 工期：1 周
> 注：原文档写于 2026-11-03 因仓库 checkout 丢失，本版按原内容重建。

---

## 1. 目标与验收（强断言）

阶段 A 完成时必须满足：

1. `GET /api/obs/q/metrics?metric=<name>&range=&repo=&user=&agent=&engine=` 可用，返回统一序列骨架（§2.3）
2. 首批 8 个 resolver（§3）全部注册，`GET /api/obs/q/metrics/catalog` 返回 metric 枚举 + 维度声明
3. **双跑对账**：对账脚本对每个 (metric × range × repo) 组合调用旧端点与新端点，数值一致（容差：整数相等、浮点 ≤1e-6）
4. 旧端点零改动、零回归（不删不改，B 阶段前端才切换）
5. resolver 单测全绿：每个 resolver 覆盖"空数据 / 单仓过滤 / 越权 404 路径"

## 2. API 设计

### 2.1 路由

```
GET /api/obs/q/metrics            领域指标序列查询（本阶段唯一新增查询端点）
GET /api/obs/q/metrics/catalog    指标目录（metric 枚举 + 各自支持的 dim + 单位 + 聚合方式）
```

### 2.2 请求参数

| 参数 | 类型 | 说明 | 默认 |
|---|---|---|---|
| `metric` | str（必填） | catalog 中的指标名，未注册 → 400 + `unknown_metric` | — |
| `range` | enum | `1h\|6h\|24h\|7d`（30d 暂缓，见 §5.4） | `24h` |
| `from` / `to` | ISO8601 | 显式时间窗（与 range 二选一）；本阶段留参数位返回 501 | — |
| `repo` | str | 仓库过滤（owner/name）；越权或不属于可见范围 → 404（沿用 RepoNotVisible 语义） | 空=全部可见仓 |
| `user` | str | 用户过滤（user_id 或用户名） | 空 |
| `agent` | str | agent 类型过滤（vod/issue/coding/qa/sre/chat） | 空 |
| `engine` | str | 引擎过滤（runtime_id 或引擎名前缀） | 空 |

**参数校验规则**（路由层完成，进 resolver 前保证干净）：

- `range` 不在枚举 → 400 `invalid_range`
- 多值（逗号分隔）本阶段不实现：收到逗号 → 400 `multi_value_not_supported`（参数位保留，C 阶段再开）
- 所有 dim 值做白名单/存在性校验，非法值 → 400 带原因

### 2.3 响应骨架（本设计的核心契约）

```json
{
  "metric": "task_success_rate",
  "range": "7d",
  "bucket_size": 3600,
  "from": "2026-11-02T00:00:00Z",
  "to": "2026-11-09T00:00:00Z",
  "series": [
    {
      "name": "grape-dev/grape",
      "labels": {"repo": "grape-dev/grape"},
      "points": [
        {"t": "2026-11-02T00:00:00Z", "v": 0.94},
        {"t": "2026-11-02T01:00:00Z", "v": null}
      ]
    }
  ],
  "total": {"v": 0.912, "agg": "avg"}
}
```

**契约细则**：

- 空桶 `v: null`；指标语义为零值时（如 queue_depth 空窗）由 resolver 声明 `zero_fill: true`，值补 0——区分"无数据"与"值为 0"
- points 时间戳为 bucket 起点（UTC，ISO8601 Z 后缀），bucket 数量 = span/bucket
- 序列分组：每个 resolver 声明默认 `group_by`（task 族默认按 repo），无分组需求时单序列
- 错误响应统一 `{"code": <http>, "error_code": "<snake>", "message": "<human>"}`——与现有 obs 路由一致

### 2.4 bucket 规格表

沿用 overview.py 的 `RANGE_SPECS` 作为唯一权威（query 层 import，不复制）：

| range | span | bucket |
|---|---|---|
| 1h | 3600 | 60 |
| 6h | 6×3600 | 300 |
| 24h | 86400 | 1800 |
| 7d | 7×86400 | 10800 |

---

## 3. 首批 8 个 Resolver

数据源与逻辑**收编自现有实现**，不新写计算逻辑——这是双跑对账可行的前提：

| # | metric 名 | 语义 | 来源（现有实现） | 默认 group_by | 聚合 | zero_fill |
|---|---|---|---|---|---|---|
| 1 | `task_count` | 任务计数，按终态拆分（series.name=completed/failed/cancelled） | overview.py `_fetch_tasks` + 分桶循环 | status | sum | true |
| 2 | `task_success_rate` | 任务成功率（completed/(completed+failed+cancelled)） | overview.py 同上 | repo | avg | false |
| 3 | `task_p95_duration` | 任务耗时 P95（秒） | overview.py `_p95` + durations | repo | p95 | false |
| 4 | `queue_depth` | 当前队列深度（排队中+已派发） | overview.py queue_depth 逻辑 | — | last | true |
| 5 | `engine_online` | 引擎在线数/总数 | overview.py `_fetch_engines`（单点） | — | last | true |
| 6 | `llm_tokens` | Token 消耗（input/output 两序列） | tokens.py / metric_rollup.py `fetch_usage_rows` | repo | sum | false |
| 7 | `llm_cost` | 成本（USD） | 同上（cost_usd 列） | repo | sum | false |
| 8 | `error_count` | ERROR 日志计数 | logs.py 检索逻辑改写为分桶 | repo | sum | false |

resolver 实现契约：

```python
# src/observability/query/registry.py
MetricSpec = {
    "name": str,              # 唯一
    "description": str,
    "unit": str,              # "count" / "ratio" / "seconds" / "usd" / "tokens"
    "agg": str,               # "sum" | "avg" | "p95" | "last"
    "group_by": [str],        # 支持的分组维度
    "dims": [str],            # 支持的过滤维度
    "zero_fill": bool,
    "kind": "timeseries" | "snapshot",   # 快照型(queue_depth/engine_online)标 snapshot
    "resolver": Callable[[QueryContext], QueryResult],
}

# QueryContext: {user(鉴权后), range_spec, filters{repo,user,agent,engine}, repo_keys(已解析可见范围)}
# QueryResult: {series: [...], total: {...}} —— resolver 不接触 HTTP，纯函数
```

## 4. 代码结构（新增文件全清单）

```
src/observability/query/           ← 新增包
├── __init__.py                    # 对外导出: q_metrics_catalog(), run_metric_query()
├── context.py                     # QueryContext / QueryResult / SeriesPoint 数据类 + RANGE 解析
├── registry.py                    # METRIC_REGISTRY + MetricSpec + register 装饰器
├── resolvers/
│   ├── __init__.py                # import 触发注册
│   ├── tasks.py                   # resolver 1-4（收编 overview.py 逻辑）
│   ├── engines.py                 # resolver 5
│   ├── usage.py                   # resolver 6-7（收编 tokens/rollup 逻辑）
│   └── errors.py                  # resolver 8
└── compat.py                      # 旧实现公共函数下沉点: visible_repo_keys/_scope_keys/
                                   # _fetch_tasks 等从 overview.py 移到这里, 旧新共用（关键决策 §5.1）
src/api/routes/observability_q.py  ← 新路由文件(prefix=/api/obs/q), 注册进 main
tests/observability/test_query_registry.py      # catalog + 未知 metric 400
tests/observability/test_query_resolvers.py     # 8 resolver × (空数据/单仓/越权)
tests/observability/test_query_compat.py        # 双跑对账(§6)
```

测试归入现有 observability 功能域，不新建测试域。

## 5. 关键设计决策

### 5.1 逻辑复用方式：下沉共享，不是复制

overview.py 的 `_fetch_tasks/_fetch_usage/_scope_keys/visible_repo_keys` 等查询原语移到 `compat.py`，旧端点与新 resolver 共同 import——**避免双份逻辑漂移**。移动是纯搬家（函数体零改动），旧端点行为不变；可单独 commit，先于其它一切。

### 5.2 resolver 与 HTTP 彻底解耦

resolver 只见 QueryContext（已鉴权、已解析可见范围），不 import FastAPI/Request——这是"可被 prometheus exporter / OTLP sink 复用"的前提（C/D 阶段直接调 resolver，不走 HTTP）。

### 5.3 时序 vs 快照

`queue_depth` / `engine_online` 是**快照型指标**（当前值），不是时序——单点序列返回（1 个 bucket、v=当前值），骨架统一但不造假时序。catalog 中 `kind: "snapshot"` 标注差异，看板据此渲染大数字卡片而非折线。

### 5.4 30d 与 from/to

本阶段不实现（501 占位）：30d 需要 obs_llm_usage 归档表的聚合路径验证，from/to 需要游标/对齐语义设计——不阻塞 B 阶段，避免 A 阶段摊大。

## 6. 双跑对账（质量安全网）

对账脚本 `tests/observability/test_query_compat.py`（pytest，标记 `compat`）：

```python
# 对每个 (metric, range, repo) 组合：
#   1. 调旧端点（overview/tokens 等）抽取对应数值
#   2. 调 run_metric_query() 取新序列
#   3. 断言：窗口聚合值一致（整数相等；比率/秒 ≤ 1e-6 容差）
# 覆盖矩阵：8 metric × 4 range × {全仓, 单仓} = 64 组
# 数据源：连 dev 库（MySQL，库名 grape_test_db），跑前打印行数基线
```

对账失败的处置：**以旧端点为正确基准**（其数值已在生产消费），修正 resolver 后重跑——对账脚本就是阶段 A 的定义性验收。

## 7. 实施顺序（5 个 commit，每个独立可回退）

| # | commit | 内容 | 依赖 |
|---|---|---|---|
| 1 | refactor(obs): 查询原语下沉 compat.py | git mv + import 调整，跑现有测试确认零回归 | 无 |
| 2 | feat(obs): query 包骨架 + registry + catalog API | context/registry + 空注册表 + `/q/metrics/catalog` 上线（此时 0 metric） | 1 |
| 3 | feat(obs): task 族 resolver（1-4）+ 单测 | tasks.py + 注册 | 2 |
| 4 | feat(obs): usage/engine/error resolver（5-8）+ 单测 | usage/engines/errors.py + 注册 | 2 |
| 5 | test(obs): 双跑对账脚本全绿 | compat 对账 64 组合 | 3,4 |

工期：commit 1 半天、2 一天、3 两天、4 一天半、5 一天——合计约 1 周。

## 8. 风险与未决问题

1. **agent/engine 维度过滤**：`agent_tasks` 有 agent_type、engine 经 runtime_id 关联——resolver 3/4 直接支持；llm_tokens 的 engine 过滤依赖 obs_llm_usage.engine_id（已存在），user 过滤依赖 user_id（部分历史行为空，按"未归属"桶语义：过滤 user 时空值不匹配任何用户）
2. **error_count 的日志源**：logs.py 扫文件（devops-bot.log + worker 日志），分桶前要统一时间戳解析——8 个 resolver 里唯一有真实新逻辑的
3. **并发与缓存**：本阶段不加缓存（调用量=对账和手测）；B 阶段前端切换时按 endpoint 加 5 分钟缓存（与 machine-health 同模式）
4. openapi.json 需重新生成（regenerate_openapi.py），新端点进契约

评审点（需拍板）：
- 序列骨架的 null/zero_fill 语义（§2.3）是否认可——决定 C 阶段 prometheus 导出时空桶的呈现
- 对账的"旧端点为基准"原则（§6）是否认可
- commit 切分粒度（1 周 5 个 commit）是否 OK
