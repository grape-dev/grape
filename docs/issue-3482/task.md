# Tasks: Issue #3482

- [x] T1 repogit：新增 already exists 特征识别 + 恢复函数（迁移/重建/合并回收，会话级 gitdir 兜底）
- [x] T2 repogit：ensure_issue_worktree 失败分支接入恢复（只读特征匹配前，不进 refuse degrade）
- [x] T3 测试：TestWorktreeAlreadyExistsRecovery 三用例（恢复建树+产物保留+写上下文、迁移失败兜底、特征匹配）
- [x] T4 回归：tests/runtime/test_repogit.py 全量 + tests/runtime 抽样通过
- [x] T5 提交 feat/issue-3482、建 PR（base=arch-weekly-11-zj）、Issue 评论、A2A QA
