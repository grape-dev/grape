# Plan: Issue #282

## 任务分解
1. `src/agent/hermes_acp.py`：新增模块级 `_global_timeout_for(max_exec_seconds)`——读 `RUNTIME_TASK_TIMEOUT`（与 `src/runtime/agent.py` `_env_float("RUNTIME_TASK_TIMEOUT", 3600)` 同源同默认），解析失败回退 3600，`max(env, max_exec_seconds)` 下限保护。
2. 内循环 `_GLOBAL_TIMEOUT = max_exec_seconds * 2` → `_GLOBAL_TIMEOUT = _global_timeout_for(max_exec_seconds)`；触发日志补实际生效上限值与来源（env / default）。
3. 新增 `tests/test_hermes_global_timeout.py`：
   - 值逻辑 5 例（默认对齐 3600 / 废弃 2× 倍率 / env 覆盖 / 下限保护 / 非法值回退）；
   - 行为 3 例（fake hermes ACP 子进程走完整 stream_execute）：持续输出超过旧 2× 上限不硬切、env 配置生效触发 timeout、无输出静默超时仍兜底（FakeTime 可控时钟加速 300s 窗口）。
4. 回归：`tests/test_runtime_hermes.py`、`tests/test_agent_executor.py`、`tests/test_executor_silent_extend.py`。

## 验证结果
- 新增 8/8 通过；
- 回归 59 通过、3 失败——经 stash 对照验证为基线（origin (integration branch)）既有失败（`src/agent/sandbox` mock 缺 `build_bwrap_cmd` 属性），与本改动无关。

## 风险
- 下限保护确保全局上限始终 ≥ 静默窗口，配置异常不会破坏静默兜底；
- 静默超时/文本静默超时路径零改动。
