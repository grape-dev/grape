# Issue #3789 — 任务拆解

- [x] T1 派发降级顺序改为 repo→official + `skill_source` 标记（`resolve_skill_dispatch`）。
- [x] T2 保存/应用链路持久化 `skill_id`（`_persist_scene_skill_ids` 接线 create/update/
      market/create/update/apply）。
- [x] T3 应用链路 session 落库改用 `resolve_skill_dispatch`（id 权威，禁 user 降级）。
- [x] T4 存量迁移 202611240002：repo 场景 repo→official 回填、纠正 #979 写入的 user 档，
      user 市场场景保留 user 档；迁移报告 + post_check。
- [x] T5 事件 payload 透传 `skill_source`（event_pipeline PR-sub / issue 覆盖、webhook A2A）。
- [x] T6 测试：保存持久化、同名 user 不误绑、降级标记、迁移幂等/纠正、应用 id 权威。
- [x] T7 更新受影响既有用例（#979 降级顺序口径）。
- [ ] 后续：线上观测 Loki `without skill_id` 告警是否收敛至 0（部署后验证）。