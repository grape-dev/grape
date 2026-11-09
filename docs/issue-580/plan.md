# Plan: Issue #580

## 任务拆解
1. T1 代码：`src/api/git_repo_api.py`
   - `LabelCreateBody.color` 默认值 `#d73a4a` → `d73a4a`
   - `create_label` 透传前 `color = body.color.lstrip('#')`
2. T2 测试：`tests/test_git_repo_api.py`
   - 新增 `test_labels_color_normalization`：GitHub/GitCode 双通道 × 带/不带 `#` 四种组合，断言透传 body color 均为不带 `#` 的 hex；不传 color 时默认 `d73a4a`
   - 修正既有断言：`test_contents_branches_milestones_labels` 中带 `#` 输入的透传结果改为归一化后的 `d73a4a`
3. T3 文档：`help/api/10-git-repos.md`、`docs/openapi/10-git-repos.md` 中 labels create 的 color 示例去 `#`

## 验收
- `python3 -m pytest tests/test_git_repo_api.py tests/test_openapi.py tests/test_api_docs_sync.py -q` 全通过

## 提交
- 原子提交（英文 message），分支 `feat/issue-580`，PR base `weekly-08-mys`
