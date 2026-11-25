# Issue #4257 任务拆解

- [x] 定位守卫实现（`src/domains/runtime/vcs/release_guard.py`，参考 #3811 同守卫域）。
- [x] 收紧正文主语锚点：`Issue #N` / `Issue N` 显式形态，排除 `段名#序号` 紧邻形态。
- [x] 剔除 `\w+#\d+` 链路引用后再取锚点。
- [x] 回归用例：链路引用放行 / 其它 Issue 仍 409 / 主语一致不回归。
- [x] 运行 `pytest tests/runtime/test_issue_4257_release_guard_link_chain.py`
      与既有 `test_issue_2225_release_guard.py`、`test_issue_3811_release_guard_domain_routing.py`。
- [ ] 建 PR 并通知 QA 审查。