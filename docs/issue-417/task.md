# Tasks: Issue #417

- [x] T1 根因定位：dashboard runtimes 查询 LEFT JOIN NULL 漏过滤 + register 竞态复活（spec.md）
- [x] T2 编写测试 tests/test_issue_417_revoked_runtime_residue.py（RED）
- [x] T3 C1 dashboard 查询侧兜底过滤（GREEN）
- [x] T4 C2 revoke 级联清理 engine_capabilities
- [x] T5 C3 runtime_register 竞态防护
- [x] T6 全量相关测试回归 + 自审查
- [x] T7 提交 feat/issue-417、建 PR（base=功能分支）、评论告知、A2A QA
