# TODO: Issue #287

- [ ] scheduler.py `_execute_autopilot_remote` 入队前预解析 skill_id（created_by）
- [ ] enqueue_task 传 skill_id，user_id 保持 0
- [ ] 解析失败降级 + warning 日志
- [ ] 新增测试：user scope 命中 / 降级不阻断
- [ ] 全量相关测试回归
- [ ] 自审查 + 建 PR + 评论 + A2A QA
