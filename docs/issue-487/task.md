# Task: Issue #487

- [x] T1 后端：repos.py 新增 `_resolve_repo_key`，GET/PUT scene-recommend-config 改用真实前缀解析
- [x] T2 后端：db.py `set_scene_recommend_config` 空场景显式 ValueError、UPDATE 0 行 INSERT 兜底、返回真实结果
- [x] T3 后端：PUT 路由映射 ValueError→400、写入失败→400
- [x] T4 前端：dashboard.js 弹窗按钮禁用/启用、失败提示、_addRecRuleRow 空选项守卫
- [x] T5 测试：tests/test_scene_recommend_config.py（保存回读 / github 前缀 / 空场景 400 / upsert 兜底）
- [x] T6 测试：tests/js/test_issue_487_scene_recommend_save.js（禁用/启用/失败提示）
- [x] T7 全量回归：pytest + node 测试通过
- [x] T8 自审查（5 轴 + pre-pr 增量检查）→ 提交 → PR → 评论 → A2A QA
