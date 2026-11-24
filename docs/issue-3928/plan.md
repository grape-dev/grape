# Issue #3928 Plan — 后端能力自检 / 重派熔断 / 跨引擎告警

## 设计
1. `backend_capability.py`（新增，worker/server 共用）
   - 引擎类型 → 后端命令映射；`shutil.which` 探测 + TTL 缓存。
   - 状态机：available / unavailable / degraded / unknown。
   - `status_from_probe()` 将既有 probe_results 映射为能力状态，兼容旧 worker。
2. `backend_health.py`（新增，服务端）
   - `agent_runtimes` 新列：backend_status / backend_reason / backend_checked_at /
     circuit_open_until / circuit_fail_count / circuit_half_open。
   - 注册/心跳观测落库；unavailable 立即 `healthy=0` 并带 runtime_id WARNING；degraded 摘认领资格但
     不改写 healthy（保留 #1982 阈值语义），并开短冷却（120s 起、封顶 600s）。
   - 熔断：runtime_id 粒度，指数冷却（unavailable 默认 300s 起、封顶 3600s；degraded 短冷却）；
     冷却后允许一次半开试探。
   - 认领闸门：`claim_allowed()` / 纯判定 `claim_eligible()`（认领、池可达性、派发候选共用）；
     半开由 `note_claim_started()` 置位；只有真实执行证据（成功产出 / 非 -1 exit / 引擎进程内报错）
     才释放熔断，spawn 前失败（skill_mismatch / prompt 空 / workspace baseline / credential_*）不释放。
   - 跨引擎：WARNING + 计数 + 按 task 去重（有界 LRU）。
3. `backend_failover.py`（新增）
   - `is_backend_unavailable_error()`：CLI not found / not found in PATH / engine startup failed /
     codearts not found 等 spawn 期特征。
   - `handle_task_failure()`：失败即熔断 runtime；未超上限走 `_do_requeue`（幂等，attempts+1），
     超上限记录 BACKEND_UNAVAILABLE_EXHAUSTED 并交由 fail_task 判死告警。
4. 接入
   - worker `_discover_local_runtimes` / probe 结果附 backend capability。
   - `runtime_register` / `runtime_heartbeat` 落 capability；installed=false 不再只写 installed=0。
   - `service._has_verified_executor` 收紧：fail/failed/spawn_failed/degraded/unavailable 与
     healthy=0 一律不认领（worker_version 不再短路放行）；unknown 保持放行。
   - `claim` / `claim_by_tag` / 池可达性判定 / `engine_select` 派发候选统一加熔断闸门；
     degraded 经冷却闸门放行后可进入一次半开试探（`cooldown_gate_passed`）。
   - `fail_task` 先走后端 failover，再走原有瞬时重试；`complete_task` 成功释放熔断。
5. 存储
   - 新列同时进 `schema_ddl`（新库）与注册式幂等迁移 202611240004（存量）。

## 测试 seam
- 探针：纯函数 `probe_engine_capability`（注入 which/clock）。
- 认领：`_has_verified_executor` 纯函数 + `claim_by_tag` 线上路径（公开 DB/凭证脚手架）。
- 重派熔断：`handle_task_failure` + `claim_allowed/note_claim_started/note_claim_outcome`。
- 可观测：`note_cross_engine` 计数/去重 + WARNING 字段（caplog）。