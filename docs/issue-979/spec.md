# Spec: Issue #979 — 场景节点 skill_id 权威化（三档同名派发锁定）+ PR 订阅补 user_id

> 基线：weekly-mys（场景注入，权威）。来源：Issue Agent Spec（Issue #979 评论）。

## Objective
- 场景节点持久化 `skill_id`（权威标识）+ `skill_name`（兼容显示）；派发优先 skill_id、按名字降级时打 warning（含降级原因）；PR 订阅派发补齐 `user_id`；老场景一次性迁移写回 skill_id。
- 动因：技能表三档（official/repo/user）允许同名共存（UNIQUE(skill_name, scope, owner_id)，src/api/db_setup.py:114），节点只存 skill_name 导致派发无法锁定档位；PR 订阅路径漏传 user_id 使个人同名技能静默落 official。

## Scope
1. 节点模型：`src/api/routes/scenes.py` `SceneNodeInput` / `nodes_json` 增加 `skill_id`（与 `skill_name` 并存，可选，向后兼容）
2. 派发解析：`src/api/event_pipeline.py` 改为 skill_id 优先；skill_id 查不到或缺失 → 按 skill_name 走现有 user→repo→official 规则（`src/user/db.py resolve_skill_id`）降级并打 warning
3. PR 订阅路径：`src/api/event_pipeline.py`（`_is_pr_sub` 分支）补传 `user_id`（优先用节点 skill_id）
4. 老场景迁移：新增 backfill migration（devops_pipeline_scene + scene_market），按现有规则解析一次并写回 skill_id
5. 单测：三档同名绑定 / PR 订阅 user scope / 老场景降级兼容 + 迁移写回

## Out of Scope
- #978（三档同名 UI 选中联动）
- 技能表 schema 变更、新增 scope 档位

## Acceptance Criteria
- [ ] 三档同名技能分别绑定同一 agent 类型场景，派发命中的 skill_id 与各自选择一致
- [ ] PR 订阅场景绑定个人同名技能 → resolve_skill_id 收到 user_id，解析到 user scope
- [ ] 仅含 skill_name 的老场景：派发结果与当前行为一致，且日志出现降级 warning
- [ ] 老场景迁移后节点写入 skill_id
- [ ] 现有测试通过；新增测试覆盖上述场景
