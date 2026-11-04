# Plan: Issue #979

## 方案概述
节点数据加 `skill_id` 作为权威标识；派发链路「id 优先、名字降级 + 告警」；PR 订阅对齐其他路径口径（补 user_id）；存量数据一次性迁移收敛。

## 改动清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `src/api/routes/scenes.py` | `SceneNodeInput` 增加 `skill_id: int = 0`（pydantic → nodes_json 序列化自动携带） |
| 2 | `src/user/db.py` | 新增 `resolve_skill_dispatch(skill_name, skill_id, repo_key, user_id, scene_id, node_tag)`：skill_id 直查（get_skill_by_id）→ 失效/缺失降级 resolve_skill_id（user→repo→official）+ `log.warning`（含场景/节点/降级原因）；返回 `{skill_id, skill_name, skill_path, degraded, skill}` |
| 3 | `src/api/event_pipeline.py` | ① 新增 `resolve_scene_node_skill_id()`（get_node_by_tag 读节点 skill_id）；② PR 订阅分支（`_is_pr_sub`）：补 `user_id=_issue_scene_uid` + 节点 skill_id 优先，id 命中跳过 #657 名字级 scope 门禁（显式绑定即授权）；③ plugin 场景覆盖分支：`resolve_skill_dispatch` 替代裸 `resolve_skill_id`，id 优先 |
| 4 | `src/api/db_setup.py` | 新增 backfill migration `202611040001 issue979_scene_nodes_skill_id_backfill`：遍历 devops_pipeline_scene（uid 由 created_by→unified_user_mapping）与 scene_market（source_user_id），name-only 节点按 user→repo→official 解析写回 skill_id，幂等（已有 skill_id 跳过） |
| 5 | `tests/test_issue_979_scene_skill_id_dispatch.py` | 三组单测：三档同名 id 锁定 / PR 订阅 user scope（helper 传 user_id）/ 老场景降级 warning + 迁移写回 |

## 关键设计决策
- 权威标识 = skill_id（引用最简、迁移一次到位）；skill_name 保留做显示与降级。
- skill_id 失效/缺失 → 名字降级 + warning（可用性优先、可观测）。
- 显式 skill_id 命中视为用户授权绑定，跳过 #657 名字级 scope 门禁；降级路径维持 #657 现行为。
- 迁移走既有 register_migration 体系（backfill kind，幂等续跑）。

## 风险与缓解
- 迁移解析错档：仅写回不删 skill_name，降级日志可观测，可人工修正。
- nodes_json 兼容：skill_id 可选字段，JSON 向后兼容。
- 迁移版本：取当前最大 stamp（202611030003）之后 202611040001。
