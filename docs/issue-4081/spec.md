# Issue #4081 需求文档（Spec 摘要）

> 结论先行（Issue Agent 已核实，本单以此为准）：`GET /api/plugins/agent/installed`、
> `POST /api/plugins/agent/report-missing` 在代码侧**早已落地**（`src/domains/mgmt/routes/plugins_agent.py`
> + `src/api/main.py` 注册 + `contracts/required_operations.json` 登记）。生产 8081 的
> 404（22 bytes `{"detail":"Not Found"}`）/ 405（31 bytes `{"detail":"Method Not Allowed"}`）
> 是**运行构建早于该路由合入**导致的发布漂移：GET 未命中路由 → 落 SPA catch-all
> `src/api/spa.py` 返回 404；同路径无 POST 处理器 → Starlette 直接 405。

## Objective

让插件市场「缺失发现 → 回补」闭环在生产**可观测、可告警、可降级**：缺端点这类发布漂移
不再静默，调用侧不再靠试错拿裸 404/405。

- 用户：runtime 侧执行中的 Agent（缺插件时）与平台值班（排障/告警）。
- 成功标准：插件代理端点不可用、或缺失上报发生降级时，分钟级可在指标/告警侧看到。

## Scope（本单交付）

1. **缺失上报计数**：`POST /api/plugins/agent/report-missing` 递增
   `grape_plugin_missing_report_total{result=submitted|degraded}`（此前只有 `logger.debug`）。
2. **告警规则**（`deploy/alerts.yml` 两条，severity=warning，`for: 5m`）：
   - `GrapePluginMissingReportDegraded`：降级（comment 通道回退）增量 > 0；
   - `GrapePluginAgentEndpointMissing`：`grape_plugin_agent_unimplemented_total` 增量 > 0
     （本构建未实现 → 501），或 Alloy 从 nginx access.log 派生的
     `nginx_upstream_requests_total{path=~"/api/plugins/agent/.*", status=~"404|405|501"}`
     增量 > 0（未升级的旧构建）。**不能**用应用侧
     `grape_http_requests_total{route=~...}`：它的 `route` 是路由模板，旧构建 GET 落
     SPA catch-all → `/{path}`、POST 未匹配 → `unmatched`，永不匹配该前缀。
3. **能力位**：`GET /api/plugins/agent/capabilities`（`include_in_schema=False`，不改端点计数）
   返回 `{available, installed, report_missing, missing_operations[]}`，数据源 =
   `contracts/required_operations.json` + `app.routes` 实际注册面（与
   `contract_integrity()` 的 missing 判定同口径），30s 缓存；调用侧「先查后调」。
   放在该命名空间而非 `/api/ping`：nginx `location = /api/ping` 指向 worker 进程
   （`deploy/nginx-grape-cluster.conf`），那里没有插件代理 router，会把「可用」误报成
   「不可用」；`/api/plugins/*` 走 `location /` → mgmt，才是回答该问题的进程。
4. **过渡期错误语义**：`/api/plugins/agent/*` 下**未登记的路径/方法**由路由级兜底返回
   `501 + error_code=NOT_IMPLEMENTED`（不再裸 404/405），并计入
   `grape_plugin_agent_unimplemented_total{method}`。兜底只覆盖该命名空间，
   **不改全局 404（SPA catch-all）语义**。

## Out of Scope

- `report-missing` 的入队/降级业务逻辑（已实现，恒 200 语义保持）；
- SPA catch-all 与全局 404 语义、插件市场前端改动；
- 生产 mgmt 实例的构建版本核对与发布（sre 动作）；
- 调用侧客户端本体（不在本仓）：本单交付其依赖的契约（能力位 + 501 语义），
  跨仓跟进项在 Issue 评论登记「遇 404/405/501 记一次结构化 WARNING、同任务内不重试」。

## Acceptance Criteria

- [x] `report-missing` 返回 200（submitted/degraded 两分支不变），指标按 result 正确递增，`/metrics` 可查；
- [x] `deploy/alerts.yml` 两条规则可加载，表达式引用真实指标名且选择器能命中真实标签
  （用例按 PromQL 语义求值 + SPA-only app 行为级验证）；
- [x] 能力位正确报告两端点可用性；缺任一操作时 `missing_operations` 给出 `operationId+method+path`；
- [x] 未实现端点返回 `501 + error_code=NOT_IMPLEMENTED`（用例覆盖 GET/POST/错误方法）；
- [x] 全局 404 语义不变（命名空间外仍 404）；
- [x] 现有测试全绿，`contract_integrity()` 报告零缺失（新增路由 `include_in_schema=False`，不改 endpoint_count）。