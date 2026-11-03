# Phase 3 Spec（Issue #699）：_execute_agent 迁移 direct_executor

## 目标

把 `_execute_agent` 系列（本机 direct 执行路径）从 Event 维度核心文件
`src/domains/runtime/pipeline/event_pipeline.py`（4000+ 行）迁出到 Worker 维度模块
`src/domains/runtime/engine/direct_executor.py`，并将 event_pipeline 中 5 处
`start_direct_agent` 即时执行调用点改为 `enqueue_task` 入队，为 Phase 4 四进程拆分
（Event 进程不再持有执行器）铺路。

设计依据：docs/weekly-09/grape-unified-implementation-design.md §6。

## 范围

1. 新建 `src/domains/runtime/engine/direct_executor.py`，迁移：
   - `_DIRECT_POOL`（有界线程池，RUNTIME_MAX_CONCURRENT_TASKS，默认 20）
   - `_DIRECT_FUTURES` / `_DIRECT_FUTURES_LOCK`（sweeper 取消用）
   - `start_direct_agent`（提交到有界池）
   - `_execute_agent`（顶层 try/except 兜底 + finally 清理）
   - `_execute_agent_inner`（prompt 复用/物化 → stream_devops_agent → 结果回写）
2. event_pipeline.py 5 处 direct 调用点改 `enqueue_task` 入队
   （`_dispatch_pr_subscription_extra` / `_dispatch_pr_subscription_redispatch` /
   `_dispatch_subscription_agent` / `_dispatch_scene_recommend_redispatch` /
   `run_pipeline` 主分派）。
3. `local_claimer` import 改从 direct_executor；并补确定性认领
   （direct 任务入队后由本机引擎 ≤3s 认领，替代原即时执行）。
4. `cancel_direct_task` / sweeper 取消链路改读 direct_executor 的 `_DIRECT_FUTURES`。

## 非目标

- 四进程拆分、路由/定时任务分进程（Phase 4）。
- local_claimer 广播认领检查链改动（仅新增确定性认领入口，复用 service.claim）。
- 历史遗留失败测试修复（test_local_direct_fallback UT-01~08 引用早已移除的
  `_pool_only_local_direct`，属 Phase 3 之前的既有失败，不在本阶段范围）。

## 验收（TC-3.1~TC-3.5，设计 §验证用例）

- TC-3.1 direct_executor.py 含 `_DIRECT_POOL/_execute_agent/_execute_agent_inner/start_direct_agent`。
- TC-3.2 event_pipeline.py 无 `start_direct_agent` 调用，5 处改 `enqueue_task`（经 `_enqueue_pipeline_task`）。
- TC-3.3 local_claimer.py 从 `src.domains.runtime.engine.direct_executor` import。
- TC-3.4 webhook → enqueue(queued) → local_claimer 认领(≤3s) → `_execute_agent` 执行 → completed。
- TC-3.5 `sweep_stuck_events_once` 能取消 direct_executor `_DIRECT_FUTURES` 中排队的 Future。

## 关键设计决策

- **行为等价迁移**：`_execute_agent` 系列原样迁出，pipeline 侧 helper（prompt 物化/
  回帖/会话同步等）以函数级延迟 import 引用，避免 engine↔pipeline 模块级环
  （scripts/check-arch.py R5 只查模块级，函数级 deferred 不受限）。
- **direct → queue 的认领闭环**：入队任务 `status=queued`；plugin 广播任务由既有
  `claim_by_tag` 链认领；确定性绑定本机引擎的任务由 local_claimer 新增的
  `claim(runtime_id, deterministic_only=True)` 认领（复用 service.claim 的串行槽/
  回收/原子抢单逻辑，跳过 legacy 池兜底，不改广播检查链）。
- **确定性认领不进 backstop 延迟**：任务已绑定本机引擎，远端 worker 不会认领，
  无需让位等待（与设计 §6.5「≈3s 认领延迟」一致）。
- **兼容层**：`src/api/event_pipeline.py` 旧 import 路径继续 re-export direct_executor
  名字；event_pipeline 模块级 re-export `_DIRECT_FUTURES` 供 `cancel_direct_task`。
