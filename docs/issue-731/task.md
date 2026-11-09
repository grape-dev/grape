# Tasks: Issue #731

- [x] 归一化函数 `_normalize_pull_files_diff` + `list_pull_files` 接入
- [x] 契约文档 `docs/openapi/10-git-repos.md` 标注 `.patch.diff`
- [x] 测试：顶层 `.diff` 混入归一化（含元信息合并/以 .patch.diff 为准）
- [x] 测试：string 形式 `.patch` 收敛为 object
- [x] 测试：非 list 出参透传
- [x] 全量跑 tests/test_git_repo_api.py（基线遗留失败 test_issues_crud_mapping 与本改动无关，stash 验证）
