# Phase 5 Plan — 契约接口填充（Issue #699）

## 实现方案

### 1. GSB 框架（src/kernel/gsb/）

**registry.py** — DB 驱动服务注册表
- `gsb_service_registry` 表 DDL（service_name PK, base_url, health_url, version, status, last_heartbeat, metadata）
- `register_service(name, base_url, health_url, metadata)` — 启动时注册
- `heartbeat_service(name)` — 30s 心跳更新
- `discover_service(name)` — 发现服务（带 90s 心跳超时检查）
- `list_services()` / `deregister_service(name)`
- 使用 `src.kernel.session_manager.db, db_lock`（与现有 DB 层一致）
- 注册表表名不进 TABLE_REGISTRY（GSB 基础设施表，不走 schema 前缀）

**client.py** — GsbClient 统一客户端
- 环境变量优先 > DB 注册表 > localhost 回退
- `_ENV_URL_MAP`: mgmt→GRAPE_MGMT_URL, runtime→GRAPE_RUNTIME_URL, insight→GRAPE_INSIGHT_URL
- 30s URL 缓存
- httpx.Client（timeout 10s, connect 5s, retries 2）
- HMAC 令牌认证头（X-Service-Token, X-Service, X-Internal-Call）
- get/post/stream_sse 方法

**router.py** — GsbService 基类
- `__init__(name, port, app)` — 接收 FastAPI app
- `mount()` — 注册服务 + 挂载 internal_router + 启动心跳任务
- `_heartbeat_loop()` — 30s asyncio 循环
- startup 注册、shutdown 注销

**provider.py** — Provider 工厂
- `register_provider(name, local_cls, remote_cls)` — 动态注册
- `get_service(name)` — 根据 GRAPE_MODE/GRAPE_PROVIDER_MODE 选择 Local/Remote
- 便捷函数: `get_mgmt_service()`, `get_runtime_service()`, `get_insight_service()`

**health.py** — 健康检查端点
- `/api/health` 返回 service_name, status, uptime

### 2. 认证安全（src/kernel/auth_sdk/）

**service_token.py**
- `SERVICE_SECRET = os.environ.get("GRAPE_SERVICE_SECRET", "")`
- `generate_service_token(service_name, target_service="")` — HMAC-SHA256, TTL 60s
- `verify_service_token(token, expected_target="")` — 验证签名 + TTL + 常量时间比较
- 空密钥时单机降级（返回空串/验证通过）

**internal_middleware.py**
- `InternalAuthMiddleware(BaseHTTPMiddleware)`
- 仅拦截 `/internal/*` 路径
- 空密钥跳过（单机模式）
- 验证 X-Service-Token 头

### 3. Provider 工厂（src/kernel/provider.py）
- 兼容入口：直接调用 gsb.provider 的便捷函数
- 保持与 design §7.1 代码示例一致

### 4. LocalProvider 实现（src/domains/contracts/local/）

每个 Local 类继承对应的 ABC，用 `t_mgmt()/t_runtime()/t_insight()` 映射表名，
通过 `src.kernel.session_manager.db, db_lock` 执行跨 schema SQL。

**mgmt_local.py** — MgmtServiceLocal
- `get_repo_webhook_config(repo_key)` → SELECT FROM t_mgmt('managed_repos')
- `get_agent_session(repo_key, agent_type)` → SELECT FROM t_mgmt('devops_agent_session')
- `get_scene_config(repo_key)` → SELECT FROM t_mgmt('devops_pipeline_scene')
- `get_scene_pool_engine_ids(scene_id)` → SELECT FROM t_mgmt('devops_pipeline_scene')
- `verify_session_token(token_hash)` → SELECT FROM t_mgmt('session_api_tokens')
- `get_bot_config(repo_key)` → SELECT FROM t_mgmt('managed_repos')
- `get_skill(skill_id)` → SELECT FROM t_mgmt('skill')

**runtime_local.py** — RuntimeServiceLocal
- `get_events(repo_key, limit)` → SELECT FROM t_runtime('events')
- `get_event_detail(seq)` → SELECT FROM t_runtime('events')
- `get_tasks(repo_key, limit)` → SELECT FROM t_runtime('agent_tasks')
- `get_task_messages(task_id, after_id)` → SELECT FROM t_runtime('task_messages')
- `get_runtimes()` → SELECT FROM t_runtime('agent_runtimes')
- `get_issue_state(repo_key, issue_number)` → SELECT FROM t_runtime('issue_state')
- `update_issue_state(repo_key, issue_number, patch)` → UPDATE t_runtime('issue_state')

**insight_local.py** — InsightServiceLocal
- `log_obs(entry)` → INSERT INTO t_insight('obs_log')
- `log_llm_usage(usage)` → INSERT INTO t_insight('obs_llm_usage')
- `log_api_latency(entry)` → INSERT INTO t_insight('api_latency_log')
- `query_metrics(repo_key, window)` → SELECT FROM t_insight('obs_metric_rollup')
- `query_slo(repo_key)` → SELECT FROM t_insight('obs_slo')
- `query_alerts(repo_key)` → SELECT FROM t_insight('obs_alert_events')
- `aggregate_runtime_stats()` → 聚合 t_runtime('events')/t_runtime('agent_tasks')

### 5. RemoteProvider 实现（src/domains/contracts/remote/）

**http_client.py** — ServiceHttpClient
- 与 GsbClient 类似但更轻量（直接 base_url，不走 DB 发现）
- get/post/stream_sse

**mgmt_remote.py / runtime_remote.py / insight_remote.py**
- 每个方法映射到对应 `/internal/*` 端点的 HTTP 调用

### 6. /internal/* 端点

**src/domains/runtime/routes/internal.py**
- GET /internal/tasks, /internal/tasks/{task_id}, /internal/tasks/{task_id}/messages
- GET /internal/events, /internal/events/{seq}
- GET /internal/runtimes
- GET /internal/issues/{repo_key}/{issue_number}/state

**src/domains/mgmt/routes/internal.py**
- GET /internal/repos/{repo_key}/webhook-config
- GET /internal/sessions/{repo_key}/{agent_type}
- GET /internal/scenes/{repo_key}
- GET /internal/scenes/{scene_id}/engine-ids
- POST /internal/tokens/verify
- GET /internal/repos/{repo_key}/bot-config
- GET /internal/skills/{skill_id}

**src/domains/insight/routes/internal.py**
- POST /internal/logs, /internal/llm-usage, /internal/latency
- GET /internal/metrics/{repo_key}, /internal/slo/{repo_key}, /internal/alerts/{repo_key}
- GET /internal/dashboard/overview

### 7. 契约注册（src/domains/contracts/__init__.py）

导入时自动注册三个 Provider 到 gsb.provider：
```python
from src.kernel.gsb.provider import register_provider
from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
from src.domains.contracts.remote.mgmt_remote import MgmtServiceRemote
register_provider("mgmt", MgmtServiceLocal, MgmtServiceRemote)
# ... runtime, insight
```

### 8. 测试（tests/test_issue_699_phase5_contracts.py）

- TC-5.1: MgmtService Local provider 查询 grape_mgmt 表
- TC-5.2: RuntimeService Local provider 查询 grape_runtime 表
- TC-5.3: InsightService Local provider 写入/查询 grape_insight 表
- TC-5.4: contracts/ 包外无裸跨 schema SQL（静态检查）
- Provider 工厂切换测试（GRAPE_MODE=single → Local, GRAPE_MODE=cluster+remote → Remote）
- GSB 注册表/客户端基本功能测试
- 服务令牌生成/验证测试

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| LocalProvider SQL 与现有业务代码重复 | Phase 5 仅提供接口，不替换现有调用；后续渐进迁移 |
| RemoteProvider 需要 httpx | httpx 已在 pyproject.toml 依赖中 |
| /internal/* 端点注册到四进程 | 仅在 cluster 模式下挂载，single 模式不加载 |
| 契约接口签名与 Phase 2 空壳不一致 | 严格继承 Phase 2 ABC，不修改接口定义 |
