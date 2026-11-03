# Plan: Issue #653

## 改动方案（单点收口 + 严格模式开关）

### 1. src/api/pipeline_config.py
- 新增 `get_scene_engine_binding(scene_id, agent_type, repo_key, user_id) -> dict`
  返回 `{"status": "no_scene"|"no_node"|"unbound"|"bound", "engine_ids": [...]}`：
  - no_scene：get_pipeline_config 为 None 或查询异常
  - no_node：场景存在但无该 agent 节点
  - unbound：节点存在但 engine_runtime_ids 为空（→ 广播，行为不变）
  - bound：节点显式配置了非空 engine_runtime_ids（→ 硬约束）
- `get_scene_engine_ids` 改为薄包装（返回 engine_ids），签名/行为向后兼容。

### 2. src/runtime/engine_select.py
- `_DEFAULT_CFG` 新增 `"strict_scene_engines": true`（engine-selection.json 可显式关闭）。
- `get_scene_pool_engine_ids` 重构解析段：
  - bound：查引擎详情 → 健康过滤 → JSON 名单；**成员为空（引擎行不存在/被删）→ None**；
    全部不健康 → None（原 #259 语义）。
  - unbound / no_node：非 SRE → ""（广播不变）；SRE → None（原语义）。
  - 场景查询异常：strict → None（fail-closed）；非 strict → "" + warning（原行为）。
- `scene_dispatch_failure_reason` 增补 bound-但引擎缺失 / 查询失败两种原因文案。
- `pick_engine` 切到 `get_scene_engine_ids` 包装（行为不变：unbound 才回退 fallback_runtime_id）。

### 3. src/runtime/service.py
- `enqueue_task` 入队前置校验：pool_engine_ids 为 JSON 空数组 "[]" 时——
  source=auto → 降级 "" 广播 + warning；其余（scene/历史）→ ValueError 拒绝入队（#253 宁失败不跑飞）。

### 4. src/api/event_pipeline.py / routes/webhook.py / pipeline_executor.py
- 各派发路径已统一走 `get_scene_pool_engine_ids` 并处理 None（#259/#541），本次收口后自动获得新语义；
  补充 a2a 路径 get_issue_scene 异常告警日志（排查静默回退路径）。

### 5. 帮助文档（联动 #659）
- help/usage/runtimes.md 新增「指定引擎执行（engine_runtime_ids）」章节：硬约束语义、
  空候选报错行为、strict_scene_engines 开关、回退告警。

### 6. 测试 tests/test_issue_653_engine_binding.py
- bound → pool 限定 / 引擎行缺失 → None / 全不健康 → None
- unbound / no_node / no_scene → ""（行为不变）
- 严格模式关闭 → bound 解析为空回退 "" + 告警日志
- enqueue_task "[]" 校验（scene 拒绝 / auto 降级）
- scene_dispatch_failure_reason 新文案
- 回归：test_engine_select / test_issue_259_offline_dispatch / test_issue_541_template_scene_id

## 风险
- 已配置但空候选从静默回退改为报错 → 仅影响显式配置节点，未配置仍广播；开关可回退。
