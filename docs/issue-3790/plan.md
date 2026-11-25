# Plan: Issue #3790

## 现状链路（目标分支 arch-weekly-11-ljt 实测）

1. `event_pipeline._scene_pool_gate`（4245）→ `engine_select.get_scene_pool_engine_ids`（1186）。
2. `get_scene_pool_engine_ids` 在 `scene_exists_in_repo=False` 时仅 `log.warning`（1222），
   不向调用方暴露来源；随后 `get_scene_engine_binding` 查不到节点 → 返回 `""`，
   派发按「无绑定 → 全量广播」继续，若引擎侧也无候选则统一报「引擎全部离线」。
3. `_enqueue_pipeline_task`（4279）虽有 `pool_engine_source` 形参，但：
   - 主派发调用点（3211）把来源硬编码为 `auto`/`scene`，会覆盖任何上游来源；
   - 其它调用点不传来源，4352-4354 按 scene 上下文推导；
   - 最内层 `enqueue_task(...)`（4553）根本没传 `pool_engine_source` → DB 列恒空。
4. 事件 payload 在门禁之前已 INSERT（3031/3074），来源解析后无回填通道。

## 改动

| # | 模块 | 改动 |
|---|------|------|
| 1 | `engine_select.py` | 新增 `POOL_SOURCE_SCENE_MISSING="scene_missing"`；`get_scene_pool_engine_ids(..., source_out=None)` 在仓内不存在时写出来源；新增 `_alert_scene_missing()` 按 (repo_key, scene_id, agent_type) 窗口收敛并输出结构化单行告警 |
| 2 | `event_pipeline.py` | `_scene_pool_gate` payload 带 `pool_engine_source`；主派发/订阅补派/重派/智能推荐重派显式透传；`_enqueue_pipeline_task` 内层 `enqueue_task` 补 `pool_engine_source`；scene_missing 时回填事件 payload、失败文案带原因码 |
| 3 | `event_store.py` | 新增 `merge_event_payload(seq, extra)`（读-改-写合并 payload） |
| 4 | `mgmt/pipeline_config.py` | 新增 `scene_pool_binding_warning(scene_id, repo_key)`（复用 `scene_exists_in_repo`） |
| 5 | `mgmt/user/db.py` | `set_repo_scene` 写前校验、告警并返回提示文案（不阻断） |
| 6 | `mgmt/routes/templates.py` | 模板建仓回执在场景仍未登记时把提示并入 `host_warning` |
| 7 | `tests/runtime/test_issue_3790_scene_missing_source.py` | 新增回归测试 |

## 兼容性

- `get_scene_pool_engine_ids` 返回语义（JSON / `""` / `None`）与既有调用点完全不变；
  `source_out` 为可选关键字参数，默认不写。
- `set_repo_scene` 由 `None` 返回改为提示文案（`""` = 无提示），既有调用点均忽略返回值。
- 事件 payload 回填仅在 `scene_missing` 时发生（其它来源已有 `agent_tasks` 列，避免无谓写放大）。
- 告警收敛状态为进程内状态，进程重启后窗口重新开始（可接受）。

## TDD 切片

1. 红：`get_scene_pool_engine_ids` 对仓内不存在场景不写 `source_out` → 绿：常量 + out 参数。
2. 红：`_scene_pool_gate` payload 无来源 → 绿：门禁回传。
3. 红：`_enqueue_pipeline_task(pool_engine_source="scene_missing")` 落库被推导成 `scene` → 绿：显式透传 + 内层 enqueue 补参。
4. 红：连续 N 次缺失告警输出 N 行 → 绿：窗口收敛。
5. 红：事件 payload 无 `pool_engine_source` → 绿：`merge_event_payload` + 回填。
6. 红：`set_repo_scene` 对仓内不存在场景无提示 → 绿：写侧校验（同时修复 #541 该用例）。