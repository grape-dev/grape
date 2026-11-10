# Spec: Issue #1758 — 场景技能 scope 判定收敛为单一判官

> Source: Issue #1758 body + Issue Agent Spec (comment id=199533517) | Base branch: weekly-mys (pipeline work baseline)

## Objective
- 抽出唯一判定入口 `scene_skill_scope.evaluate(node, repo_key, scene_scope, caller)`，
  把「场景节点引用技能」的 scope 判定从 4 处各自实现改为统一调用。
- 固定口径：repo 级场景引用 user scope 技能一律不放行（含创建者本人）；
  显式 `skill_id` 仍需 scope 校验；查询异常统一 fail-closed。
- 保存阶段即拦截并给出可执行改绑候选；a2a 400 返回结构化字段。

## Why
同一份场景配置在保存/audit/派发/precheck 得到互斥结论，已造成
「保存能过、a2a 派发被 400 阻断」的真实故障（#1722 报障，events seq=61889/61890）。
任何单点修改都会让其它入口静默漂移。

## Scope
### In Scope
- 新增 `src/api/scene_skill_scope.py`：skill_id 解析、按名解析、scope 比对、
  fail-open/fail-closed 策略、改绑候选全部集中定义。
- 改造调用点：`scenes.py` 保存/apply 预检、`skill-scope-audit`、`event_pipeline`
  派发门禁、`scene_precheck.py`、`scenes detail` 节点级字段。
- a2a scope 不匹配 400 增加 `skill_scope` / `scene_scope` / `rebind_candidates`。
- 四入口交叉断言测试（保存预检 / audit / 派发 / precheck，另含 detail）。

### Out of Scope
- 不恢复 #1721 已回退的独立 detail 判官；detail 只在本判官结果上投影。
- 不改技能物化/分发链路；不新增 caller 维度例外开关。

## Acceptance Criteria
- [ ] 四入口对同一配置结论一致，交叉断言测试覆盖
- [ ] 保存阶段即拦截，错误含全部冲突节点与可执行改绑建议
- [ ] 全仓不再跨模块 import 私有判定函数（`_skill_scope_mismatch` 等）
- [ ] #1722 报障场景（repo 级场景 issue 节点按名引用 user 技能）保存阶段被拒绝
- [ ] a2a 400 结构化字段可被 CLI/前端直接消费
- [ ] 相关既有测试通过（基线已存在的环境型失败除外）