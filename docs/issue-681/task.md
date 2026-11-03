# Task: Issue #681 开发拆解

- [x] T1 session_store.get_issue_bound_scene（两层真实绑定，无 dev_scene 回退）
- [x] T2 event_pipeline 推荐入口判定换用新函数
- [x] T3 核查 worker _handle_vod_recommend_complete / direct 路径无同款判断（无需改）
- [x] T4 tests/test_issue_bound_scene.py 三态 + pipeline 行为测试（12 用例）
- [x] T5 回归：test_issue_415_scene_binding.py（10）+ test_bot_opened_recommend.py（6）+ 场景族测试全绿
- [x] T6 文档：docs/issue-681/spec.md / plan.md / task.md
