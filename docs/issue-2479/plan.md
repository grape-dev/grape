# Plan: Issue #2479 — 方案设计

> 前置：docs/issue-2479/spec.md | 基线分支 `arch-weekly-11-ljt`

## 1. 总体架构

```
业务调用 get_service(name)
        │
        ▼
┌─ provider.get_service ─────────────────────────────┐
│ 1) 矩阵守卫：caller ∈ {runtime, insight} 且 target=mgmt │
│    → 直接返回 Local 实例（不查任何配置）                  │
│ 2) GRAPE_PROVIDER_MODE ≠ remote                       │
│    → 直接返回 Local 实例（存量零回归）                   │
│ 3) 否则返回 _RoutedService 代理（按方法逐次决策）         │
└──────────────┬──────────────────────────────────────┘
               ▼ 每次方法调用
        decide_mode(service, method)
          ├─ 矩阵守卫（恒优先）→ lib
          ├─ 配置表缓存（route_config.snapshot，TTL+version）
          │    └─ 读取失败 → 上一次已知配置（启动期兜底）
          └─ 内置基线：热路径四项→lib；public 原语→lib；其余→remote
               ▼
        local_instance / remote_instance 调用
          └─ remote 通道传输异常 → GsbRemoteTimeoutError（fail-fast，不回落 lib）
```

## 2. 模块设计

### 2.1 `src/kernel/gsb/route_config.py`（新增，kernel 层不 import domains）

- 表 `gsb_provider_route`（复用 `gsb_service_registry` 同库 = `session_manager.db`）：
  `route_key TEXT PRIMARY KEY, mode TEXT NOT NULL, timeout_ms INTEGER DEFAULT 1000,
  version INTEGER DEFAULT 1, updated_at REAL, updated_by TEXT`。
- 运行时幂等 `_ensure_table()`（同 registry 模式）+ 迁移脚本
  `scripts/migrations/006_gsb_provider_route.py`。
- 读写接口：`set_route()` / `remove_route()` / `list_routes()` /
  `set_all_known(mode)`（一键全量回滚）/ `get_cached_route()`。
- 进程内缓存：全表快照 + TTL（`GRAPE_GSB_ROUTE_TTL`，默认 30s）；单飞刷新；
  DB 读失败保留旧快照继续服务（退回上次已知配置）。
- 静态 key registry（kernel 不能 import domains，故以字面量镜像 + CI 同步测试防漂移）：
  `CONTRACT_METHODS`（23）与 `PUBLIC_PRIMITIVES`（mgmt 39 + runtime 66 数据原语；
  `available`/`health` 探活口不参与路由）。

### 2.2 `src/kernel/gsb/provider.py`（修改）

- `caller_domain()`：`GRAPE_PROCESS_ROLE` / `GRAPE_SERVICE_NAME` 显式角色映射
  （server→mgmt、event/worker→runtime、insight→insight），回落
  `db_registry.current_domain()`（DEVOPS_DB_NAME）。
- `matrix_forces_lib(target)`：caller ∈ {runtime, insight} 且 target == mgmt。
- `decide_mode(service, method)`：矩阵守卫 → 全局开关 → 配置表缓存 → 内置基线。
- `_RoutedService` 代理：惰性各持一个 Local/Remote 实例；`__getattr__` 仅对公共
  可调用属性生成派发闭包；remote 调用包装传输异常为 `GsbRemoteTimeoutError`
  （业务异常原样抛出）。
- `get_service`：矩阵守卫 / 非 remote 模式直接返回 Local 实例（热路径零代理开销）；
  remote 模式返回代理。`get_local_service` 不变（恒 Local）。

### 2.3 `src/kernel/gsb/client.py`（修改）

- 新增 `GsbRemoteChannelError` / `GsbRemoteTimeoutError`（稳定 `code`：
  `GSB_REMOTE_CHANNEL_ERROR` / `GSB_REMOTE_TIMEOUT`），供 provider 代理包装。
- metrics status 归一化：超时→`remote_timeout`、连接/传输→`remote_unreachable`、
  HTTP 业务错误→数字状态码（成功仍为数字码）——`observe()` 标签即可区分超时与业务错误。

### 2.4 `scripts/check_gsb_route_matrix.py`（新增 CI 静态扫描）

- 扫描 `src/domains/runtime/`、`src/domains/insight/`：
  禁止引用 `MgmtServiceRemote`、`GsbClient("mgmt")`、`ServiceHttpClient("mgmt")`、
  直接访问 `_PROVIDERS[`。
- 输出 public 原语清单逐个归属标注（mgmt 原语：runtime|insight 调用方 → lib-only）。
- 退出码：0=通过；1=命中违规（CI 可红）；2=fail-closed（没扫到文件）。
- 接入 `scripts/check-all.sh`。

### 2.5 `scripts/bench_gsb_route_p95.py`（新增压测）

- 同进程、同参数、同数据量：lib = Local 直调；remote = 真实 loopback HTTP
  （FastAPI+uvicorn 挂载真实 `/internal/*` 路由）经 `GsbClient`（含服务发现缓存态 +
  HMAC + 序列化）。
- 契约 23 方法 lib/remote 各 ≥200 次取 P95；public 原语 lib 采样 200 次
  （remote 通道本期不存在，标注 lib-locked / N/A）。
- 阈值判定：增量 ≤10ms 且相对增幅 ≤20%；超阈值即从 remote 候选除名并提示锁 lib。
- 产出 markdown 报告（stdout / `--out`）。

## 3. 关键决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 全局开关保留 | `GRAPE_PROVIDER_MODE=remote` 才进按方法路由 | 存量单机/共库零回归；缺省 remote 偏好只在 remote 制式内生效 |
| 代理粒度 | 服务级代理 + 方法级决策 | 热路径在非 remote 模式下完全不走代理（零开销） |
| fail-fast 边界 | provider 代理包装传输异常，client 保持原始异常语义 | 不破坏既有 GsbClient 测试口径；错误码在业务调用面稳定 |
| key registry | kernel 字面量镜像 + 同步测试 | kernel 不能 import domains（check-arch R1 HARD） |
| 原语 remote | 本期仅登记路由 key，传输通道后续 Issue | Spec Out of Scope：不新增传输层基础设施 |

## 4. 风险与缓解

- remote 一跳延迟影响热路径 → 热路径白名单内置 lib + 非 remote 模式零代理 + 压测数据。
- 配置表缓存不一致 → version+TTL 单飞刷新，读失败退回上次快照。
- 矩阵被绕过 → 运行时守卫（不依赖配置表）+ CI 静态扫描 + 单测双向验证。
- key registry 漂移 → 同步测试把静态表与 contracts/public 实际面锁定，CI 红。

## 5. 测试计划

`tests/kernel/test_gsb_route.py`：基线取值 / 配置表覆盖 / TTL 热切换（≤30s 实测）/
缓存读失败退回 / 矩阵守卫（契约+原语+get_service 三口径）/ 代理逐方法派发 /
fail-fast 不回落 + 错误码 / metrics 标签区分 / 一键全量回滚 / key 同步 /
静态扫描可红；另更新 `test_gsb_service.py` 中编码旧全局行为的 1 条用例。
