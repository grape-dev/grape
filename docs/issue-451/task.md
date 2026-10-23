# Task Breakdown: Issue #451

## T1: token_resolver.py — agent_type 透传
- [x] `_post_comment_via_provider` 加 `agent_type` 参数
- [x] 4 个 Handler `post_issue_comment` 加 `agent_type` 参数
- [x] 基类抽象签名更新

## T2: runtime/service.py — 兜底文案 + 传参
- [x] complete_task L2170 传 `_agent_type` + 简化兜底
- [x] fail_task L2720 传 agent_type

## T3: event_pipeline.py — conclusion 保留 + 兜底简化
- [x] _post_result_to_issue conclusion 提取失败时保留有效 result_text
- [x] 兜底文案改为 `任务已完成。`

## T4: 测试
- [x] 修复 test_webhook_echo_and_rollback mock
- [x] 新增 test_issue_451_placeholder_comment.py
- [x] 全量回归通过
