# Spec: Issue #955 — 场景节点 tag 无角色枚举校验导致派发引擎绑定静默失效

## 目标
修复场景节点 tag 无角色枚举校验导致的派发引擎绑定静默失效（全量广播）：
- 写侧拦截非法角色名（节点 id ∈ `_KNOWN_AGENT_ROLES ∪ {"chat"}`，非法 → 400）
- 读侧按节点 id（角色名）优先匹配、tag 兜底，恢复非标准 tag 存量场景的引擎绑定
- 顺带修复 `_validate_scene_engine_ids` 按 id 校验能力标签，堵住 `tag='all'` 通配

## 范围
| 模块 | 改动类型 | 说明 |
|------|---------|------|
| src/api/routes/scenes.py | 修改 | `_validate_scene_node_roles`（400）+ create/update 挂载；`_validate_scene_engine_ids` id 优先 + 去 `tag='all'` 通配 |
| src/api/pipeline_config.py | 修改 | `get_scene_engine_binding` / `get_node_by_tag` id 优先、tag 兜底 |
| src/runtime/engine_select.py | 复用 | `_KNOWN_AGENT_ROLES` 作为角色全集唯一来源 |

## 验收标准
- [x] 保存 id 非标准角色的场景 → 400，报错含非法值与角色全集
- [x] 保存 tag=中文 + id=vod 的场景 → 允许且正常生效
- [x] 合成回归场景（tag=中文阶段名、id=角色名、engine_runtime_ids=[eng]）派发 pool_engine_ids 含指定引擎，不再全量广播
- [x] get_node_by_tag 在 tag=中文/id=角色名场景解析到技能，不退回 mapping 兜底
- [x] redispatch 46108 对照（线上引擎 ID，以合成场景等价复现，见测试注记）
- [x] 现有相关测试通过 + 新增 tests/test_issue955_scene_node_role_validation.py（17 用例）
