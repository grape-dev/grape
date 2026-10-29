# Task: Issue #476 — 任务拆解

## 切片 1（红）：listener 裸调用测试 + update_event_status 防御测试
- [ ] tests/test_issue_476_failed_result.py：listener failed/exception 两路径 + _fail_event diag + 防御回填 + list_events 字段 + gap 口径（先红）
- 提交：`test(#476): listener 失败路径与接口字段先写失败测试`

## 切片 2（绿）：失败收口与诊断上下文
- [ ] event_pipeline._fail_event 扩展 diag（result code 前缀 + payload _fail_diag 幂等合并）
- [ ] event_store.update_event_status 终态 failed 防御回填（result/exit_code/completed_at）
- 提交：`feat(#476): _fail_event 诊断上下文 + update_event_status 非空 result 防御`

## 切片 3（绿）：webhook listener 收口 + ref_seq 注入
- [ ] `_a2a_source_event_seq` 助手 + a2a_endpoint payload/evt 注入 ref_seq
- [ ] `_on_handoff_dispatch` 两处裸调用 → `_fail_event`（含错误详情 + diag）
- 提交：`feat(#476): agent/handoff listener 失败收口到 _fail_event（含 ref_seq）`

## 切片 4（绿）：列表接口字段 + 前端
- [ ] list_events 补 result/issue_id/task_id（LEFT JOIN agent_tasks）
- [ ] events.js 失败诊断渲染；pipeline.js 回退跳转修复；observability.js 摘要卡
- 提交：`feat(#476): 事件列表补 result/issue_id/task_id 与前端跳转/诊断展示`

## 切片 5（绿）：观测口径 + 存量标记 + 记录
- [ ] src/observability/failed_gap.py + /api/obs/events/failed-result-gap
- [ ] scripts/backfill_legacy_failed_events.py（--dry-run/--apply）
- [ ] docs/issue-476/record-main-prod.md（main 已确认；生产待复核）
- 提交：`feat(#476): 跨仓 failed 空 result 占比口径与存量 legacy 标记`

## 收尾
- [ ] 全量回归（相关测试文件）
- [ ] 自审查（code-review-and-quality + pre-pr-release-check）
- [ ] 建 PR（base=weekly-09）+ Issue 评论 + A2A QA + tasks complete