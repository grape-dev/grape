# Issue #520 任务拆解

## T1 服务层 bound 分支
- [ ] claim() 增加 expected_task_id/expected_event_seq 参数 + 原子校验分支（含 pool_engine_ids 守卫）
- [ ] claim_by_tag() 同上（scope SQL 追加 expected 过滤；跳过 FIFO 跳过/健康检查循环）
- [ ] 新增 lookup_task_by_event_seq()

## T2 路由层四件套
- [ ] RuntimeClaimBody 扩展 expected 成对字段 + model_validator 成对校验
- [ ] _require_runtime_owned 归属校验助手
- [ ] POST /runtimes/{runtime_id}/tasks/claim-bound
- [ ] POST /runtimes/{runtime_id}/tasks/claim-by-tag-bound/{tag}
- [ ] GET /runtimes/{runtime_id}/tasks/claim-capabilities（bound_claim + task_lookup 两个能力位）
- [ ] GET /runtimes/{runtime_id}/tasks/lookup?expected_event_seq=N
- [ ] 旧 claim/claim-by-tag 路由透传 expected（成对时）

## T3 测试
- [ ] tests/test_issue_520_bound_claim.py：验收用例 1-12 全覆盖
- [ ] 既有 FIFO 回归：test_runtime_protocol.py 等全量通过

## T4 交付
- [ ] 自审查（5 轴 + pre-pr 增量检查）
- [ ] 提交 / 推送 feat/issue-520，建 PR（base=weekly-08-mys），Issue 评论告知，A2A 通知 QA
