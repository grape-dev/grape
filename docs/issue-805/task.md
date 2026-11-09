# Task Breakdown: Issue #805

- [x] T1 db_setup 幂等补列 issue.created_by
- [x] T2 issues_center: ensure_issue_task creator 参数 + 幂等空补写
- [x] T3 issues_center: _query_tasks 透出 creator
- [x] T4 issues_center: _build_creator_filter
- [x] T5 event_pipeline: 建档分支提取创建人并传入
- [x] T6 routes/issues: /mine 与仓库列表 creators 参数 + creator 透出
- [x] T7 routes/issues: 详情（本地/VCS 兜底）透出 creator
- [x] T8 scripts/backfill_issue_creator.py
- [x] T9 static/js/pages/issues.js 展示/筛选切换
- [x] T10 单测（写 creator / 幂等不覆盖 / 过滤）
- [x] T11 回归现有相关测试
