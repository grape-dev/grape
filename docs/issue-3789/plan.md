# Issue #3789 — 方案设计

## 写入端（mgmt/scenes）
- 新增 `scenes._persist_scene_skill_ids(nodes, repo_key, scene_id)`：复用单一 scope 判官
  `scene_skill_scope.evaluate`（repo 口径），命中 repo/official 时写回 `skill_id` 与规范
  `skill` 名；内置包形态（不在 skill 表）保持原样，派发侧继续按名降级。
- 接线：`create_scene` / `update_scene` / `create_market_scene` / `update_market_scene`
  在既有 `_validate_scene_skill_scope` 之后调用（校验先拦截非法档位，再固化 id）。
- 应用链路 `apply_scene`：节点副本先固化 id，有变更回写场景；session 落库改为
  `resolve_skill_dispatch(skill, node.skill_id, ...)`，不再用 user 档名字解析。

## 派发端（mgmt/user/db.resolve_skill_dispatch）
- `skill_id` 有效 → `skill_source="skill_id"`（id 权威）。
- `skill_id` 缺失/失效 → `resolve_skill_id(name, repo_key=...)`（repo→official，
  `user_id` 不参与降级顺序），`degraded=True`、`skill_source="name_degraded"`。
- 完全解析不到 → `skill_source="unresolved"`。
- 调用方（event_pipeline PR-sub / issue 场景覆盖、webhook A2A）把 `skill_source`
  写入事件 payload。

## 存量迁移（mgmt/migrations/scene_nodes_skill_id_repo_scope.py，202611240002）
- `devops_pipeline_scene`（repo 场景）：逐节点 repo→official 解析；当前 `skill_id`
  指向 user 档 → 清空并改绑 repo/official；解析不到清 0（派发按名降级）。
- `scene_market`：有 repo_key 的行同 repo 口径；user 行（scope=user）保留 user 解析。
- 幂等、输出迁移报告（扫描行 / 回填节点 / 纠正 user 档 / 未解析 / 写回行）；
  `post_check`：活跃 repo 场景节点不得引用 user 档技能，否则不盖章。

## 测试 seam
- 保存：`create_scene` / `create_market_scene` 路由（持久化结果读 `nodes_json`）。
- 派发：`resolve_skill_dispatch`（id 权威 / 降级顺序 / skill_source）。
- 迁移：`backfill_scene_nodes_skill_id`（幂等、user 纠正、user 市场行保留）+
  `verify_repo_scenes_without_user_skill`（收敛判定）。
- 应用：`apply_scene` + monkeypatch `upsert_agent_session` 断言 skill_id。