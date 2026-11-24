# Issue #3821 任务拆解（task）

- [ ] T1（红）新增 `tests/api/test_issue_3821_comment_update_delete.py`：
  - issues 数字 id create→update→delete；
  - issues 十六进制 id update/delete（单查解析）；
  - issues 非法 id 422 可读；
  - pulls 数字 id（/pulls 创建路径）update/delete 直传 /pulls 通道；
  - pulls 合入后回退 issue note：数字 id 400 note-type → /issues 通道；hex id 解析走 issue scope；
  - pulls 其他 400 不回退。
  - 契约 operation 存在断言。
- [ ] T2（绿）通用 id 解析器 + issues PATCH/DELETE + pulls 通道选路。
- [ ] T3 `gitlab.py` 补 `ISSUE_COMMENT_FLAT_PATH=False` 与 issue note 路径 rewrite。
- [ ] T4 契约重定基（openapi_baseline 390→392）+ required_operations 新增两条。
- [ ] T5 help/api/10-git-repos.md 端点块 + CLI 示例。
- [ ] T6 回归：pytest 相关套件 + 契约/文档同步测试全绿。
- [ ] T7 自审查 + pre-pr 增量检查 → 建 PR → Issue 评论 → A2A QA → tasks complete。