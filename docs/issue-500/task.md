# Task: Issue #500 开发拆解（TDD）

- [ ] T1 红：`tests/test_agent_bwrap_noise_500.py` — 三调用点 × 三态测试 + sandbox 包导出/布局测试（先失败证明契约）
- [ ] T2 绿：`src/agent/sandbox.py` → `src/agent/sandbox/__init__.py`（git mv + `__all__` 导出）
- [ ] T3 绿：`src/agent/hermes_acp.py` 配置短路 + `_note_bwrap_fail_once`
- [ ] T4 绿：`src/agent/hwcloud_acp.py`、`src/agent/executor.py` 同型修改
- [ ] T5 回归：现有相关测试（test_agent_executor / test_hermes_* / test_hwcloud_acp / test_probe_acp_*）全通过
- [ ] T6 自审查（5 轴 + pre-pr 增量检查）→ push → PR（base=weekly-09）→ Issue 评论 → A2A QA
