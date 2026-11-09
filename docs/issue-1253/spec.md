# Spec: Issue #1253 — Hermes 静默杀死阈值 300s → 600s

> 来源：Issue Agent Spec（评论 198716851），开发评级 ready。

## 目标
把 Hermes ACP 的工具静默超时默认值从 300s 提升到 600s，降低长耗时任务（构建、等待外部系统、长推理）在正常进行中被误杀的概率；同时记录治本方案结论。

## 现状
- `src/agent/hermes_acp.py::_silent_timeout_for`：读 `HERMES_SILENT_TIMEOUT`，env 未设置时默认 300s，非法值回退 300s。
- `src/runtime/agent.py::_execute_task`：`_AGENT_SILENT_TIMEOUTS` 仅对 coding/qa 注入 1200s，其余 agent_type 不注入，落到 Hermes 默认 300s。
- `src/agent/executor.py`：`AGENT_SILENT_WINDOW` 默认 `min(300, max_exec)`，与 Hermes 默认对齐。

## 改动点
1. `src/agent/hermes_acp.py`：`_silent_timeout_for` 默认值与异常回退值 300 → 600；同步 docstring 与注释。
2. `src/runtime/agent.py`：`_DEFAULT_SILENT_WINDOW` 300 → 600；同步 #378 注释；coding/qa 映射（1200s）不变。
3. `src/agent/executor.py`：默认 `min(300, max_exec_seconds)` → `min(600, max_exec_seconds)`，保证不超过 max_exec；同步注释与超时总账示例。
4. `tests/test_agent_silent_timeouts.py`：默认值断言 300 → 600；保留 env 覆盖 / 非法回退 / coding、qa 用例。

## 不动
- coding/qa 1200s 差异化阈值不变。
- 全局 `RUNTIME_TASK_TIMEOUT`（3600s）与 executor 进程存活续期机制不变。
- `RUNTIME_TEXT_SILENT_TIMEOUT`（text 静默后「疑似提前结束」判定）不调整。
- `src/runtime/service.py` 中 600s 是 dispatched 任务回收窗口，不属本 Issue。

## 验收
- [ ] 未设置 env 时 `_silent_timeout_for(None)` / `_silent_timeout_for({})` 返回 600.0
- [ ] 非法值回退 600.0；显式 `HERMES_SILENT_TIMEOUT`（1200/180/0）仍优先生效
- [ ] coding/qa 仍注入 1200s，无回归
- [ ] 全局任务超时与存活续期逻辑不受影响
- [ ] 相关单测通过
- [ ] 治本方案结论在 Issue 中记录

## 治本方案结论
- 短期（本 Issue）：默认 300→600，保留 `HERMES_SILENT_TIMEOUT` 任务级覆盖。
- 中期（建议单独立项）：静默阈值纳入 `server-config.json`/`task-timeout.json` 统一配置，消除散落硬编码。
- 长期：Hermes 按最后一次事件活动时间计时 + executor 存活续期兜底（`AGENT_SILENT_EXTEND_CAP`），本质已为活跃度判定；不建议额外心跳协议，固定窗口仅作挂死兜底，总时长由 `RUNTIME_TASK_TIMEOUT` 兜底。