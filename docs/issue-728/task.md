# Task: Issue #728 — 任务拆解

- [x] T1 纯函数模块 `src/vcs/issue_ref_filter.py`（strip + extract，strict 可选）
- [x] T2 `create_pull` 自动关联走过滤层（`related_issue_nums` 空时从 body 提取）
- [x] T3 服务端 unlink 端点 `gitrepo_pulls_issues_unlink`（gitcode GET 校验 + PATCH 剩余；幂等；github no-op）
- [x] T4 runbook 文档：`help/api/10-git-repos.md` unlink 示例 + 规避写法说明段
- [x] T5 新增测试 `tests/test_issue_728_pr_issue_unlink.py`（过滤三语境 + strict + 自动提取 + unlink 三分支）
- [x] T6 回归 `tests/test_git_repo_api.py` + `tests/test_git_provider.py`
- [x] T7 自审查（5 轴 + pre-pr 增量检查）→ 提交 → push → 建 PR（base=weekly-08-mys）
