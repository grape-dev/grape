# Task: Issue #1635

- [x] T1 skill_materialize：manifest/digest/version/drift 基础函数
- [x] T2 validate_skill_dir 四元组 + baseline_dir/scope，内部调用点同步
- [x] T3 zip/files/dir 三渠道统一漂移校验与 official 阻断
- [x] T4 来源透传（skill_id/scope/type/version/digest + drift）
- [x] T5 event_pipeline / agent / direct_executor / scheduler 调用点补传
- [x] T6 新增 7 条测试；回归 1070/887/367/574 全绿
- [ ] T7（关联仓库 grape-skills）补 cross-scene-handoff.md / Step 4b / 3.4.0 / 兜底正则
      —— 本仓无关联 worktree，需在 grape-skills 单独提交