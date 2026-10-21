# Plan: Issue #400 — chat agent 引擎选择显示异常

## 依赖图

```
engine_select.py (engine_unique_key / dedupe_engines)
        │  供 chat.py / 自身候选收口
        ▼
chat.py GET /config → _backends 去重 + selectedEngine(以 cfg.runtime_id 为权威)
        │
        ├── session_store.py get_or_create_agent_session(runtime_id=...) 指针落库
        └── chat.js 单选渲染 + 顶栏引擎徽标
```

## 实施步骤（按依赖序）

### Step 1. engine_select.py — 唯一键归一与去重
- `engine_unique_key(m: dict) -> str`
  - local: `local:{name}`
  - 其他: `{scope}:{instance_name(name, device_name, credential_id)}`
- `dedupe_engines(members: list[dict]) -> list[dict]`：按唯一键保留首个，日志输出被合并数
- 接入点：
  - `_apply_filters` 尾部去重（resolve_default_engine / resolve_local_engine / pick_engine SRE 路径）
  - `get_scene_pool_engine_ids` healthy 过滤前先去重（广播名单不重复）

### Step 2. chat.py — GET /config 去重 + selectedEngine
- 局部 helper：
  - `_chat_engine_unique_key(b)`（复用 engine_select.engine_unique_key 的键语义，字段对齐后端 dict）
  - `_chat_selected_engine(cfg, backends)`：返回唯一 dict 或 None
- 在返回体加 `selectedEngine`；`_backends` 去重
- 派生 display_name 失败时回退 type/instance 名

### Step 3. session_store.py — 会话指针一致性
- `get_or_create_agent_session(..., runtime_id=0)`：显式传入时优先采用（不再走 pick_engine）
- chat.py 4 处调用传 `runtime_id=runtime_id`（send/stream/_enqueue_chat/submit/send_async 处取到的执行引擎 id）

### Step 4. 前端 chat.js
- `_buildModelPicker`：active 判定改为 selectedEngine.runtime_id 精确匹配（无 ptr 时精确 name 匹配）
- 渲染 `chatEngineBadge`（chat-hdr 内），loadChatConfigData / _saveModelConfig 后刷新
- `_saveModelConfig` 成功后 re-fetch config（权威 selectedEngine）

### Step 5. 样式 chat.css
- `.chat-engine-badge` 小胶囊样式（app.css + apple-theme 兼容）

### Step 6. 测试
- tests/test_engine_select_dedupe.py：unique_key / dedupe / _apply_filters 去重
- tests/test_chat_config_engine_selection.py：GET /config 去重 + selectedEngine 唯一 + 指针一致
- tests/test_chat_session_pointer.py：get_or_create_agent_session(runtime_id=) 落指针
- tests/js/test_issue_400_chat_engine_single_select.js：前端单选渲染

### Step 7. 回归 + 自审查
- 跑 tests/test_engine_select.py test_chat_routes.py test_chat_backend_resolution.py
  test_session_engine_guard.py test_session_manager.py
- 自审查 5 轴（正确性/一致性/范围/测试/兼容）

## 风险

| 风险 | 缓解 |
|------|------|
| get_or_create_agent_session 参数变更影响其他调用方 | 默认 runtime_id=0，行为与原来完全一致 |
| JS 测试环境无法跑真实 DOM | 用 node 模拟 DOM 单测断言 active 判定函数/渲染输出 |
| apple-theme 覆盖 chat-hdr | 徽标样式在 app.css + apple-theme.css 各加一处 |