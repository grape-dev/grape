# Issue #4081 方案设计：以「指标 + 告警 + 能力位 + 过渡期错误语义」收敛契约漂移

## 关键判断

两端点在代码侧已存在，本单**不重写端点**，而是把「端点是否可用」从运行期试错
前移到发布期可验证、运行期可观测。四条改动都落在既有分层，无新增契约端点
（`include_in_schema=False`），因此不触发 `contracts/openapi_baseline.json` 重定基。

| 层 | 改动 | 位置 |
|---|---|---|
| 指标注册 | `grape_plugin_missing_report_total{result}`、`grape_plugin_agent_unimplemented_total{method}`（含名字常量，供告警测试对齐） | `src/domains/insight/observability/__init__.py` |
| 上报埋点 | `report_missing` 按 `submit.submitted` 记 `submitted`/`degraded`，降级加结构化 WARNING | `src/domains/mgmt/routes/plugins_agent.py` |
| 过渡期语义 | 路由末位 `api_route("/{rest:path}")` 兜底：未登记路径/方法 → `501 + error_code=NOT_IMPLEMENTED` + 计数 | 同上 |
| 能力位 | `plugin_agent_capabilities(app)`：`load_required_operations()` × `app.routes`（`_iter_routes` 展开 fastapi≥0.140 惰性包装），30s 缓存，异常 fail-closed | `src/api/openapi.py` |
| 能力位出口 | `/api/ping` 增 `plugin_agent` 段（构造失败降级 `available=false+error`，探针不拉垮） | `src/api/server_common.py`（四进程/mgmt 主路径）、`src/api/health_endpoints.py`（legacy 入口同面） |
| 错误码 | `api_error()` 增可选 `error_code`（与 deps.py 401 族同口径，未传时响应体形态不变） | `src/api/errors.py` |
| 告警 | `GrapePluginMissingReportDegraded`、`GrapePluginAgentEndpointMissing` | `deploy/alerts.yml` |

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 能力位出口 | 新增 `/api/plugins/agent/capabilities` / `/api/ping` 附加段 | 命名空间内新路由（`include_in_schema=False`） | nginx `location = /api/ping` → worker 进程（没挂本 router），放 ping 上会把「可用」误报成「不可用」；`include_in_schema=False` 不改 endpoint_count，无需契约重定基 |
| 能力位数据源 | 跑 `custom_openapi(app)` / 路由表 × 登记清单 | 路由表 × 登记清单 | `custom_openapi` 在降级态抛 `ContractDegradedError`，不能在探针里用；路由表判定与 `contract_integrity` 的 `seen.get(oid) != (path, method)` 同口径 |
| 404/405 改 501 | 全局 catch-all / 路由内兜底 | 路由内兜底（末位） | 全局改造会波及鉴权/前端兜底与既有 404 断言；命名空间兜底语义精确 |
| 「已知路径 + 错误方法」 | 保留 405 / 统一 501 | 统一 501 | 调用侧约定「404/405/501 = 不支持插件代理」，单一判定分支；且该命名空间内裸 405 正是本单要消除的歧义 |
| 告警第二分支 | 仅新计数器 / 新计数器 `or` 已登记路由 404-405-501 速率 | 两者 `or` | 旧构建没有兜底路由，计数器为 0；用 `grape_http_requests_total{route=~...}` 兜住未升级构建 |

## 兼容性

- `report-missing` / `installed` 的入参、返回体、恒 200 语义不变（仅新增指标与日志）；
- `api_error` 新增参数为可选，既有调用方响应体形态逐字不变（用例守护）；
- `/api/ping` 只新增 `plugin_agent` 键，`{ok, ts}` 不变（既有探针测试/gsb 健康检查不受影响）；
- 兜底路由 `include_in_schema=False`：`/openapi.json` endpoint_count、`contract_integrity()` 零影响。

## 风险

| 风险 | 缓解 |
|---|---|
| 探针变重（每次 ping 遍历路由/读清单） | 30s TTL 缓存 + `load_required_operations()` 模块级缓存；清单读取只发生一次 |
| 兜底吃掉已登记端点 | 兜底必须是文件最后一条路由；用例断言 `installed` 仍 200、登记清单路径/方法未变 |
| 能力位误报（惰性 `_IncludedRouter` 未展开） | `_registered_pairs` 复用 `openapi._iter_routes`，与契约渲染同一套展开逻辑；用例断言 mgmt app 两标志为 true |
| 新告警误报 | `for: 5m` + 仅插件代理路径前缀；降级规则只在真实降级时计数 |
| 生产仍是旧构建 → 代码全绿但线上 404/405 | 告警第二分支兜住；能力位用于发布后验收；构建版本核对属 sre |