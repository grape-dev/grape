# Spec: Issue #451 — 移除系统占位回帖「[AGENT Agent] 任务已完成（agent agent）。」

> 目标分支：`weekly-08-lyz`

## 目标
移除 Agent 处理完成后系统自动发布的无意义占位评论 `[AGENT Agent] 任务已完成（agent agent）。`。
Agent 已发结论评论时系统不再补发；未发时以真实 Agent 身份兜底补发一条。

## 根因链路（三处叠加）
1. `src/token_resolver.py` `_post_comment_via_provider()` L1084：硬编码 `agent_type="agent"` 传给 `_post_result_to_issue`，丢失真实 Agent 身份。
2. `src/runtime/service.py` `complete_task()` L2170：兜底文案 `任务已完成（{_agent_type} agent）。` 经 `_post_result_to_issue` 二次处理后占位串被覆盖为 `（agent agent）`。
3. `src/api/event_pipeline.py` `_post_result_to_issue()` L3963-3965：conclusion 提取失败时以 `任务已完成（{agent_type} agent）。` 覆盖有效 result_text；`agent_type="agent"` 时产出 `（agent agent）`。

## 验收标准
- 新处理 Issue 不再出现 `[AGENT Agent] 任务已完成（agent agent）。` 评论
- Agent 自行发布的结论评论正常保留
- Agent 未发评论时兜底补发生效且显示真实身份（如 `[VOD Agent]`）
- `_post_result_to_issue` 去重（方案 B）在 Agent 评论已存在时正确跳过
- 所有现有测试通过，新增测试覆盖新逻辑
