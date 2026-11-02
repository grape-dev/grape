# Issue #684 任务拆解

| # | 任务 | 文件 | 验证 | 状态 |
|---|------|------|------|------|
| 1 | 统一安全 JSON 序列化出口 SafeJSONResponse | src/api/json_safe.py | tests/test_json_safe.py | done |
| 2 | app 注册 default_response_class | src/api/main.py | 既有 api 测试全绿 | done |
| 3 | milestones state 归一化 + 端点描述 | src/api/git_repo_api.py | tests/test_git_repo_api.py 增补 | done |
| 4 | API 文档口径注明 | docs/openapi/10-git-repos.md | 文档评审 | done |
| 5 | 覆盖测试（控制字符/孤立代理项/milestones） | tests/test_json_safe.py, tests/test_git_repo_api.py | pytest 通过 | done |
| 6 | 自审查 + pre-pr 检查 + 提交/PR/A2A | — | 走查表 | done |

每步为原子提交（全英文 commit message）。
