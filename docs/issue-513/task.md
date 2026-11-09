# Task: Issue #513 — 执行清单

- [x] T1 红：新增 `tests/test_devstage_delete_404_513.py`（`delete_missing_environment_is_404` 原子用例 `-m atomic` + 上游 404/502 分类用例 + 客户端 `_request` 判别用例）；`pytest.ini` 注册 `atomic` marker。提交 `test(#513)`。
- [x] T2 绿：`src/im/devstage_client.py` `DevStageError.not_found` 分类 + `_request()` 白名单判别；`src/api/routes/devstage.py` `delete_environment()` 404 统一包络映射 + 前置 `_list_envs` 信号修复。提交 `feat(#513)`。
- [x] T3 回归：devstage 相关测试全绿 + 全量相关面回归；`docs/issue-513/` 文档入库。
- [x] T4 自审查（5 轴 + 开源前置增量检查）→ 建 PR（base=weekly-09）→ Issue 评论 → A2A 通知 QA。
- [x] 同根因 #413：无既有 PR（已核验），本 PR 一次覆盖两单（PR body 关联 #413）。
