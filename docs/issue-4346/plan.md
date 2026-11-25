# Plan：Issue #4346 实现方案

## 现状

- 会话建立发生在 worker 侧 ACP 客户端（`src/domains/runtime/agents/{hermes,aishell,hwcloud}_acp.py`），`session/new` 超时预算 100s；失败以字符串进入 `engine errors:` / `session failed:`，服务端 `service.fail_task` 只做「后端启动失败（#3928）→ 熔断+requeue」与「瞬时上游抖动（#403）→ 退避重试」，session 超时不在任何分类内，直接判 failed。
- 引擎准入已有两把尺子：`backend_health.claim_allowed`（backend_status + circuit_*，认领/池可达/派发候选共用）与 `engine_select._backend_claimable_map`（派发候选）。健康分 `health_scorer` 只统计任务层 `ENGINE_OFFLINE` 语义。
- 注册链路 `routes/runtime.py::runtime_register` 落 `backend_capability`；worker `_discover_local_runtimes` 上报 healthy/能力。无 LLM provider 校验。

## 方案

### 1. 新模块 `engine/session_guard.py`（session 通道独立于 ENGINE_OFFLINE）

- `classify_session_failure(text)` → `{error_code, stage, reason}`：`No LLM provider` → `ENGINE_MISCONFIGURED`；`session/new|session/resume ... timeout` → `SESSION_CREATE_BLOCKED`；write/EOF/connection refused/spawn → `ENGINE_UNREACHABLE`。
- `failure_payload(runtime_id, engine_name, cls)` → 固定含 `engine_id/queue_depth/disk_avail_bytes`（失败取 `None`；`queue_depth` 由 agent_tasks 在跑/排队计数，`disk_avail_bytes` 由 `shutil.disk_usage("/")`）。
- 熔断状态落 `agent_runtimes` 新列：`session_fail_streak / session_fail_total / session_circuit_until / session_circuit_half_open / session_last_error_code / session_last_failure_at / session_last_success_at / llm_provider_ok / llm_provider_reason`。
  - 每次 `SESSION_CREATE_BLOCKED` 记 streak+1；streak < N → 短冷却（默认 60s，保证 requeue 后不会回到同引擎）；streak ≥ N → `ENGINE_SESSION_CIRCUIT_OPEN` 长熔断（默认 600s）。
  - 冷却结束 → `session_circuit_half_open=0` 时放行一次探测（认领即置占位）；成功 `note_session_success` 清零，失败续熔断。
- `session_claim_allowed(runtime_id)`：session 熔断/`llm_provider_ok=0` → False（fail-open：缺列/查询异常不拦）。

### 2. 换引擎重试（复用现有 requeue 链路）

`service.fail_task` 在 #3928 backend failover 之后、#403 瞬时重试之前插入 `session_guard.handle_task_failure`：

- 分类并**留痕**：`agent_tasks.errors` 追加结构化条目（含上述三字段），事件 `payload._session_fail` 合并写入（`merge_event_payload`）。
- `SESSION_CREATE_BLOCKED` → 记 streak/冷却 → `_do_requeue`（attempt ≤ 1，默认 1）；引擎被 session 冷却挡在认领闸门外 → 实际由另一健康引擎认领（= 换引擎）。重试失败（attempts 超限/无其它可用引擎）→ 返回 exhausted，走既有判死语义（不原地挂起），并写 `NO_SESSION_ENGINE_AVAILABLE` / `..._EXHAUSTED` 原因。
- `ENGINE_MISCONFIGURED` → 标记该引擎 provider 不可用（`llm_provider_ok=0` + 长熔断 + healthy=0）→ 同样换引擎重试。

### 3. 派发/认领过滤

- `backend_health.claim_allowed` 追加 session 闸门（认领、池可达、派发候选四处调用点自动同尺）。
- `engine_select._backend_claimable_map` 查询 session 列 + `llm_provider_ok`，`claim_eligible AND session_eligible`。
- 认领成功点补 `note_session_probe_started`（半开占位）；任务真实执行证据（`note_claim_outcome(executed=True)`）→ `note_session_success`。

### 4. 健康分透出

`health_scorer.get_health_score / _compute_and_persist / _empty_score` 增补 `session_timeout_streak / session_failure_total / session_circuit_open / session_circuit_until / session_last_error_code`，不改变既有 `ENGINE_OFFLINE` 计分与 `is_healthy` 语义。

### 5. LLM provider 前置校验

- 新模块 `engine/provider_check.py`（与 `dependency_check.py` 同层）：`validate_engine_llm_provider(engine_name, ...)` 读 hermes `config.yaml`（复用 `engine_model.read_hermes_model_config`）/opencode 配置。判定口径：配置文件**存在**但解析不到 `(base_url, api_key)` → `{ok: False}`；配置文件缺失 → 跳过校验（`engine_model` 既有「返回 None = 调用方跳过」约定，且派发期 `llm_cred_selector` 可能注入凭据），避免把无本地配置的合法 worker 误摘出池。
- worker `_discover_local_runtimes` 上报 `llm_provider`；配置存在但无可用 provider → healthy=False（拒绝健康注册）。
- 服务端 `runtime_register` 落 `llm_provider_ok/llm_provider_reason`；worker 上报缺失 provider → `healthy=0` + `ENGINE_MISCONFIGURED` 日志（存量行不进调度池）。

### 6. 回归守护

- ACP worker 侧失败文案追加稳定分类标记（`session_guard.format_session_error`），服务端分类不依赖文案微调。
- #423 解析侧回归：新增断言 `_ACP_READ_MAX_LINES` 防御上限 > 10 且 deadline 驱动读取用例保持绿（既有 `tests/insight/test_probe_acp_read_response_423.py`）。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 熔断误伤可用引擎池 | 默认 N=3/600s，可配；半开探测；短冷却仅 60s |
| 重试放大负载 | 仅 1 次 + requeue 归池（非原地重试） |
| 新列缺失旧库导致查询异常 | 全部读路径 fail-open（异常→不拦截），迁移幂等补列 |
| 事件字段新增破坏下游 | 字段固定但值可为 null，独立命名空间 `_session_fail` |
| provider 校验误杀 | 仅「明确缺失」才拒绝；解析失败/未知引擎放行 |