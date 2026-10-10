# Spec: Issue #282 — hermes 引擎 1200s 后超时失败（全局超时对齐）

> 来源：Issue Agent Spec（2026-10-10）| 评级 ready | 目标分支：`local-liufei`

## 目标
消除 hermes ACP 引擎独有的全局墙钟超时硬切：`src/agent/hermes_acp.py` 内循环全局上限从写死倍率 `max_exec_seconds * 2`（=1200s）对齐为外层任务总上限 `RUNTIME_TASK_TIMEOUT`（默认 3600s，同源同默认值）。

## 根因
- hermes（`src/agent/hermes_acp.py`）：`_GLOBAL_TIMEOUT = max_exec_seconds * 2 = 1200s` 写死，20 分钟一到即 `stopReason=timeout` 强切，即使输出正常滚动（实测任务 9620：786 条消息、20 分 13 秒被切）。
- opencode（`src/agent/executor.py`）：仅静默超时（无输出兜底），总时长由外层 `RUNTIME_TASK_TIMEOUT=3600` 约束。

## 改动范围
- In：`src/agent/hermes_acp.py` 全局超时取值逻辑（新增 `_global_timeout_for()`，读 `RUNTIME_TASK_TIMEOUT`，默认 3600，下限保护 `max_exec_seconds`）；触发日志打印实际生效上限与来源；新增 `tests/test_hermes_global_timeout.py`。
- Out：opencode/codearts 超时路径、外层 `src/runtime/agent.py` 看门狗、场景引擎绑定、`_SILENT_TIMEOUT`(300s)/`_TEXT_SILENT_TIMEOUT`(60s) 语义（保持不变）。

## 验收标准
- [x] 持续正常产出超过 1200s 的任务不再被 20 分钟硬切（测试：max_exec=1 持续输出 3s > 旧上限 2s，正常完成）
- [x] 全局上限与 `RUNTIME_TASK_TIMEOUT` 对齐且配置可生效（测试：env=2s 生效、非法值回退 3600、下限保护）
- [x] 静默超时回归：无输出场景 300s 静默超时仍触发（测试：可控时钟验证）
- [x] 自动化测试覆盖持续输出不超时 / 静默超时仍生效 / 配置生效三类用例
