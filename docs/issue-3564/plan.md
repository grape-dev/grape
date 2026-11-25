# Issue #3564 — 方案设计

## 数据流
1. **场景配置**：`SceneNodeInput.engine_priorities` 随 `nodes_json` 落库；
   `_validate_scene_engine_ids` 校验键属于 `engine_runtime_ids`、值 1..9。
2. **解析**：`pipeline_config.get_scene_engine_binding` 在 `bound` 时返回归一化
   `engine_priorities`（只保留绑定引擎，非法值丢弃，空 dict=未配置）。
   `engine_select.get_scene_engine_priorities` 输出稳定 JSON（不做在线过滤）。
3. **入队**：`service.enqueue_task` 新增 `engine_priorities` 参数；未显式传且
   `pool_engine_ids` 非空时，按任务 env `GRAPE_SCENE_ID` 解析并写入
   `agent_tasks.engine_priorities`。
4. **认领**：`claim_by_tag` 在健康分检查之后、`break` 前调用
   `_claim_priority_gate`：
   - 空映射 → 放行（旧行为，无日志）；
   - 超时（>=120s）→ 放行并记录 bypass；
   - 存在 `p' < 本机 p` 且 `_engine_claimable_now`（`_check_pool_engines_reachable`
     + `_real_active_count < max_concurrent`）→ 跳过本任务尝试下一个并记录决策日志；
   - 否则放行。全部在既有 `with db_lock` 临界区内，原子 UPDATE 不变。
5. **UI**：`DashboardView` 引擎绑定弹窗对已勾选引擎显示高/中/低下拉（1/5/9），
   确认时只保存选中引擎的映射；创建/编辑/草稿/复制均透传并清洗孤儿键。

## 迁移
`src/api/db_setup.py` 新增 `register_migration("202611240016", ...)`：
`agent_tasks.engine_priorities TEXT DEFAULT ''`（`_ensure_column` + `allow_copy`/`strict`）。
fresh DB 由 `src/domains/runtime/schema_ddl.py` 建列；跨库视图跳过 ALTER。

## 对外接口
- `get_scene_engine_binding(...)` 返回值新增 `engine_priorities`（向后兼容新增键）。
- `get_scene_engine_priorities(...)` 新函数，未配置返回 `""`。
- `enqueue_task(...)` 新增可选 `engine_priorities=""`。

## 测试
`tests/runtime/test_issue_3564_engine_priority.py`（sqlite 内存库）：
高优空闲跳过 / 满载 / 离线 / 心跳过期 / 无 tag / scope 不符 / 未配置回归 /
未配置按最低档 / 超时 bypass / 同档不跳过 / 高优自身认领 / 场景解析 / JSON 输出 / 列清单。
