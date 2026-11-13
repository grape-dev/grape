# Tasks: Issue #1811

- [x] 代码实勘：定位 `_execute_autopilot_agent` 第 6 步 env_extra 缺口
- [x] 编写 spec.md / plan.md
- [x] 新增 `tests/test_issue_1811_autopilot_bot_agent_env.py`（RED）
- [x] scheduler 第 6 步延迟 import 并复用 `_inject_bot_agent_env`（GREEN）
- [x] BOT_USERNAME 空值输出含 exec id + repo 的 WARNING
- [x] 回归 #573 注入用例与 autopilot 引擎过滤用例
- [x] 自审查（代码质量 + 开源前置增量检查）
- [x] 建 PR #1124 + Issue 评论 + A2A 通知 QA（seq=63156）