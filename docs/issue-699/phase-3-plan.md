# Phase 3 Plan（Issue #699）

## 迁移内容与位置

| 符号 | 迁移前（event_pipeline.py） | 迁移后 |
|------|------------------------------|--------|
| `_DIRECT_POOL` | 模块头（原 L13-16） | direct_executor.py |
| `_DIRECT_FUTURES`/`_DIRECT_FUTURES_LOCK` | 模块头（原 L18-19） | direct_executor.py（event_pipeline re-export 供 cancel_direct_task） |
| `start_direct_agent` | 原 L348-356 | direct_executor.py |
| `_execute_agent` | 原 L3668-3704 | direct_executor.py（+函数级 import `_cleanup_direct_workdir/_update_direct_task`） |
| `_execute_agent_inner` | 原 L3816-4550 | direct_executor.py（+函数级 import 20 个 pipeline helper） |

## 5 处调用点改 enqueue_task（对齐设计 §6.5 行号表）

| 调用点（现函数） | 改法 |
|------------------|------|
| `_dispatch_pr_subscription_extra` | direct 分支折叠进同函数 else 分支的 `_enqueue_pipeline_task`（参数一致） |
| `_dispatch_pr_subscription_redispatch` | 同上 |
| `_dispatch_subscription_agent` | direct 分支折叠进 `_enqueue_pipeline_task(..., user_id=_sub_uid)` |
| `_dispatch_scene_recommend_redispatch` | direct 分支折叠进 `_enqueue_pipeline_task(..., is_scene_recommend=False)` |
| `run_pipeline` 主分派 | if/elif 两分支合并为单一 `_enqueue_pipeline_task`（条件取并集） |

## 认领闭环（TC-3.4）

1. `_enqueue_pipeline_task`：legacy 模式 + runtime_id=0 时回退本机 direct 引擎
   （`_local_direct_engine_ids()[0]`），保持「direct 部署无绑定引擎也能落到本机」。
2. `service.claim` 增加 `deterministic_only=False` 参数（默认不变）；True 时只认领
   `runtime_id=?` 定向任务，跳过 legacy 池兜底。
3. `local_claimer_loop`：每轮先 `claim(rid, deterministic_only=True)`（绑定本机的
   queued 任务，跳过 backstop），再走原 `claim_by_tag` 广播链（backstop 不变）。
4. `_execute_claimed_locally` → `start_direct_agent(_execute_agent, ...)`（import 改
   direct_executor，TC-3.3）。

## 提交切分（原子）

1. `docs: #699 add Phase 3 spec, plan and task breakdown`
2. `refactor: #699 move _execute_agent series to engine/direct_executor.py`（纯迁移，行为等价）
3. `feat: #699 replace 5 start_direct_agent call sites with enqueue_task`（含 local_claimer 确定性认领）
4. `test: #699 add Phase 3 acceptance tests (TC-3.1~TC-3.5)`

## 风险

- 认领延迟（≤3s + 广播 backstop 15s）替代原 direct 即时执行 → 设计已接受（§6.5 注意）。
- legacy+engine_id=0 回退本机引擎，若无本机引擎则事件失败（原路径同样依赖本机引擎存在）。
- 双跑安全：claim 原子 UPDATE（status='queued' 条件）+ 串行槽 NOT EXISTS，沿用现状。
