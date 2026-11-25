# Spec: Issue #3788 无场景绑定的 issue 事件被技能 scope 门禁 fail-closed 拦截

> 权威版本见 Issue #3788 评论「📋 Spec Document」（Issue Agent 出品，评级 ready）；本文为实现侧需求摘录。

## Objective

`scene_id` 为空的 issue 事件（`feedback→issue` 建单、API 建单不传 `scene_id`）不得再被技能
scope 门禁按 repo 场景策略判为 mismatch 而 fail-fast；「场景存在且为 repo 级时禁止引用 user
档技能」的既有策略（#1722 / #1758）保持不变。

## 现象与根因（实现侧复核）

- 事件中心 1 小时 11 条 `skill_resolve_failed`（`scene=` 为空，`skill=issue-cli` scope=user）。
- 根因：`scene_skill_scope.evaluate()` 的 `expected_scope = scene_scope or SCENE_SCOPE_REPO`——
  调用方无法表达「无场景」，空串仍回落到 `repo`，因此 session 级 user 档技能必然 mismatch。
- 派发侧唯一入队口 `_enqueue_pipeline_task()` 的门禁从 `evt` 取场景，而 `evt` 并不携带场景
  （`normalize_event()` 不写 `scene_id`），所以「有没有场景」在门禁里根本不可知——这是本单
  必须一并收口的事实：否则「无场景放行」等于把 #1722/#1758 的派发期保护整体关掉。

## 需求（In Scope）

1. 判官 `src/api/scene_skill_scope.py::evaluate` 新增**显式**「无场景」语义载体：
   `scene_scope=None` → 不执行 repo→user 拦截，`ok=True`、`expected_scope=""`、
   `source="no_scene"`，reason 标注「无场景绑定，按 session 档位放行」。
   `""` 与缺省仍回落 `SCENE_SCOPE_REPO`（四入口语义零改动）。
2. 派发侧门禁 `_evaluate_skill_scope` / `_enqueue_pipeline_task` 显式调用该分支：
   - 场景三态判定：`evt` 已带场景 → 按 repo 口径；`evt` 未带 → 读 Issue 真实绑定
     （`_issue_authoritative_scene`）；**确认未绑定**（`""`）→ `scene_scope=None`；
     **未判定**（查询异常 `None`）→ 仍按 repo 口径（fail-closed 不回退）。
   - 放行时输出结构化 INFO：`decision=no_scene`、skill、actual_scope、`scene_id=""`。
3. 单测覆盖三条路径：无场景放行、repo 场景引用 user 技能仍拦截、scope 查询异常未判定仍拦截。

## Out of Scope

- 建单链路（`feedback→issue`、API 建单）强制/默认绑定 `scene_id`：产品决策，另立 issue。
- `scene_scope` 枚举、DB 字段与四入口（保存/体检/详情/预检）调用点改动。

## Acceptance Criteria

- [ ] 无场景 issue 事件 + session user 档技能 → 派发门禁 `ok=True`、`source=no_scene`、
      `expected_scope=""`，不再产生 `skill_resolve_failed`；日志含 `decision=no_scene`。
- [ ] repo 级场景（`evt` 未带场景、Issue 真实绑定）下 user 档技能 → 仍判 `ok=False`，
      `rebind_candidates` 保留（#1722/#1758 不退化）。
- [ ] Issue 场景查询异常（未判定）→ 仍按 repo 口径（不放行）。
- [ ] `evaluate(..., scene_scope="")` 与不传 `scene_scope` 的行为与改动前一致（四入口零退化）。
- [ ] 相关既有用例（`tests/mgmt/test_issue_1758_scene_skill_scope.py`）不新增退化。