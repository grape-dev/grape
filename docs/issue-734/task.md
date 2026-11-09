# Issue #734 任务拆解

| # | 任务 | 状态 |
|---|------|------|
| 1 | `get_repo_tree`：GitCode ref 存在性预校验 + 404 归一（sha ref 直连豁免） | 完成 |
| 2 | `_ref_to_tree_sha`：解析失败返回 None（调用方 404），HTTPException 上抛 | 完成 |
| 3 | `tests/test_git_repo_tree_slash_ref.py`：不存在 ref 404 矩阵 + sha 直连回归 | 完成 |
| 4 | `tests/test_git_repo_api.py`：contents 404 回归矩阵 | 完成 |
| 5 | 全量回归（两个测试文件 + test_openapi） | 完成 |
| 6 | 提交、建 PR（base=weekly-08-mys）、A2A QA | 完成 |
