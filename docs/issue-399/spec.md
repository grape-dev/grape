# Spec: Issue #399 — chat agent 超时可配置 + 等待期中间反馈

> 完整 Spec 见 Issue #399 评论（[issue agent] 发布）。本文件为开发侧摘要。

## Objective
将 chat agent SSE 流式响应中硬编码的 120s 超时改为可配置（`CHAT_STREAM_TIMEOUT`，默认 300s），并在等待期间每 15s 向用户推送「思考中」心跳事件。

## Scope (In)
- `src/api/routes/chat.py`：`chat_task_stream` 两处 120s 硬编码（`_poll_db_stream` deadline、`event_stream` `asyncio.wait_for` timeout）
- `src/config.py`：新增 `CHAT_STREAM_TIMEOUT`（env，默认 300）+ `CHAT_STREAM_HEARTBEAT_INTERVAL`（env，默认 15）+ `get_chat_stream_timeout()` 动态读取 `server-config.json` 的 `chat_stream_timeout`
- 心跳事件复用现有 SSE 事件 type「思考中」

## Out of Scope
- LLM 调用本身超时、前端 UI 改造、跨机器调度根因修复。

## Acceptance Criteria
- `CHAT_STREAM_TIMEOUT` 可通过环境变量配置，默认 300s；`server-config.json` 可动态覆盖（无需重启）
- DB 轮询/SSE 等待期间每 15s 推送心跳，超时文案动态显示配置时长
- 复现原 120s 超时场景默认配置下不再被杀死
- 现有测试通过 + 新增测试覆盖配置读取与心跳推送
