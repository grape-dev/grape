# Task: Issue #568 任务拆解

| # | 任务 | 状态 |
|---|------|------|
| 1 | webhook `_scene_node_check` 节点来源统一到 `get_pipeline_config` | done |
| 2 | webhook `_cross_pipeline_handoff`：存在性校验 + `_refresh_issue_scene_after_handoff` 刷新有效 scene_id | done |
| 3 | scenes `get_scene_detail` 会话绑定二级解析 `_resolve_session_bound_scene` | done |
| 4 | 测试 `tests/test_issue_568_scene_visibility_unification.py`（两路一致/会话绑定兜底/移交刷新/权限不放松） | done |
| 5 | docs/issue-568 spec/plan/task + runbook 降级路径指引 | done |
| 6 | 回归：现有相关测试（a2a_scene_check / issue_534 / issue_533 等） | done |
