# Issue #3201 任务拆解

- [x] T1 接管 active 判定自愈 + 忽略计数（`takeover.py`）
- [x] T2 建 task 出口守门：`_create_direct_task` / `_enqueue_pipeline_task`（`event_pipeline.py`）
- [x] T3 A2A 入口 409 `already_taken_over`（`webhook.py`）
- [x] T4 状态聚合锁定 running + 过滤 skipped_takeover（`pipeline_status.py`）
- [x] T5 列表/详情/finish 暴露已忽略 N 条（`mgmt/routes/takeover.py`、`takeover.py`）
- [x] T6 测试：webhook/direct/A2A/过期自愈/跨 Issue/聚合/finish 顺序与计数
- [x] T7（第 4 轮 R1）手动重派入口统一守门（`routes/events.py`）
- [x] T8（第 4 轮 R2）autopilot 入队前仓储级统一守门（`engine/scheduler.py`、`takeover.py`）
- [x] T9（第 4 轮 R3）A2A direct/remote 分支命中后返回 skipped 且不启动 agent（`api/a2a_dispatch.py`、`event_pipeline.py`）

验证命令：`pytest tests/test_takeover_dispatch_guard_3201.py tests/test_takeover_flow.py tests/test_takeover_event_guard.py -q`

## 验收对照（追加清理项）

| 项 | 结论 | 自检 |
|----|------|------|
| 全仓零调用点死函数 `assert_no_active_takeover` | 选 A：删除定义；显式派发入口（A2A / a2a-redo / issues-resume）继续统一走 `active_takeover_for`，常规 webhook / autopilot 派发走 `_create_direct_task` / `_enqueue_pipeline_task` 中的 `_skip_dispatch_for_takeover`，不新增第二套守门判断 | `git grep -n "assert_no_active_takeover" src tests` → 0 命中 |

## 第 4 轮审查 R1–R3 对照表（验收条款 → 代码位置 → 测试名）

| 验收条款 | 代码位置 | 测试名 |
|----------|----------|--------|
| R1 重派入口（`check_repo_access` 之后、任何建 task 分支之前）命中 active 接管 → 409 `already_taken_over`，不改事件状态、不建 task | `src/domains/runtime/routes/events.py:669`（`redispatch_event` 入口，单点覆盖 queued 复用直跑 / local_only 复用 / 快照重建 / 终态重建 / 无任务重触发全部分支） | `tests/test_takeover_dispatch_guard_3201.py::test_redispatch_blocked_under_active_takeover` |
| R1 `local_only=True` 分支同样拦截 | 同上（入口判定先于 `local_only` 分支，`events.py:786` 起的所有分支都在其后） | 同上（`local_only` 参数化断言） |
| R1 接管结束后同一调用正常重派（对照组） | 同上（守卫只拦 `status=active` 且未过期） | `test_redispatch_allowed_after_takeover_closed` |
| R2 autopilot 入队前判定；命中 → execution `skipped`、error「接管进行中，自动派发已拦截」、不建 task、不消耗执行次数。autopilot 无真实 Issue 维度（`_autopilot_issue_ref` 为纯字母统计合成键，与真实会话 `#<数字>` 不命中），故按**仓库**查 active 接管 | `src/domains/runtime/engine/takeover.py`（`active_takeover_in_repo`，仓储级 active 查询+过期自愈）；`scheduler.py`（`_skip_autopilot_for_takeover`）、`:503`（direct 分支，`_create_direct_task` 之前）、`:674`（remote 分支，`enqueue_task` 之前） | `test_autopilot_dispatch_skips_under_active_takeover`（真实 `#3201` 接管）、`test_autopilot_dispatch_allowed_after_takeover_closed` |
| R2 skipped 不得进入重试循环：`success` / `cancelled` / `skipped` 均为终止态，不按 `max_retry` 重跑、不新建 execution | `scheduler.py`（`_execute_task` 结果判定，`skipped` 与 `cancelled` 同属 break 分支） | `test_execute_task_stops_on_skipped_without_retry` |
| R3 A2A direct 分支 `_a2a_task_id` 为空 → 返回 `{"status":"skipped",...}` 且不调用 `start_direct_agent` | `src/api/a2a_dispatch.py:282` | `test_a2a_direct_skips_and_does_not_start_agent_under_takeover` |
| R3 remote / 广播分支 `_enqueue_pipeline_task` 返回空串 → 返回 skipped（非 ok） | `src/api/a2a_dispatch.py:238`（plugin 广播）、`:303`（legacy remote）；`event_pipeline.py:3636` 起所有提前返回改为 `""`，尾部 `return tid or ""` | `test_a2a_remote_returns_skipped_under_takeover` |
| R3 A2A 队列 drain：skipped 不得判失败、不得改事件为 failed | `event_pipeline.py:3375`（drain 仅在 `failed` 或 `seq` 为空时走 `_fail_event`；skipped 带 seq → 视为已处理） | `test_a2a_queue_drain_skips_without_failing_event` |

### 第 4 轮自检输出

```
$ git grep -n "active_takeover_for" src
src/api/pipeline_status.py:73-74
src/domains/runtime/engine/takeover.py:119,1106
src/domains/runtime/pipeline/event_pipeline.py:605-606
src/domains/runtime/routes/events.py:679,682            # R1
src/domains/runtime/routes/webhook.py:1826-1827

$ git grep -n "active_takeover_in_repo" src
src/domains/runtime/engine/takeover.py:155              # 仓储级 active 判定（R2）
src/domains/runtime/engine/scheduler.py:111            # autopilot 守门唯一调用点

$ git grep -n "enqueue_task(" src/domains/runtime/routes/events.py src/domains/runtime/engine/scheduler.py
events.py:885   # redispatch 入口守门（events.py:669）先于该分支
events.py:1383  # 同上
events.py:1997  # event conversation（人工对话，非自动派发，未下沉守门）
scheduler.py:678 # 紧邻上方 scheduler.py:674 的 _skip_autopilot_for_takeover（R2）

$ git grep -n "start_direct_agent" src/api/a2a_dispatch.py
src/api/a2a_dispatch.py:276  # 函数级 import
src/api/a2a_dispatch.py:282  # if not _a2a_task_id: return skipped（R3）
src/api/a2a_dispatch.py:290  # 仅在 task_id 非空分支内调用

$ python -m pytest tests/test_takeover_dispatch_guard_3201.py -q
17 passed
$ python -m pytest tests/test_takeover_dispatch_guard_3201.py tests/test_takeover_flow.py tests/test_takeover_event_guard.py tests/test_takeover_dispatch.py tests/test_takeover_mark.py tests/runtime/test_issue_1770_a2a_dispatch_guard.py -q
72 passed
```

## 不在本单范围（观察项，未改动）

`active_takeover_for` 读路径写库与 N+1、`#N` 归一化、`issues.py get_events(limit=30)` 窗口、`_skip_dispatch_for_takeover` 对终态事件的无条件回写、`skipped_takeover` 与 `EVENT_STATUS_MAP` 的关系——均未在本 PR 内改动。