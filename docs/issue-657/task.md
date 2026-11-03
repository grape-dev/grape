# Tasks: Issue #657

- [x] T1 勘察 apply/保存/派发/技能解析代码路径，定位静默失败点
- [x] T2 输出 spec.md / plan.md
- [x] T3 编写失败用例（apply 拦截 / 保存拦截 / 派发兜底 / smoke 回归）
- [x] T4 event_types 新增 skill_resolve_failed；user/db 新增 get_skill_scope_info
- [x] T5 scenes.py apply + create/update 校验（scope 不匹配 400）
- [x] T6 event_pipeline.py + webhook.py 派发前 fail-fast 错误事件
- [x] T7 全量相关测试回归（新增 + 存量）
- [ ] T8 自审查（5 轴 + 开源前置增量检查）→ 建 PR → 评论 → A2A QA
