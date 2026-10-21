# Task: Issue #386

## TDD 顺序

- [x] T1 编写 `tests/test_issue_386_token_validation_400.py`（7 例，requests mock）
- [x] T2 运行 → 4 例 RED（当前返回 401，断言 400 失败）；502/清除 3 例先行通过
- [x] T3 修改 `src/api/routes/settings.py` 4 处（401→400 + 用户化文案）
- [x] T4 运行 → 7/7 GREEN
- [x] T5 回归 `tests/test_api_routes.py::TestSettings` 19/19 通过；全文件 17 失败经
      `git stash` 基线对比确认与本改动无关（Knowledge/Repos/Skills 既有失败）
- [x] T6 自审查（5 轴：正确性/安全/性能/可维护/测试）→ 原子提交 → push → 建 PR（base=weekly-08）
- [x] T7 Issue 评论告知 + A2A 通知 QA
