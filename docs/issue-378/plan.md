# 方案设计：Issue #378

## 方案概述
在任务执行入口（`src/runtime/agent.py` `_execute_task`）按 agent_type 注入差异化的静默超时 env（`AGENT_SILENT_WINDOW` / `HERMES_SILENT_TIMEOUT` / `RUNTIME_TOOL_SILENT_TIMEOUT`），底层各 backend 读 env 生效。采用 `setdefault` 语义保留手动调参覆盖能力。

## 实现路径

### 1. src/runtime/agent.py（修改）
- **模块级常量**：新增 `_AGENT_SILENT_TIMEOUTS = {"coding": 1200, "qa": 1200}` 与 `_DEFAULT_SILENT_WINDOW = 300`。
- **`_execute_task` 注入**：在 `env` 构建（env_extra + fresh_env 合并）之后，按 `task.agent_type` 命中映射表时，以 `env.setdefault(key, str(value))` 注入三个 env 变量。setdefault 保证上层显式设置（手动调参）不被覆盖。

### 2. src/agent/hermes_acp.py（修改）
- L448 `_SILENT_TIMEOUT = 300` 写死改为读 env：
  ```python
  _silent_src = (env or {}).get("HERMES_SILENT_TIMEOUT") or \
      os.environ.get("HERMES_SILENT_TIMEOUT", "300")
  _SILENT_TIMEOUT = float(_silent_src)  # try/except 回退 300.0
  ```
- 与 `hwcloud_acp.py` L503-508 `RUNTIME_TOOL_SILENT_TIMEOUT` 模式对齐。

### 3. src/agent/executor.py（修改 — Spec 偏差说明）
- Spec 声称 executor.py「无需改动」，但实际 L734 `_silent_window` 读 `os.environ.get("AGENT_SILENT_WINDOW", ...)`，而 `_execute_task` 注入的是局部 `env` dict（非 os.environ，避免 async 交错竞态，见 agent.py L1774 注释）。
- **最小偏差**：L734 改为 `env.get("AGENT_SILENT_WINDOW", os.environ.get("AGENT_SILENT_WINDOW", ...))`——优先读任务级 env dict（含 env_extra），os.environ 作次级 fallback（兼容手动调参/测试），默认值不变。
- 该 1 行改动向后兼容，使 Spec 的 env 透传链路真正生效。

### 4. src/agent/hwcloud_acp.py（无改动）
已支持 `RUNTIME_TOOL_SILENT_TIMEOUT` env（L503-508），依赖上层注入。

## env 透传链路
```
_execute_task(agent.py)
  env.setdefault("AGENT_SILENT_WINDOW"/"HERMES_SILENT_TIMEOUT"/"RUNTIME_TOOL_SILENT_TIMEOUT", "1200")
  → env_extra=env → _stream_runtime_async → stream_agent(executor.py)
    → env.update(env_extra)  # env dict 含注入值
    → executor.py _silent_window: env.get("AGENT_SILENT_WINDOW") ✅（改动后）
    → Popen env=_proc_env  # 子进程 env
  → _stream_hermes_acp(env=env) → hermes_acp.py: (env or {}).get("HERMES_SILENT_TIMEOUT") ✅
  → _stream_hwcloud_acp(env=env) → hwcloud_acp.py: (env or {}).get("RUNTIME_TOOL_SILENT_TIMEOUT") ✅
```

## 风险与缓解
| Risk | Mitigation |
|------|------------|
| coding/qa 长静默掩盖真挂死 | 全局 RUNTIME_TASK_TIMEOUT=3600s 兜底；超 1200s 仍杀 |
| env 透传断裂 | executor.py 1 行改动补齐（Spec 遗漏点） |
| 手动调参被覆盖 | setdefault 语义，上层显式设置优先 |
