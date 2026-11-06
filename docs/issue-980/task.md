# Issue #980 Tasks

- [x] 共用解析层 src/api/skill_display.py（resolve + build_map，失活=未命中）
- [x] scenes.py with-pipeline 调用点替换（skill_id 优先 → 共用映射）
- [x] dashboard.py 调用点替换（同口径）
- [x] 单测：三档同名区分 / skill_id 缺失回退 / skill_id 失活回退 / 无冲突零回归 / 两路由行为级 / 源码契约
- [x] 回归：test_scene_list_pagination / test_api_routes / test_agents_routes / test_issue_406 与基线一致（基线既有 21 失败不受影响）
