# Plan: Issue #451

## 方案
沿回帖链路（complete_task → _post_comment_via_provider → _post_result_to_issue）修正 agent_type 传递、去重时序与兜底文案。

## 改动清单

### 1. src/token_resolver.py — agent_type 透传
- `_post_comment_via_provider()`：新增 `agent_type: str = "agent"` 参数，透传给 `_post_result_to_issue`（替换硬编码 `"agent"`）。
- `TokenActionHandler.post_issue_comment()` 抽象签名 + 4 个实现类（Session/RepoBot/GlobalBot/User）：新增 `agent_type: str = "agent"` 参数，传递到 `_post_comment_via_provider`。

### 2. src/runtime/service.py — 兜底文案 + 传参
- `complete_task()` L2170：调用 `post_issue_comment` 时传入 `_agent_type`；兜底文案从 `任务已完成（{_agent_type} agent）。` 简化为 `任务已完成。`（前缀已携带身份）。
- `fail_task()` L2720：调用 `post_issue_comment` 时传入 `task.get("agent_type") or "agent"`。

### 3. src/api/event_pipeline.py — 去重前置 + result_text 保留 + 兜底简化
- `_post_result_to_issue()` L3963-3965：conclusion 提取失败时，若 result_text 非空且非 JSON（纯文本），保留为 conclusion；否则用简洁兜底 `任务已完成。`（去掉 `（{agent_type} agent）` 占位串）。
- 去重逻辑（GET 评论列表 → 匹配 agent_type tag → 跳过）已存在且正确，无需改动；修复 agent_type 透传后匹配精度提升。

### 4. tests/ — 回归测试
- 修复 `test_webhook_echo_and_rollback.py::TestPlanBMarkerAdded` mock（GET 返回空列表）。
- 新增 `test_issue_451_placeholder_comment.py` 覆盖三条链路：
  - agent_type 透传（_post_comment_via_provider 不再硬编码 "agent"）
  - conclusion 提取失败时保留有效 result_text
  - 兜底文案不含 "agent agent" / "AGENT Agent"
  - Agent 已发评论时去重跳过

## 风险
- 去重前置增加一次评论列表查询：已有逻辑，无新增开销。
- 兜底文案变更影响旧测试断言：同步修复 test_webhook_echo_and_rollback。
