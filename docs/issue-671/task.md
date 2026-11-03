# Task Breakdown: Issue #671

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1 | A2A 锚点块改注册表优先 + 未命中标记 | src/api/routes/webhook.py | done |
| 2 | 回退校验 helper（token 同源解析 + VCS 探测 + verdict） | src/api/routes/webhook.py | done |
| 3 | 幂等补录 helper（INSERT OR IGNORE 最小行） | src/api/routes/webhook.py | done |
| 4 | 鉴权后回退接入 + 细分 400 错误语义 | src/api/routes/webhook.py | done |
| 5 | managed_repo_exists helper | src/user/db.py | done |
| 6 | create_new_repo 托管失败兜底注册 | src/api/routes/repos.py | done |
| 7 | repair_host 未注册仓 VCS 自证放行 | src/api/routes/repos.py | done |
| 8 | A2A runbook 400 降级路径 | help/api/12-a2a.md | done |
| 9 | 测试（回退放行/幂等补录/错误语义/未认证不外呼） | tests/test_issue_671_a2a_repo_fallback.py | done |
