# Issue #679 方案设计（plan）

## 改动点（三处 + 测试）

### 1. `src/api/event_pipeline.py` — `_post_result_to_issue`（issue 回帖主路径）

`if not _conclusion:` 的 else 分支（原 `_conclusion = "任务已完成。"`）改为记录日志并 `return`，即无纯文本正文可回填时直接跳过发帖。标记与防重复逻辑不动。

### 2. `src/api/event_pipeline.py` — `_post_result_to_pr`（PR 订阅回帖）

原 `if not _conclusion: _conclusion = f"PR 审查任务已完成（{agent_type} agent）。"` 改为：
- 与 issue 主路径对齐：先尝试回填有效纯文本正文（非空、非 `{`/`[` 开头，剥 marker，截 3000）；
- 仍无内容则日志 + `return` 跳过发帖。
纯文本回填是验收第 2 条（纯文本正文正常发布）在 PR 路径的等价实现，同时保持 `tests/test_issue_453_outbound_ledger.py` 纯文本回帖入账链路不变。

### 3. `src/runtime/service.py` — `complete_task`（handler 发帖链）

原 `_h.post_issue_comment(_repo_key, _issue_num, _result_text or "任务已完成。", _r, _agent_type)` 改为 `if _result_text:` 才调用；空时日志说明并跳过。`service.py:2018` 内部补偿状态描述不动。

### 4. 测试调整

- 实际依赖「兜底文案必发」的断言在 `tests/test_issue_451_placeholder_comment.py`（`test_issue_372_deep_analysis.py` 核对后不含此类断言，无需改动）：
  - `test_empty_result_uses_clean_fallback` → `test_empty_result_skips_comment`（断言无 POST）
  - `test_json_without_conclusion_uses_fallback` → `test_json_without_conclusion_skips_comment`（断言无 POST）
  - `TestCompleteTaskFallback` 增加 `任务已完成。` 不再出现在 `complete_task` 源码的断言
- 新增 `TestIssue679SkipEmptyPosting`：PR 路径空/纯 JSON 跳过、纯文本正常发、issue 路径 conclusion 正常发、service 源码级空跳过断言。

## 影响面评估

- 不改任何标记（`<!-- grape-auto-comment -->`）、防重复（agent 已发评论跳过）、台账（outbound ledger）逻辑。
- 空 result_text 场景此前必发一条占位评论，现在不发——对外仅表现为冗余评论消失。
- `test_issue_453` 的 PR 回帖纯文本入账链路保持通过（纯文本回填后照常 POST + 入账）。

## 回归基线说明

以下失败在未改动基线（stash 后）同样失败，属历史遗留，与本次无关：
- `test_webhook_echo_and_rollback.py::test_plain_bot_issue_opened_still_skipped`
- `test_issue_372_deep_analysis.py::TestGrapeAPICommentMarker::test_create_issue_comment_appends_marker`
- `tests/test_a2a_queue.py` 环境相关 10 例
