# Phase 5 Spec — 契约接口填充（Issue #699）

## 目标

填充 Phase 2 创建的三域契约空壳接口（MgmtService / RuntimeService / InsightService）的
LocalProvider 与 RemoteProvider 实现类，建立 GSB（Grape Service Bus）统一服务通信框架，
完成 `/internal/*` 端点暴露，并将跨 schema SQL 调用收口到契约接口。

## 范围

### 交付物

1. **GSB 框架** (`src/kernel/gsb/`)
   - `registry.py` — DB 驱动服务注册表（register/heartbeat/discover/deregister）
   - `client.py` — GsbClient 统一域间通信客户端（httpx + 服务发现 + HMAC 令牌）
   - `router.py` — GsbService 基类（注册/心跳/中间件挂载）
   - `provider.py` — Provider 工厂（Local/Remote 切换，动态注册）
   - `health.py` — 服务健康检查端点

2. **认证安全** (`src/kernel/auth_sdk/`)
   - `service_token.py` — HMAC-SHA256 服务令牌（生成/验证，TTL 60s）
   - `internal_middleware.py` — `/internal/*` 认证中间件（单机自动跳过）

3. **Provider 工厂** (`src/kernel/provider.py`)
   - `get_mgmt_service()` / `get_runtime_service()` / `get_insight_service()`
   - 根据 `GRAPE_MODE` / `GRAPE_PROVIDER_MODE` 自动选择 Local 或 Remote

4. **LocalProvider 实现** (`src/domains/contracts/local/`)
   - `mgmt_local.py` — MgmtServiceLocal（跨 schema SQL 查 grape_mgmt）
   - `runtime_local.py` — RuntimeServiceLocal（跨 schema SQL 查 grape_runtime）
   - `insight_local.py` — InsightServiceLocal（跨 schema SQL 查 grape_insight）

5. **RemoteProvider 实现** (`src/domains/contracts/remote/`)
   - `http_client.py` — ServiceHttpClient（连接池、超时、重试）
   - `mgmt_remote.py` — MgmtServiceRemote（HTTP API 调用 Mgmt 进程）
   - `runtime_remote.py` — RuntimeServiceRemote（HTTP API 调用 Runtime 进程）
   - `insight_remote.py` — InsightServiceRemote（HTTP API 调用 Insight 进程）

6. **`/internal/*` 端点** — 每个域进程暴露内部端点供 RemoteProvider 调用
   - `src/domains/runtime/routes/internal.py` — Runtime 内部端点
   - `src/domains/mgmt/routes/internal.py` — Mgmt 内部端点
   - `src/domains/insight/routes/internal.py` — Insight 内部端点

7. **契约注册** — `src/domains/contracts/__init__.py` 在导入时注册三个 Provider

8. **测试** — `tests/test_issue_699_phase5_contracts.py`（TC-5.1~TC-5.4）

### 不做

- 不替换现有业务代码中的裸跨 schema SQL（仅提供契约接口，替换留给后续渐进迁移）
- 不修改四进程入口的路由注册逻辑（Phase 4 已完成）
- 不引入新外部依赖（httpx 已在 pyproject.toml 中）

## 验收标准

| 编号 | 验证目标 | 命令 |
|------|---------|------|
| TC-5.1 | MgmtService 契约接口可正确查询 grape_mgmt schema | `python -c "from src.domains.contracts.mgmt_service import MgmtService; ..."` |
| TC-5.2 | RuntimeService 契约接口可正确查询 grape_runtime schema | `python -c "from src.domains.contracts.runtime_service import RuntimeService; ..."` |
| TC-5.3 | InsightService 契约接口可正确查询 grape_insight schema | `python -c "from src.domains.contracts.insight_service import InsightService; ..."` |
| TC-5.4 | 裸跨 schema SQL 消除验证（contracts/ 外无裸跨 schema SQL） | `grep -rn "grape_runtime\." src/domains/mgmt/ --include="*.py" | grep -v contracts/` |
