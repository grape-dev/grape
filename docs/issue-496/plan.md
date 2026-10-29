# Plan: Issue #496

## 派发级自动重试设计（pr_merged_sre_retry）

```
run_pipeline (pull_request_merged → sre)
  └─ get_scene_pool_engine_ids(...) is None   # #259 方案A 命中点（4/7 failed seq 根因）
       ├─ _create_failed_task(...)            # 保留：重派参数快照
       ├─ schedule_pr_merged_sre_retry(...)   # 新增：仅 pull_request_merged+sre
       │    └─ Timer 退避链 _run_attempt(n)
       │         ├─ _pick_ready_engine: pool 非 None 且 pick_engine 有引擎
       │         ├─ 就绪 → _redispatch:
         │         │    _claim_for_redispatch（仍 failed 且无活跃任务）
         │         │    reset_event_terminal（failed→running + 守卫重抢）
         │         │    _dispatch_picked（广播 / 远程定向 enqueue / 直连 _create_direct_task+start_direct_agent）
         │         │    异常 → _fail_event 收口（不僵尸）
         │         └─ 未就绪 → 续排 / 耗尽 → result 追加终止说明
       └─ _fail_event(seq, 原文案 + "；已安排自动重试（引擎恢复后自动重派，Issue #496）")
```

- 幂等：进程内 `_scheduled` 集合按 seq 去重；重派前 DB 校验事件状态 + 活跃任务数。
- 可观测：`[496-retry]` 日志前缀贯穿 scheduled/redispatched/exhausted。
- 隔离：新模块不改动 #259 判定、#403 transient_retry、人工重派端点行为。

## 测试策略（测试先行）
tests/test_issue_496_pr_merged_sre_retry.py：
T1 耗尽收口（failed 保持 + result 终止说明）| T2 恢复重派（running + enqueue 参数）| T3 触发门槛（仅 merged+sre）| T4 接管跳过 | T5 run_pipeline 集成（offline fail + 调度标记）| T6 调度幂等。
