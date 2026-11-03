# Issue #754 Task Breakdown

- [x] T1 merge 门禁：`_scan_pull_conflict_markers` + `merge_pull` 硬阻断 + 留痕
- [x] T2 PR 评论 405 兜底：`create_pull_comment` GitCode 通道降级 `/issues/{n}/comments`
- [x] T3 内部路径兜底：`_post_result_to_pr` 405 降级
- [x] T4 测试：merge 阻断/回归/fail-open、405 兜底两路径
- [x] T5 文档：spec/plan/task
