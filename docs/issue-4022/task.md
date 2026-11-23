# Task: Issue #4022 — pending-review 置位后首读即见新状态（read-your-writes）

- [x] T1 定位根因：列表 5s 结果缓存无写后失效；详情 `task_status` 走聚合 map 与 `status get` 口径分裂。
- [x] T2 读侧一致性：`_issue_status_by_ids` + `_cached_list_is_current`，`_cached_repo_list` 命中核对、漂移/异常即重算（写路径零接入）。
- [x] T3 口径统一：`_read_issue_status_by_id`；`_assemble_issue` 按内部主键取 `task_status` 并新增 `status` 别名；`_task_status_map` 收窄 + 确定性；详情 pipeline 业务状态同源。
- [x] T4 回归测试 `tests/runtime/test_issue_4022_read_your_writes.py`：缓存预热后置位即读、连续多次不抖动、任意写路径即时可见、list/detail/status get 口径一致。
- [x] T5 文档：`docs/issue-4022/spec.md` / `plan.md` / `task.md`。
- [x] T6 自审查（5 轴 + 开源前置增量检查）与相关测试回归。
- [ ] T7 提交、推送 `feat/issue-4022`、创建 PR（base `weekly-mys`）、Issue 评论、A2A QA、tasks complete。