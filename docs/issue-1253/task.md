# Task: Issue #1253 — Hermes 静默杀死阈值 300s → 600s

- [ ] 红：更新 `tests/test_agent_silent_timeouts.py` 默认/非法回退断言 300→600（先变红）
- [ ] 绿：`src/agent/hermes_acp.py` 默认值 + 异常回退 300→600，注释同步
- [ ] 绿：`src/runtime/agent.py` `_DEFAULT_SILENT_WINDOW` 300→600 + #378 注释同步
- [ ] 绿：`src/agent/executor.py` 默认 `min(300, max_exec)` → `min(600, max_exec)` + 注释/总账示例同步
- [ ] 单测全绿：`python3 -m pytest tests/test_agent_silent_timeouts.py`（含 coding/qa 回归）
- [ ] 自审查（code-review-and-quality + pre-pr-release-check）
- [ ] 建 PR → Issue 评论 → A2A QA