# Spec: Issue #653 — 场景节点 engine_runtime_ids 派发硬约束

> 目标分支：weekly-08-mys（跨流水线移交后基线，替代原 Spec 的 weekly-09，其余内容不变）

## Objective
把场景节点级 `engine_runtime_ids` 变为派发硬约束：配置后任务只允许派给清单内引擎；候选为空时显式报错，不再静默回退全局池。

## Root Cause
- `get_scene_engine_ids`（pipeline_config.py）对「节点不存在/查询失败」与「未配置」均返回 `[]`，语义混同。
- `get_scene_pool_engine_ids`（engine_select.py）空值语义：`""` = 全量广播。已配置节点在解析落空（引擎行不存在/场景查询异常）时也走 `""` → 全局池 ECS 抢任务（#86/#88/#89 事故形态）。
- 严格模式开关缺失：依赖全局池兜底的存量场景无法显式选择回退。

## In Scope
- `get_scene_engine_binding` 新 API：区分 no_scene / no_node / unbound / bound 四态。
- `get_scene_pool_engine_ids` 单点收口：bound 解析为空 → None（显式报错）；unbound/no_node → ""（广播不变）；场景查询异常在严格模式下 fail-closed。
- 严格模式开关：`engine-selection.json` 新增 `strict_scene_engines`（默认 true）。
- `enqueue_task` 入队前置空候选校验（防御 "[]" 绕过）。
- 帮助文档补充 engine_runtime_ids 语义（联动 #659）。
- 单测覆盖全部分支。

## Out of Scope
- #24 / #250 / #310 / #605 各自独立处理；引擎健康度模型改造。

## Acceptance Criteria
- 配置 `engine_runtime_ids` 的节点派发只落指定引擎或显式报错（含原因），绝不落全局池。
- 空候选报错信息含场景/agent/引擎清单；无静默全局池回退。
- vod 与非 vod 通道行为一致。
- 未配置节点行为不变（全量广播）。
- 严格模式默认开启、可显式关闭（回退伴随告警日志）；帮助文档同步。
- 新增单测全分支通过；现有测试全部通过。
