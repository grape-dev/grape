# Task: Issue #4231 — 任务拆解与执行记录

| # | 任务 | 状态 |
|---|------|------|
| 1 | 新增 `src/domains/mgmt/user/repo_naming.py` 公共校验工具 | done |
| 2 | `repos.py` check-name 前置校验（token 解析前，非法 400 零上游调用） | done |
| 3 | `repos.py` create 复用同一工具，删两处内联正则 | done |
| 4 | 后端 502 去「检查失败:」前缀（只回裸原因） | done |
| 5 | 前端 TemplatesView 模板创建流程补单层前缀 | done |
| 6 | 新增/更新回归测试（4231 校验 + 3563 断言翻转） | done |
| 7 | 全量相关测试 + 自审查 + pre-pr 增量检查 | done |

## 执行记录（arch-weekly-11-ljt）
- 本轮为跨流水线重落位：旧基线（arch-weekly-11-yaw）PR #2196 未合入，按 Spec v3 在 `arch-weekly-11-ljt`
  重新实现；开发分支 `feat/issue-4231-ljt`（避免改写旧 PR #2196 的 `feat/issue-4231` 分支）。
- 验证命令：`python3 -m pytest tests/mgmt/test_issue_4231_repo_name_validation.py
  tests/mgmt/test_issue_3563_reauth_401_split.py tests/runtime/test_issue_3580_create_space.py
  tests/mgmt/test_oauth_reauth_scenarios.py -q`。
