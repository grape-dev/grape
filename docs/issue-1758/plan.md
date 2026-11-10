# Plan: Issue #1758

## 方案概述
「单一纯函数判官 + 五处薄适配」：判官返回结构化结果
`{ok, skill, skill_id, actual_scope, expected_scope, reason, source, owner_id,
node_id, node_tag, rebind_candidates}`，各入口只负责传上下文与映射响应形态。

## 实现步骤
1. 新增 `src/api/scene_skill_scope.py`
   - `evaluate(node, repo_key, scene_scope="repo", *, scene_id,
     on_error="deny", cache=None)`：skill_id 优先、失效回退名字；repo/official
     兼容、user 一律拒绝（含本人）；repo 档校验归属仓库；未知技能按官方内置包
     兼容放行；查询异常默认 fail-closed，可显式 `on_error="allow"` 降级。
   - `rebind_candidates`、`skill_agent_type_by_name`、`format_rebind_hint`。
2. `src/user/db.py`：`get_skill_by_id` / `get_skill_scope_info` 增加仅关键字
   参数 `raise_on_error=False`，判官传 True 以区分查询异常与未命中。
3. `scenes.py`
   - `_collect_scene_skill_scope_violations` 改为逐节点调用 `evaluate`
     并复用查询 cache。
   - `_validate_scene_skill_scope` 400 增加 `skill_scope` / `scene_scope` /
     `rebind_candidates` / `rebinds`，逐节点 reason 含改绑候选。
   - `_scene_node_scope_check` 改为判官结果投影，删除对
     `event_pipeline._skill_scope_mismatch` 的跨模块 import。
   - audit 改绑建议委托 `rebind_candidates`。
4. `event_pipeline.py`
   - `_skill_scope_mismatch` / `_skill_scope_mismatch_reason` 保留为兼容 shim，
     内部委托判官；新增 `_evaluate_skill_scope` 供派发门禁直接调用。
   - PR 订阅、issue 场景覆盖、fallback 派发、VOD 重派四个门禁统一为
     「显式 skill_id 也校验」。
   - `_fail_skill_resolve_event` 增加 `scope_info`，result/diag/event payload
     携带 `skill_scope` / `scene_scope` / `rebind_candidates`。
5. `webhook.py`：a2a issue 场景覆盖路径改为判官校验（无论名字解析是否命中），
   400 返回结构化字段。
6. `scene_precheck.py`：`_skill_resolvable` / `_check_skill` 改为判官，
   查询异常不再无条件 fail-open；problem 文案含 reason 与改绑候选。
7. 测试：新增 `tests/test_issue_1758_scene_skill_scope.py`（判官口径 +
   四入口交叉断言 + 400 结构化字段）；同步更新 #657/#1564/#1721/#979/#1245
   中与新固定口径冲突的断言。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| repo 场景放行本人 user 技能 | 不放行 | 派发身份是 agent 而非本人，保存放行必然与派发矛盾 |
| 显式 skill_id 是否授权 | 仍需 scope 校验 | id 有效不代表引用方有权使用；派发侧也按 id 解析，必须同判 |
| 查询异常 | 统一 fail-closed（可显式降级） | 各入口独立 fail-open 是漂移根源 |
| 未知技能（不在 skill 表） | 兼容放行 | 官方内置包形态的存量兼容，存在性检查另有 `_check_skills_exist` |
| 复用方式 | 独立公共模块 | 禁止跨模块 import 私有符号 |

## 风险与缓解
| 风险 | 缓解 |
|------|------|
| 口径收紧后存量场景配置被拒 | 保存/apply 一次性返回全部冲突节点 + 改绑候选；audit 提供存量扫描 |
| 派发门禁 fail-closed 放大查询异常 | 判官保留 `on_error="allow"` 显式降级开关并记录 source=db_error |
| 四入口改造遗漏 | 交叉断言测试常驻（保存/audit/派发/precheck/detail） |
| #979 显式 id 口径变化 | 更新 #979 用例：repo 档 id 仍权威，user 档 id 改为拦截 |