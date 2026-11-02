# Task: Issue #627（方案 C：发布子单，复用现有子任务机制）

1. [x] 基线确认：feat/issue-627 分支就绪（基于 local-liufei，快进同步到 origin/local-liufei 最新），git 身份已配置。
2. [x] 代码线索考察：production_release 旁路实现（issues_center create_production_release / _dispatch_production_release_sre /
   _fill_production_release_result / session_store 存储层 / webhook payload 透传 / runtime.service 回填钩子）、
   现有子任务机制（issue 表 is_subtask_mode/parent_issue_number/parent_issue_id、`## 父任务` 识别、ensure_issue_task、
   a2a_endpoint 标准派发、complete_task 子任务完成回帖）、issue_opened 路由（vod + 智能推荐）。
3. [x] 文档：docs/issue-627/plan.md 更新为方案 C（发布子单）。
4. [ ] 红：tests/test_issue_627_production_release.py 重写为方案 C 契约（拒绝链 + 创建子单 + 唯一性 + 查询 + 回填三分支 + 状态机不变 + PR #799 修复项保留）。
5. [ ] 绿：
   - db_setup：删除 production_release 表 + 索引；issue 表补 release_candidate_sha/release_target_env/release_evidence_ref/
     release_approved_by/release_approved_at/release_result/release_failure_reason 列。
   - session_store：删除 production_release 存储函数；新增 list_release_subissues。
   - issues_center：create_release_subissue（校验链 + 平台建单 + 建档 + stage=sre + 派发 sre）+ 回填三分支 + is_release_subissue。
   - routes/issues：POST/GET /production-release 改调发布子单实现（路径不变）。
   - runtime/service：回填钩子改按 task.issue_id 反查子单。
   - webhook：移除 production_release payload 透传。
   - event_pipeline：issue_opened 发布子单跳过 vod（webhook 回声防护）。
6. [ ] 回归：627 测试全绿；test_state/test_tasks_lifecycle/test_issues_api/openapi 等相关套件通过（pre-existing failures 与基线一致）。
7. [ ] 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base=local-liufei）→ Issue 评论 → A2A QA → tasks complete。