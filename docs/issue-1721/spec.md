# Spec: Issue #1721 — scenes detail 节点级 scope 可解析性前置暴露 + 存量冲突体检与改绑指引

## Objective
- `scenes detail` 节点级新增 `resolvable` / `scope_check`，让 Agent 在 A2A 派发前即可判定节点技能是否可解析。
- `GET /api/scenes/skill-scope-audit` 支持 `tk_` 会话 token 只读体检本仓，并输出存量冲突场景与改绑指引。
- 不改变派发 400 契约（#657）与 webhook 信封；不静默改写历史场景配置。

## Scope
### In Scope
- `src/api/routes/scenes.py`：
  - `scenes detail` 节点级附加 `resolvable` 与 `scope_check{ok, skill, actual_scope, expected_scope, reason}`；与派发侧同一口径：节点显式 `skill_id` active 即视为授权（跳过名字级 scope 门禁），否则按技能名复用派发侧 `_skill_scope_mismatch` 判定；detail 阶段 reason 使用“保存/派发时将拦截”措辞（不复用“派发已阻断”）。
  - `skill-scope-audit` 鉴权从 `require_no_session_token` 放宽为 `get_current_user_or_api_key`，`tk_` 限定本仓（`repo_key=会话仓`）、只读、越权仓拒绝。
  - audit 响应每个冲突项附带建议改绑的 repo/official 级同类技能（仅指引）。
### Out of Scope
- 派发 400 契约（#657）与 webhook 信封可读性。
- 平台自动改绑历史场景；场景保存/apply 预检（#657/#1564）仅做回归。

## Technical Approach
1. 新增 `_scene_node_scope_check` / `_attach_scene_node_scope_checks`：逐节点按技能名调用派发侧判定函数，附加增量字段；不修改缓存中的节点对象（复制后附加）。
2. `get_scene_detail` 返回前对 `cfg["nodes"]` 做增量附加，其余响应字段不变。
3. `audit_scene_skill_scope` 直接依赖 `get_current_user_or_api_key`：会话 token 未显式传 `repo` 时取绑定 `repo_key`；显式传其它仓则 403。响应在既有 violations 上追加 `suggested_skills`。
4. `_suggest_repo_official_skills` / `_skill_agent_type_by_name`：只读查询同 agent_type 的 repo/official 级技能作为改绑候选。

## Acceptance Criteria
- [ ] 报障场景 `agent-best-practices/fix-system-agent` 的 `default` 在 detail 即可看到 issue 节点 `resolvable=false` 与 `scope_check`（技能名、user/repo 双方 scope、原因）。
- [ ] `skill-scope-audit` 以 `tk_` 可读本仓冲突清单与改绑建议，且不写库；显式其它仓 403。
- [ ] 无冲突场景 detail 仅新增字段，既有字段与行为不变。
- [ ] 冲突场景保存/apply 仍 400（回归）；改绑后派发门禁放行（#657/#1564 既有链路保障，另由 `test_detail_after_rebind_to_repo_skill_is_resolvable` 覆盖改绑后 `resolvable=true` 且 `_skill_scope_mismatch=false`）。
- [ ] 显式有效 `skill_id` 的节点 detail 判定 `resolvable=true`（与派发侧一致）；`skill_id` 失效时回退名字判定。
- [ ] 现有测试通过，新增测试覆盖 detail 增量字段、`skill_id` 口径、改绑回归、audit `tk_` 鉴权与越权拒绝。

## Open Questions
- 无。
