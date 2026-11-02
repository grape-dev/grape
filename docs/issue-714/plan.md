# Plan: `_vcs_url()` subpath 逐段 percent-encode（Issue #714）

## 实现路径
1. `src/api/git_repo_api.py` `_vcs_url()`：`base + subpath` 改为 `base + "/".join(quote(seg, safe="") for seg in subpath.split("/"))`。
   - 纯 ASCII 段编码后不变 → branches/issues/pulls 等存量路由零变更；
   - `/` 分隔语义保留 → ref/branch 类 subpath 结构不变。
2. 测试（TDD）：`tests/test_git_repo_api.py` 新增 5 用例（+ 编码 / 中文+空格 / ASCII 回归 / 404 透传 / `_vcs_url` 单元级）。

## 设计决策
- 编码位置：后端 `_vcs_url` 单点（与 owner/repo 一致），CLI 不动。
- 编码粒度：按 `/` 逐段（`safe=""`），保留分隔语义。

## 风险
- 过度编码：quote 对字母数字及 `-_.~` 恒不编码，ASCII 回归用例把关。
