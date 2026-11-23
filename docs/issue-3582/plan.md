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

## QA 复审修订（2026-11-20，PR #1871）

针对 QA 两轴审查意见的处理：

1. **PEP8 E302**：删除 `scenes.py` 中已无调用的私有兼容委托
   `_scene_node_scope_check` / `_attach_scene_node_scope_checks`（grep 确认
   src/tests 仅定义处引用），同时消除 `pin_scene` 与顶层函数间空行不足。
2. **AC1 标识口径**：`valid_targets` 收录有效节点的 `id` 与 `tag` 别名
   （不同时同时收录、去重），与 `node_matches_agent_role` 的「id 优先、tag
   兜底」一致；新增 T7 覆盖 id ≠ tag 场景（如 `coding` / `coding-agent`）。
3. **Objective 清单语义**：`nodes` 明确为全量场景定义（含 disabled/
   unresolvable，展示/诊断用，不是可用清单）；新增 `available_nodes`（可用
   节点）与 `valid_targets`（可用目标别名），并在 scenes detail OpenAPI
   description 与 docstring 中写明。按可用清单交办均不会 409。
4. **AC3/AC5 路由级覆盖**：新增 T9（GET /api/scenes/{scene_id} 的
   available_nodes/valid_targets 与 handoff 同源）、T10（POST /api/a2a 真实
   409 响应含 conflict_reason + valid_targets，覆盖 not_in_scene 与
   node_disabled）。
5. **冲突归因收敛**：`resolve_scene_targets` 直接产出 `target_reasons`
   （node_disabled / skill_unresolvable），`_scene_node_check` 仅做
   `valid_targets` 命中判定与原因投影，删除 webhook 内二次遍历节点的
   `node_matches_agent_role` 判定。
6. **未解析口径注明**：技能名不在 skill 表时 `scene_skill_scope.evaluate`
   按存量兼容返回 ok=True（source=unresolved），此类节点仍进入可用清单；
   新增 T8 固化该口径。

## QA 复审回归修订（2026-11-23，PR #1871 第二轮）

针对 QA 新基线两轴复审 2 阻塞 + 2 部分的处理：

1. **组织/存量场景回归（阻塞，#955/#2118）**：`_dispatchable_role` 复用
   `node_agent_role`（id 优先）导致 `id=org_intake/tag=issue` 节点被整体
   丢弃、`valid_targets=[]`、POST /api/a2a 交办 issue/coding/qa 全 409。
   改为 `_dispatchable_roles`：节点 `id` 与 `tag` 中**任一**落在
   `DISPATCHABLE_AGENTS` 白名单的名字均产出目标；`_scene_node_check`
   恢复基线兼容的 `node` 键与可用节点 id 直达别名（org_* 按 id 命中），
   `tests/runtime/test_issue_2118_org_pipeline_flow.py` 原样通过；
   新增 T13 组织场景路由级用例（POST issue/coding/qa 不 409、sre 409）。
2. **409 契约回归（阻塞）**：恢复经 `_a2a_skip_response` 的 409 组装——
   `ok/reason/detail/agent/handoff_result/source_seq` 字段齐全；`skipped`
   **保持稳定枚举 `not_in_scene`**（旧消费方与 help 文档按此字面判定），
   粒度归因（node_disabled / skill_unresolvable）放 `conflict_reason`；
   同步更新 T10 断言。
3. **AC4 跨进程缓存（部分，接受为已知限制）**：写路径（set_scene_nodes /
   scene_nodes_write_by_id）已在本进程立即失效 `get_pipeline_config`
   缓存；webhook（event 进程）与 scenes detail（mgmt/api 进程）各持进程级
   缓存，跨进程场景定义变更后的最长陈旧窗口仍为 TTL 60s（无跨进程失效
   通道）——已在 `scene_targets.py` 模块 docstring 明确登记为已知限制，
   同进程内两处读取口径恒一致，跨进程 TTL 窗口后自动收敛。
4. **available_nodes 与 valid_targets 一致性（部分，随 1 修复）**：
   组织节点按 tag 产出角色后同时进入 `available_nodes` 与 `valid_targets`
   （此前 org 节点因角色为空被整体跳过，两侧清单与实际接受集合不一致）；
   T13 断言组织场景下两清单与 POST 接受集合一致。
