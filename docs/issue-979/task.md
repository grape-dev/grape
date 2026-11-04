# Task: Issue #979

- [x] T1 SceneNodeInput 增 skill_id（nodes_json 持久化）
- [x] T2 db.resolve_skill_dispatch（id 直查 + 名字降级 + warning）
- [x] T3 event_pipeline：resolve_scene_node_skill_id + PR 订阅补 user_id / id 优先 + plugin 覆盖分支 id 优先
- [x] T4 migration 202611040001 老场景写回 skill_id
- [x] T5 单测（三档同名 / PR 订阅 user scope / 老场景兼容 + 迁移）
- [x] T6 全量相关测试回归
- [x] T7 QA 复审修复：DCO 补签（amend -s）；backfill scene_market 行取 repo_key（repo 档）；get_node_by_tag user_id 生效（个人场景节点查找）；PR-sub / issue 场景覆盖分支管线级集成测试；fallback 分支 id 命中同步跳过 #657 名字级门禁（集成测试暴露的真实缺口）
