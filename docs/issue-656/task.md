# Task — Issue #656

- [x] T1 写 docs/issue-656/{spec,plan,task}.md（本目录）
- [x] T2 issues_center.py 新增 auto_finalize_issue_on_last_node + _close_vcs_issue_auto
- [x] T3 runtime/service.py complete_task 末节点回调接入（替换 pending_review 硬编码）
- [x] T4 event_pipeline.py 本地 exec 完成回调接入（sre 硬编码泛化）
- [x] T5 confirm-done 人工确认触发条件标注（routes description + help/api/11-issues.md）
- [x] T6 新增 tests/test_issue_656_auto_finalize.py（6 场景）并全绿
- [x] T7 pre-pr 检查（黑名单/敏感增量扫描/英文 commit）→ push → PR → 评论 → A2A QA
