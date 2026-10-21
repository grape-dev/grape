# Spec: Issue #378 — 看门狗静默杀死事件针对 coding 与 QA Agent 放宽为 20 分钟

> 完整 Spec 见 Issue #378 评论 id=194639487（Issue Agent 发布）。本文件为开发落盘存档。

## 目标
将 Agent 执行看门狗的「静默无输出杀死」阈值，针对 coding 与 QA 两类 Agent 放宽为 20 分钟（1200s），其余 Agent 保持现有默认值（300s）。

## 范围
- 按 agent_type 差异化：coding=1200s、qa=1200s，其余=默认 300s。
- 覆盖三条静默杀死路径：
  1. `src/agent/executor.py` `_silent_window`（opencode/codearts/codex 通用路径）
  2. `src/agent/hermes_acp.py` `_SILENT_TIMEOUT`（hermes ACP 内循环）
  3. `src/agent/hwcloud_acp.py` `_SILENT_TIMEOUT`（hwcloud ACP 工具静默，已支持 env）

## 验收标准
- coding/qa 静默 300s~1200s 不杀；>1200s 仍杀并落库
- 其他 Agent 零回归；hermes 默认值不变
- 新增单测覆盖 env 注入映射与 hermes 读 env；现有测试通过
