# Plan: Issue #4329 release guard 主语边界收紧

## 改动点
- `src/domains/runtime/vcs/release_guard.py`：`_ANCHOR_RE` 由
  `(?i)(?<![a-z])issues?\s+(?:#\s*)?(\d+)` 收紧为
  `(?i)(?<![A-Za-z0-9_/.])issues?\s+(?:#\s*)?(\d+)(?![A-Za-z0-9_])`，
  注释同步说明边界语义（显式 ASCII 字符集、中文紧邻保留、编号续写排除）。
- `first_issue_anchor` / `first_issue_ref` / `check_issue_consistency` 调用链不变，
  链路剔除（`_strip_chain_refs`）已存在于 `weekly-mys` 基线，保持不动。

## 测试
- 新增 `tests/runtime/test_issue_4329_release_guard_subject_boundary.py`：四类验收
  （样例前缀放行 / 文件名与路径放行 / 显式跨 Issue 仍 409 / 既有语义与链路引用回归）。
- 回归 `tests/runtime/test_issue_2225_release_guard.py`、
  `tests/runtime/test_issue_3811_release_guard_domain_routing.py`、
  `tests/runtime/test_issue_4257_release_guard_link_chain.py`。

## 风险
- 边界排除过宽漏拦真跨 Issue：以显式 `Issue #9999` 拦截用例锁定。
- 中文紧邻合法主语误伤：边界字符集不含中文，回归 2225 中文用例锁定。

## 任务拆解
1. 收紧正则 + 注释；2. 新增测试文件；3. 跑四套守卫测试；4. 提交推送建 PR，交 QA。
