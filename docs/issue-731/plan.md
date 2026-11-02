# Plan: Issue #731

1. `src/api/git_repo_api.py`：新增 `_normalize_pull_files_diff(resp)`：
   - 顶层 `.diff`（string）且 `.patch.diff` 缺失 → 并入 `.patch = {"diff": ...}`（保留已有 patch 元信息），移除顶层字段
   - `.patch` 为 string（旧式）→ 收敛为 object `{"diff": ...}`
   - 归一化后一致性断言：无 `.patch.diff` → log.warning 显式暴露
   - `list_pull_files` 返回前调用；非 list 出参原样透传
2. `docs/openapi/10-git-repos.md`：pulls/files 返回示例与契约标注统一字段路径 `.patch.diff`
3. `tests/test_git_repo_api.py`：新增 3 个测试（顶层 .diff 归一化 / patch string 收敛 / 非 list 透传）
