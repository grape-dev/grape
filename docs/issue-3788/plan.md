# Plan: Issue #3788 无场景门禁分支

## 设计要点

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 「无场景」语义载体 | 新增 `no_scene` 布尔 / `scene_scope=None` | `scene_scope=None` | 显式、不引入新枚举；四入口（不传 `scene_scope`）零改动 |
| 空串 `""` 语义 | 放行 / 仍回落 repo | 仍回落 `repo` | 锁死 #1722/#1758：任何显式 repo 判定都不因空串静默放行 |
| 派发侧如何知道「无场景」 | 判官内 `scene_id==""` 短路 / 派发侧显式入参 | 派发侧显式入参 | 判官内短路会一并关闭保存/体检/详情/预检的校验 |
| 派发侧场景来源 | 只看 `evt` / `evt` 缺失时读 Issue 真实绑定 | `evt` 优先，缺失回读真实绑定 | `normalize_event` 不写 `scene_id`，只看 `evt` = 门禁永远认为无场景，会把保护整体关掉 |
| 查询异常（未判定） | 放行 / 拦截 | 拦截（repo 口径） | 只有「确认未绑定」才放行，DB 抖动不得扩大放行面 |

## 切片

1. **切片 1（红）**：新增 `tests/runtime/test_issue_3788_no_scene_skill_gate.py`
   - 判官：`scene_scope=None` → `ok/source=no_scene/expected_scope=""`；
     `scene_scope=""`、不传 → 仍 `ok=False`（user 档）；
   - 门禁场景三态：未绑定 → `("", None)`；已绑定 → `(scene, "repo")`；查询异常 → `("", "repo")`；
   - 端到端：无场景 issue + session user 档技能 → 不落 `skill_resolve_failed`。

2. **切片 2（绿，判官）**：`src/api/scene_skill_scope.py`
   - `evaluate(..., scene_scope: str | None = SCENE_SCOPE_REPO)`；
   - `scene_scope is None` → 新分支：best-effort 解析 `actual_scope`（可观测），恒 `ok=True`、
     `expected_scope=""`、`source="no_scene"`；解析异常不改变放行结论（无场景不适用该策略）；
   - `_result()` 的 `expected_scope` 兜底只在 `None` 时回落 `repo`（空串可透传）。

3. **切片 3（绿，派发侧）**：`src/domains/runtime/pipeline/event_pipeline.py`
   - 新增 `_gate_scene_context(repo_key, evt, issue_id)`：返回 `(scene_id, scene_scope)` 三态；
   - `_evaluate_skill_scope(..., scene_scope=SCENE_SCOPE_REPO)` 透传；
   - `_enqueue_pipeline_task` 门禁用三态结果，`source=no_scene` 时留结构化 INFO。

## 影响面

- `src/api/scene_skill_scope.py`：新增入参专值 + 一个分支；四入口调用点不动。
- `src/domains/runtime/pipeline/event_pipeline.py`：门禁 helper 与调用点；派发其余逻辑不变。
- 关联仓库 `grape-dev/grape-admin`：无该判官/门禁代码（`grep scene_scope` 0 命中），零改动。

## 风险与缓解

| Risk | Impact | Mitigation |
|------|--------|-----------|
| 把「无场景」放宽成所有场景 | High | 仅 `scene_scope is None` 触发；`""`/缺省仍 repo；用例锁定两种入参 |
| `evt` 无场景 = 门禁永远放行 | High | 缺失时回读 Issue 真实绑定；已绑定仍 repo 口径（用例覆盖） |
| DB 抖动导致误放行 | Med | 三态中 `None`（未判定）不当作无场景 |
| 四入口回归 | High | 不改调用点、不改默认值；#1758 用例回归 |