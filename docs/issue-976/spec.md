# Issue #976 Spec — hermes ACP tool_call 入参恒为空

> 权威 Spec 由 issue agent 发布于 Issue #976 评论（spec-ready），本文为开发侧落地摘要。

## Objective
修复 hermes ACP 链路 `tool_call` 事件不携带工具入参导致任务日志中 write/patch 入参恒为空的 P1 bug，使 `GET /api/task-logs/{id}/messages` 可完整还原 write/patch 产物。

## 根因（实测确认，hermes 0.20.0）
- 引擎侧：hermes `acp_adapter/tools.py` 对 write_file/patch 等 `_POLISHED_TOOLS` 的 `start_tool_call` 不传 `raw_input`（wire 上 `rawInput=None`），完整入参只体现在 content diff 块（path/oldText/newText）。
- grape 侧：`src/agent/hermes_acp.py` 只读 `rawInput/input/parameters` → 恒为 `{}`；tool 名与 title 皆空时整条 tool_use 事件被静默丢弃。

## Scope (In)
- `src/agent/hermes_acp.py`：ACP 语义读取入参（rawInput → input → parameters → content diff/text 块）；name/title 皆空时输出占位 `{"tool":"unknown","input":{},"title":...}` 事件不丢弃；超限裁剪带 `truncated:true + original_bytes + sha256_prefix`。
- 新增单测：57KB 保真 / 占位事件 / 端到端 messages 还原 / 裁剪契约。

## Out of Scope
- 引擎 acp_adapter 上游补传 raw_input（hermes 为外部 pip 包，不在本仓，PR 内说明）。
- 任务日志存储层体积策略重构；非 hermes/ACP 引擎链路。
