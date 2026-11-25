# Issue #3909 需求文档（Spec 摘要）

## 目标

三库拓扑下，mgmt 进程读取 runtime 域表 `scene_engine_detach_log` 不再恒 1146；场景详情
`detached_engines` 与巡检任务 `engine_detached` 能读到真实解绑事实；「查询失败」与「无解绑」
在接口与 UI 上可区分；mgmt 一键加回不再因错库连接失败。

## 现象（生产实测）

`{service_name="grape-mgmt"} |= "get_detached_engine_hints"` = 40 条 WARNING/小时，文案恒为
`(1146, "Table 'grape_prod_mgmt.scene_engine_detach_log' doesn't exist")`。

## 根因

1. 读路径 `src/domains/runtime/engine/engine_select.py::get_detached_engine_hints` 用
   `src.kernel.session_manager` 进程主库；mgmt 进程主库 = `grape_prod_mgmt`，该表 owner=runtime。
2. 写路径（`_detach_dead_engine_bindings` 审计 INSERT）只在 worker（runtime 域进程）执行，
   落库正确 → 「有写入、无读侧」。
3. 异常被 `log.warning + return []` 吞掉，「查询失败」与「无解绑」同形。

## 范围（本条收口）

- 读路径改按域取连接（`db_registry.cursor_for("runtime")`），单库/测试拓扑回落进程主库零回归。
- 失败返回显式降级形态（`degraded` + `reason`），3 个消费点契约同步。
- 同族错库连接一并收口：`recovery.recover_engine` / `_readd_scene_bindings` /
  `_recover_in_tx`（`agent_runtimes` / `agent_tasks` / `scene_engine_detach_log` 均 owner=runtime）。
- 失败日志补 `route` / `target_db` / `pid` / `host`。

## 非目标

不改表归属（`scene_engine_detach_log` 仍归 runtime）；不为规避 1146 在 mgmt 库补建同名表；
不改解绑/回绑业务语义。

## 验收

- 三库拓扑：runtime 侧写入未回绑审计行 → mgmt 场景详情 / 任务能读到；`rebound_at` 后不再展示。
- 单库/测试拓扑零回归。
- 降级路径可测：`DomainUnavailable` / 查询异常 → 接口带降级标记且仍 200。
- mgmt 一键加回走 runtime 事务，域不可用时返回显式失败原因（不再 503 撞库）。
- 新增 `tests/runtime/test_issue_3909_detach_hint_topology.py`；本 PR 涉及的
  `engine_select.py` / `recovery.py` 计数只降不升并刷新基线（`transient_retry.py` 的
  #3820/#3822 预存在漂移不在本 PR 吸收，另拆基线 PR 收敛）。
