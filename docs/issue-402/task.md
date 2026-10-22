# Task: Issue #402 — 任务拆解

- [ ] T1 pipeline_config：`_query_scene_by_id` 补充 icon 返回
- [ ] T2 scenes.py：`_clean_scene_engine_ids_for_copy` 清洗助手
- [ ] T3 scenes.py：`POST /{scene_id}/copy` 端点（名称生成/冲突序号/截断/创建/重建技能引用）
- [ ] T4 前端：详情页复制按钮 + `_copySceneFromDetail` 弹窗/提交/跳转
- [ ] T5 i18n：中英文案 key
- [ ] T6 文档：docs/openapi/06-scenes.md + help/api/06-scenes.md
- [ ] T7 测试：tests/test_scene_copy.py + tests/js/test_issue_402_scene_copy.js，全量回归
