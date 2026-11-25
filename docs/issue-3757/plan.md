# Issue #3757 — 实现方案

## 根因（逐行核对 `src/api/grape_server.py`）

mgmt 进程的路由存在**三条**重复注册来源，彼此叠加：

1. **两套用户面注册同时生效**（本 Issue 主体）
   - 第一套：`526-625` 行的显式 `app.include_router(...)` 清单；
   - 第二套：文件末尾 `for r in [30 个 router]: app.include_router(r)` 循环
     （`main.py` 尾块整段移植进本文件时留下的）。
   - 交集 28 个 router → 同一批 operationId 在 `app.routes` 里出现两次，
     `get_openapi()` 第二次命中即 `UserWarning: Duplicate Operation ID`（456 条）。
   - 两套各有独有项：第一套独有 `categories` / `im` / `takeover` /
     `org_repos` / `org_dispatches` / `org_context_store` / `org_knowledge` /
     `org_audit` / `org_analysis`、以及 `runtime.router` 的 GET 子集；第二套独有
     `observability` / `runtime_view`。**任一套直接删都会丢端点**，故取并集。
2. **同一批 handler 的两次注册**（`server_common.include_selected` 与 `runtime_view`）
   - `include_selected(app, runtime.router, methods={"GET"})` 挂 35 条 runtime GET；
     `runtime_view.view_router` 又注册了其中 15 条（同一函数对象，operation_id 同名）
     → 再叠 15 条重复。
3. **共享装配函数不幂等**（`attach_health` / `install_health_endpoints`、
   `attach_frontend` / `install_spa_routes`）
   - `spa.install_spa_routes` 只对 **mount** 做了「已挂过就跳过」，**页面路由**没有：
     `/`、`/{path:path}`、`/admin`、`/account`… 被注册两遍（mgt 17 条，main 18 条）；
   - `/health*`、`/api/ping`、`/metrics` 同理被注册两遍。
   - 这些不进 `/api/` 契约面，但让「路由注册表唯一性」整体失真，也是同一份残留。

## 改动

### 1. `src/api/grape_server.py`
- 删除文件末尾的 `for r in [...]` 第二套循环；把其独有的 `observability.router`、
  `runtime_view.view_router` 并入上方单一清单（追加在 `knowledge.router` 之后，
  保持原挂载顺序语义）。
- `include_selected(runtime.router, methods={"GET"})` 传入
  `exclude_ops={(METHOD, path) | view_router 已注册}`，只补挂 view 未覆盖的 19 条 GET。
- 清理尾块残留：重复 import（`sys/os/asyncio`、`ensure_python_version`、
  `FastAPI/Request`、各路由模块、`admin_router`、`git_repo_router`、
  `runtime_view_router`、未使用的 `JSONResponse/StaticFiles`）与重复模块 docstring；
  其内容改写为注释（A 进程职责 / 启动命令行不变）。
- 装配末尾新增 `server_common.assert_route_registry_unique(app)`（自检必须在最后一步，
  否则其后注册的路由逃检）。

### 2. `src/api/server_common.py`
- `include_selected(..., exclude_ops=None)`：排除指定 `(METHOD, path)`，避免「交集补挂」。
- `RouteRegistryConflict(RuntimeError)` + `route_registry_conflicts(app)` +
  `assert_route_registry_unique(app)`：复用 `openapi._iter_routes` 展开
  `_IncludedRouter`，校验契约面（`/api/` 且 `include_in_schema`）的
  `(method,path)` 与显式 `operationId` 唯一；冲突即抛错并列出冲突项。
- `has_route(app, path, method=None)`：幂等装配判定（先注册者才是生效实现）。

### 3. `src/api/spa.py` / `src/api/health_endpoints.py`
- 页面/健康路由改为「已注册即跳过」的路由级幂等装配（`_spa_get` / `_spa_post` /
  `_health_get`）：等价的第二份装配不再进 `app.routes`；各自独有端点
  （`/faro/collect`、`/api/ping`）照常补装，不丢。

### 4. `src/api/main.py`
- `/metrics` stub 加 `has_route` 守卫：`attach_health(readiness=True)` 已注册 `/metrics`，
  此前第二份既污染注册表又在 schema 里刷一条 `Duplicate Operation ID`。

### 5. `scripts/check-openapi-endpoints.py`（CI 门禁）
- `check_route_registration_unique` 改为经 `_iter_routes` 展开后再判定，并新增
  fail-closed：一个 APIRoute 都展开不出来 → 直接 FAIL（防门禁与新 fastapi 失配后假绿）。
- 新增 `check_app_operation_ids_unique`（路由注册表层的 operationId 重复）。
- 新增 `check_process_apps_route_registry`：对 **main / mgmt / event / worker / insight
  五个入口**逐个跑（此前只扫单进程 `main`——被扫的干净、没扫的重复，门禁长期假绿）。

## 关键决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 以哪一套为准 | 删第一套 / 删第二套 | 第一套 + 并集 | 第一套含 9 个仅此注册的 router；取并集不动挂载顺序 |
| runtime 视图去重 | 删 view_router / 删 include_selected / 排除交集 | 排除交集 | 两者都不完整（view 少 19 条 GET，runtime 少 4 条 PUT + faro），排除后端点零丢失 |
| 幂等粒度 | 整段跳过装配 / 路由级跳过 | 路由级 | 整段跳过会丢 `install_spa_routes` 独有的 `/faro/collect`（实测） |
| 自检范围 | 全部路由 / 契约面 | 契约面（`/api/` + `include_in_schema`） | 与 openapi 渲染口径一致；SPA/health 非契约面重复不属本 Issue 判定面 |
| 自检位置 | 入口模块内联 / 共享函数 | 共享函数 `server_common` | 五个入口可复用，CI 门禁与启动自检同源码级实现 |

## 验证

- `python3 -m pytest tests/api/test_issue_3757_route_registry_unique.py -q` → 19 passed
- `python3 scripts/check-openapi-endpoints.py --require-baseline` → OK（401 paths，契约面无重复）
- 端点集合 diff（修复前 HEAD app vs 修复后）：`lost=[] added=[]`，唯一 `(method,path)` 均 675
- 重复注册计数：mgmt 544 → 0，main 18 → 0；`Duplicate Operation ID` 告警 mgmt 442 → 0
- 既有回归文件失败集合与基线完全一致（30 failed / 125 passed，全为与本单无关的
  nginx/systemd 三进程文本断言等基线红灯）