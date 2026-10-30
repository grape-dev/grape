# Task: Issue #524

- [x] T1 `ApplySceneRequest` 增加 `strict` 参数
- [x] T2 重写 `apply_scene` 节点循环：按节点回执 + updated 计数修正 + strict 整单拒绝
- [x] T3 `agents.py get_agent_detail` 增加 declared/effective 区分字段
- [x] T4 新增 `tests/test_issue_524_scene_apply_receipt.py`（不健康 / 恢复后重 apply / 部分成功 + agents detail 字段）
- [x] T5 回归：`tests/test_scene_apply_enable.py`、`tests/test_agents_routes.py`、`tests/test_health_scorer.py` 通过
- [x] T6 自审查（5 轴 + pre-pr 增量检查）→ 提交 → PR（base=weekly-08-mys）→ 评论 → A2A QA
