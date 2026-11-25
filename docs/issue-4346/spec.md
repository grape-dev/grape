# Spec：Issue #4346 — session/new 建会话超时可观测、换引擎重试与会话层熔断

## 目标

为引擎 `session/new`（建会话）链路补齐四层防护，消除「单次建会话超时即整条流水线 failed」的单点失败面：

1. **超时可区分**：`ENGINE_UNREACHABLE`（引擎不可达）/ `SESSION_CREATE_BLOCKED`（TCP 可达、请求已发出但 100s 无响应）/ `ENGINE_MISCONFIGURED`（引擎返回 `No LLM provider configured` 等配置类 500）。
2. **换引擎重试**：首次 `SESSION_CREATE_BLOCKED` 后至少重试 1 次，且**必须换引擎**（同引擎进入 session 冷却期，认领闸门拒绝），重试留痕；仍失败判 failed，不原地挂起。
3. **会话层熔断**：连续 N 次（默认 3，可配）`session/new` 超时 → 引擎进入 session 熔断（默认 10min），从派发/认领候选中剔除；窗口结束半开，放行 1 次探测，成功恢复、失败续熔断。
4. **LLM provider 前置校验**：引擎注册/启用期校验 provider 配置，缺失即拒绝健康注册并标记 `llm_provider_ok=0`，存量引擎不进调度池，派发链路不再出现该 500。

## 观测字段（SESSION_CREATE_BLOCKED 事件）

任务 `agent_tasks.errors` 结构化条目 + 事件 `payload._session_fail` 必含：

| 字段 | 语义 | 取不到 |
|------|------|--------|
| `engine_id` | 引擎 runtime id | `null` |
| `queue_depth` | 该引擎排队/在跑任务数 | `null` |
| `disk_avail_bytes` | 宿主 `/` 可用字节 | `null` |

字段必须存在（值可为 null），不破坏既有消费者。

## 范围

- In：`session/new` 失败分类与 payload、换引擎重试、session 层熔断/半开、健康分透出 session 指标、注册/启用期 provider 校验。
- Out：磁盘写满根因（#4204/#4320）、worktree 基线（#4206）、重派路径缺陷、#423 解析侧逻辑改动（仅加回归守护）。

## 验收

- 三类超时可区分，`SESSION_CREATE_BLOCKED` 事件含 `engine_id/queue_depth/disk_avail_bytes`。
- 超时自动换引擎重试 ≥1 次且留痕；仍失败判 failed 并回落下一跳。
- 连续 N 次超时引擎熔断并剔除调度候选；半开探测可恢复。
- 健康分可读 session 层失败计数与熔断状态。
- 无 LLM provider 引擎在注册/启用期被拦截，派发链路不再出现该 500。
- #423 解析侧行为不被重新引入（回归用例守护）；现有测试全通过 + 新增测试覆盖。