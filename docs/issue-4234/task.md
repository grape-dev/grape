# Task Breakdown: Issue #4234

- [x] T1 抽取 STATUS_VIEW_MAP（api/labelKey/fallback/strip），落地 IssuesView.vue
- [x] T2 TASK_STATUS_TO_API / BOARD_CURSOR_STATUSES 改由映射表派生（值不变）
- [x] T3 STATUS_LABELS 与 statusInfo 展示名同源化（列头=卡片状态点）
- [x] T4 detailCardHtml 状态点改走 mapStatusL 派生口径
- [x] T5 新增 tests/js/test_issue_4234_status_wording_map.js（5 用例全绿）
- [x] T6 回归：看板列分屏/回合化/视图切换/接管徽标测试通过；statuses= 口径断言不变
- [x] T7 文档：spec.md / plan.md / task.md
