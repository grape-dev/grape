# Task: Issue #418 — 执行记录

| # | 任务 | 状态 |
|---|------|------|
| T1 | `_fail_event` 补 exit_code 形参 + 幂等语义确认 | ✅ |
| T2 | webhook.py 4 处早退 + scene-all-offline 兜底改走 `_fail_event` | ✅ |
| T3 | runtime/service.py `_fail_task_pool_*` 补 agent_tasks.exit_code（#369 收口） | ✅ |
| T4 | 新增 tests/test_issue_418_a2a_fail_event.py（6 用例） | ✅ |
| T5 | 回归：a2a_queue/a2a_scene_check/issue_368/issue_253/issue_369/event_* 全通过；基线既有失败（test_a2a_redo×4、test_pipeline×1、webhook_echo×1、agent_event_subscription×1）stash 对照确认与本次无关 | ✅ |
| T6 | 自审查 5 轴 + pre-pr 增量检查（黑名单拦截 + 敏感信息增量扫描） | ✅ |
| T7 | commit + push + PR + Issue 评论 + A2A QA | ✅ |

验收核对（对应 Spec Acceptance Criteria）：
- pick_engine=None / auto-select 空集 / auto-select 异常 / try 兜底：事件三字段完整 ✅
- 回归 broadcast/direct 派发（test_a2a_queue T1/T2）✅
- 同 seq 重复早退终态幂等 ✅
- #369 任务侧判死回写完整（事件三字段 + 任务 exit_code=1）✅
- 新增 6 用例通过 ✅
