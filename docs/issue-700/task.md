# Task: Issue #700

## 切片清单

- [x] T1 红：register 降级守卫失败测试（tests/test_issue_700_healthy_oscillation.py）
- [x] T1 绿：runtime.py UPDATE 守卫（探活新鲜不降级）
- [x] T2 红：ACP_CHECK_TIMEOUT env 覆盖失败测试
- [x] T2 绿：acp_bootstrap.py 超时常量 + env 覆盖
- [x] T3 红：Windows 启动器解析失败测试（.exe/.cmd/.bat）
- [x] T3 绿：_hermes_venv_bin() Windows 分支
- [x] 回归：test_issue_490 / test_issue_471（32 全绿）；相关面 acp/hermes/bootstrap 197 passed（3 预存失败在基线同样失败，非本次引入）
- [ ] 自审查（5 轴 + 开源前置增量检查）
- [ ] 建 PR（base=weekly-09）+ Issue 评论 + A2A QA
