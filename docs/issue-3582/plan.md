# Plan: Issue #3582 — a2a handoff 409 与 scenes detail 节点清单口径不一致

> Spec 来源：Issue 评论 #202434358（Developability: ready）｜基线分支：weekly-mys

## 需求摘要

`scenes detail` 返回的节点清单与 `a2a handoff` 校验的目标集合口径不一致，
调用方按清单交办会收到 409 且无法自愈。收敛两处到同一份场景定义（同一解析
函数、同一 enabled/resolvable 过滤口径），并让 409 响应携带可机读的冲突
原因与当前有效目标清单。

## 方案设计

### 单一数据源

新增共享模块 `src/api/scene_targets.py`：

- `resolve_scene_targets(scene_id, repo_key, cfg=None)`：唯一解析函数。
  节点来源统一走 `get_pipeline_config`（DB 项目场景行 → 市场官方模板 →
  pipelines.json → default，进程级缓存 + 写路径失效链路）；
- 过滤口径统一：`enabled`（节点未启用不在有效清单）+ `resolvable`
  （`scene_skill_scope.evaluate` 单一判定器）；
- 产出 `nodes`（附加 resolvable/scope_check，展示用）、`valid_targets`
  （有效目标清单）、`node_status`（conflict_reason 归因）。

### 两处调用收敛

| 调用方 | 改动 |
|--------|------|
| `scenes detail`（`src/domains/mgmt/routes/scenes.py::get_scene_detail`） | 改用 `resolve_scene_targets`（复用已解析 cfg，避免会话绑定二级解析漂移）；响应新增 `valid_targets` 字段，既有字段不变 |
| `a2a handoff` 校验（`src/domains/runtime/routes/webhook.py::_scene_node_check`） | 改用同一函数；`in_scene` = 目标命中 enabled+resolvable 节点；返回 `valid_targets` + `conflict_reason` |

### 409 结构化响应

`POST /api/a2a` 场景节点校验失败时，响应体在既有字段（`skipped`/`scene`/
`message`/`handoff_result`）之外新增：

- `conflict_reason`：`not_in_scene`（目标不在场景）/ `node_disabled`
  （节点被禁用）/ `skill_unresolvable`（节点技能 scope 冲突不可派发）；
- `valid_targets`：当前有效目标清单（与 scenes detail 同源产出）。

### 范围外

- #3528（handoff 幂等键）不合并处理。

## 任务拆解

1. [x] 新增 `src/api/scene_targets.py`（解析 + enabled/resolvable 过滤）
2. [x] `scenes detail` 收敛调用 + 响应新增 `valid_targets`
3. [x] `webhook._scene_node_check` 收敛调用 + 409 结构化
4. [x] `scenes.py` 私有辅助函数改为兼容委托（防口径漂移回潮）
5. [x] 新增测试 `tests/mgmt/test_issue_3582_scene_targets.py`（T1-T6，
   覆盖同源一致 / 合法节点全放行 / 409 reason+targets / 禁用同步拦截 /
   不可解析同步拦截 / 场景变更两处同步）
6. [x] 回归既有测试（#568 / #1721 / #1758 / a2a_scene_check 等全通过）

## 验收对照

- 同一场景下 scenes detail `valid_targets` 与 handoff 接受集合一致（T1/T6）
- 按 scenes detail 清单对每个合法节点 handoff 均不 409（T2）
- 非法/冲突目标 409，响应含 conflict_reason + valid_targets（T3-T5）
- 场景定义变更后两处同步生效（同一解析 + 同一缓存失效链路，T6）
