# Issue #490 — 方案设计与任务拆解

## 方案

### A. 服务端幂等去重（`src/api/routes/runtime.py`）

`runtime_heartbeat` 中处理 `body.probe_results` 时，对每条 `{status, latency, last_probe, error}`：

1. 先 `SELECT probe_last FROM agent_runtimes WHERE name=? AND credential_id=?`。
2. **相同 `last_probe`（重放）**：只刷新 `probe_status/probe_latency/probe_last/probe_error`（值相同，幂等），**不触碰 `probe_fail_count` 与 `healthy`**。
3. **不同 `last_probe`（新观测）**：
   - `ok` → `probe_fail_count=0, healthy=1`（原逻辑）
   - `fail` → `probe_fail_count=probe_fail_count+1, healthy=CASE WHEN probe_fail_count+1>=2 THEN 0 ELSE healthy END`（原逻辑）
   - 其它（skip/pending/refused）→ 仅刷新状态（原逻辑）

实现方式：把每个分支的 `UPDATE` 前置一个 `SELECT probe_last` 比较，相同则走「仅刷新」分支。为减少 SQL 往返，用单条 `SELECT` 取当前 `probe_last` 后在 Python 层比较分支。

### B. Worker 复用注册探针 + 首轮延迟（`src/runtime/agent.py`）

`_worker_probe_loop()`：把 `_time_mod.sleep(_PROBE_INTERVAL)` **移到循环体最前**（当前在最后），使首轮先等待一个周期再探测——与注册时 `_worker_probe_once()` 的即时结果间隔 ≥ `_PROBE_INTERVAL`，避免启动阶段连续两轮。

### C. Worker 心跳指纹去重（`src/runtime/agent.py` + `src/runtime/client.py`）

新增模块级 `_last_sent_probe_fingerprint: dict`（`engine_name → last_probe`，已成功发送的指纹）。`_run_loop` 心跳分支：
- `_collect_worker_probe_results()` 取当前快照后，**过滤掉指纹与 `_last_sent_probe_fingerprint` 相同的引擎**（即未变化的观测不重发）。
- 心跳 `status==ok` 后，把本次发送的指纹写回 `_last_sent_probe_fingerprint`。
- 心跳失败不更新指纹（下次重试仍可发送）。

`client.py` 的 `heartbeat()` 无需新增字段——`probe_results` 内已含 `last_probe`，服务端按它幂等；client 层只负责透传。

## 任务拆解

| # | 任务 | 提交 | 类型 |
|---|------|------|------|
| 1 | 写失败测试：服务端幂等去重（3 次重放只 +1 / 两条不同失败达 2 / 成功清零） | `test(#490): 服务端心跳探针幂等去重先写失败测试` | RED |
| 2 | 写失败测试：Worker 启动时序（首轮先 sleep `_PROBE_INTERVAL`） | `test(#490): Worker 后台探针首轮延迟先写失败测试` | RED |
| 3 | 实现服务端 `runtime.py` 幂等去重 | `feat(#490): 服务端心跳按 last_probe 幂等去重探针计数` | GREEN |
| 4 | 实现 Worker `agent.py` 首轮延迟 + 心跳指纹去重 | `feat(#490): Worker 复用注册探针并按指纹去重心跳观测` | GREEN |
| 5 | 回归测试 + 自审查 | — | VERIFY |

## 风险

- **服务端 `SELECT probe_last` 旧 Worker 兼容**：旧 Worker 不发 `last_probe` 时 `_plast` 回退为 `now`（每次不同）→ 走原递增逻辑，**不回归**（与旧行为一致）。
- **指纹去重导致服务端迟迟收不到更新**：仅当观测未变化才不发；一旦新探测（`last_probe` 变化）立即发送。心跳仍每周期发 metrics/logs，探针只是其中一个字段。
- **并发**：`_last_sent_probe_fingerprint` 用 `_worker_probe_lock` 保护（与 `_worker_probe_results` 同锁）。

## 影响面

- 仅探针观测计数路径；注册/claim/任务执行不动。
- 服务端 SQL：心跳每条 probe 增一次 `SELECT probe_last`（轻量，主键+name 索引）。
