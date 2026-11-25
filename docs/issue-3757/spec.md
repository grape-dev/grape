# Issue #3757 — 运行实例路由重复注册导致 operationId 唯一性失守

## 需求（Issue 原文收敛）

mgmt 进程（`src/api/grape_server.py`）每次生成 OpenAPI schema 刷出成片
`UserWarning: Duplicate Operation ID`：单次约 456 个不同 operationId 各报一次
（Loki 双流 ≈912 行/次，小时级 1824 行）。运行期 `/static/openapi.json` 本身干净
（390 paths / 469 operations、id 全唯一），说明**契约文件没坏，重复发生在内存里的
`app.routes`**：同一批 APIRoute 被挂了两遍，schema 里后写覆盖前写，契约的
「唯一性 + 幂等性」实际已失守。

## 目标

1. 消除 mgmt 进程的重复注册：单次 schema 生成 `Duplicate Operation ID` 计数 = 0；
2. `(method, path)` 与 `operationId` 在契约面严格唯一；
3. 新增 fail-fast 门禁（启动自检 + CI），重复注册再也回不去——不再依赖 FastAPI 的
   `UserWarning`（warning 不阻断启动）。

## 范围

### In Scope
- `src/api/grape_server.py`：两套用户面路由注册合并为**单一清单**；删除文件末尾的
  `for r in [...]: app.include_router(r)` 第二套循环；清理该尾块遗留的重复 import 与
  重复模块 docstring；装配末尾接入唯一性自检。
- `include_selected(runtime.router, methods={"GET"})` 与 `runtime_view.view_router` 的
  15 条 GET 交集去重（同一批 handler 的两次注册，属同一缺陷）。
- `src/api/server_common.py`：新增 `assert_route_registry_unique` / `route_registry_conflicts`
  / `has_route`（幂等装配判定），`include_selected` 增加 `exclude_ops`。
- 共享装配函数幂等化：`health_endpoints.install_health_endpoints` 与
  `spa.install_spa_routes` 的页面路由（此前只有 mount 做了「已挂过就跳过」）。
- CI 门禁 `scripts/check-openapi-endpoints.py`：注册表唯一性守卫必须展开
  fastapi≥0.140 的 `_IncludedRouter` 惰性包装，并覆盖**全部五个进程入口**。
- 测试 `tests/api/test_issue_3757_route_registry_unique.py`（正/反用例 + 门禁用例）。

### Out of Scope
- `/openapi.json` 恒 500（`contracts/deprecated_endpoints.json` 未落盘）→ #3756 独立跟踪，
  本单不触碰 `custom_openapi` / `load_deprecated_endpoints` 渲染语义。
- 契约版本号 bump 与 changelog 生成。
- 重复**中间件**装配（CORS / metrics / 异常处理器在 `build_app` 与入口各装一次）：
  不影响路由注册表与契约唯一性，留独立单跟进。

## 验收标准

- [x] 渲染 schema 不再出现 `Duplicate Operation ID`（mgmt / main 双入口实测 0 条）
- [x] `app.routes` 展开后 `len(ops) == len(set(ops))`（mgmt：675/675）
- [x] 契约面 `operationId` 集合大小 == operation 数（mgmt：546/546；修复前 546 个 operation
      上叠加 442 个重复 id）
- [x] 端点零丢失：修复前后唯一 `(method,path)` 集合完全一致（675 条，未增未减），
      `observability` / `runtime_view` 路由面仍在
- [x] 反向用例：注入重复 router / 重复 operationId 时自检抛 `RouteRegistryConflict`
- [x] CI 门禁在 `_IncludedRouter` 惰性包装下能抓到重复（此前该守卫对所有路由重复静默假绿）
- [x] 现有回归用例失败集合与基线一致（`tests/runtime/test_issue_596_*`、
      `test_issue_699_phase4_servers`、`tests/api/test_check_openapi_endpoints`、
      `test_frontend_favicon` 三个文件：基线 30 failed / 125 passed，改后同 30 / 125）

## 风险与处置

| 风险 | 影响 | 处置 |
|------|------|------|
| 合并时漏挂仅第二套注册的 router（observability / runtime_view）→ 404 | High | 端点集合逐条 diff（零丢失）；用例锁 `/api/obs/overview`、`/api/runtime/install` |
| 直接删 `include_selected` 会丢 view_router 未覆盖的 19 条 runtime GET | High | 改为 `exclude_ops` 只排已覆盖的 (method,path) 交集 |
| SPA/health 幂等化误删真实端点 | Med | 实测发现 `install_spa_routes` 独有 `/faro/collect`，故幂等做到**路由级**（已注册才跳过），不整段跳过 |
| 启动自检误伤其它进程/测试构造的临时 app | Low | 自检是普通函数，只在 `grape_server` 装配末尾调用；口径限契约面（`/api/` + `include_in_schema`） |