# Task: Issue #1753

- [x] T1 `_read_target_scene_work_branch` 读取承接场景 work_branch
- [x] T2 `_cross_pipeline_handoff` 补/覆盖 `carried_context.target_branch` 并留痕
- [x] T3 A2A 上下文与审计事件显式声明承接基线
- [x] T4 承接侧 issue Agent prompt 基线一致性校验指引
- [x] T5 新增测试 `tests/test_issue_1753_handoff_target_branch.py`
- [x] T6 回归 cross-pipeline / prompt 相关测试
- [x] T7 自审查 → commit → push → PR（base=weekly-mys）→ Issue 评论 → A2A QA
- [x] T8 复审修复：prompt 仅对 `target_scene_work_branch` 来源声明权威，缺失标 UNCONFIRMED
- [x] T9 复审修复：抽 `session_store` 场景 work_branch 公共 helper，消除三处重复查询
- [x] T10 复审补测：端到端断言 prompt 目标分支 == 派发时承接场景 `work_branch`（含快照漂移）
- [x] T11 第三轮：A2A 基线声明以 `target_branch_source` 为门，未确认输出 UNCONFIRMED
- [x] T12 第三轮：多跳链无 `work_branch` 失效历史权威并清除 `target_branch_replaced`
- [x] T13 第三轮：`target_scene_work_branch` / `unconfirmed_no_target_scene_work_branch` 提共享常量
- [x] T14 第四轮：运行时基线改用 `session_store.get_scene_work_branch`（env 仅可选覆盖）
- [x] T15 第四轮：A2A 基线声明前置 + `handoff_baseline_note` 独立字段 + 派发 evt 携带
- [x] T16 第四轮：`event_pipeline` 场景/高级选项查询收敛到 `session_store` 公共 helper