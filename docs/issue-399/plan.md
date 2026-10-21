# Plan: Issue #399

## 方案概述
抽取 120s 硬编码为配置常量（env + server-config.json 动态覆盖），DB 轮询与 SSE 流共用；等待循环周期性 yield「思考中」心跳。

## 实现步骤
1. **config.py**
   - `CHAT_STREAM_TIMEOUT = int(os.environ.get("CHAT_STREAM_TIMEOUT", "300"))`
   - `CHAT_STREAM_HEARTBEAT_INTERVAL = int(os.environ.get("CHAT_STREAM_HEARTBEAT_INTERVAL", "15"))`
   - `get_chat_stream_timeout()`：优先读 `read_server_config()["chat_stream_timeout"]`（int，>0），否则回退 env 常量。
2. **chat.py `_poll_db_stream`**
   - `deadline = _t.time() + timeout_s`（`timeout_s = get_chat_stream_timeout()`）
   - 循环内距上次心跳 ≥ `CHAT_STREAM_HEARTBEAT_INTERVAL` 时 yield `{"type":"思考中","text":"仍在处理，请稍候…"}` 并重置计时
   - 超时文案：`f"查询超时（{timeout_s}s），请重试"`
3. **chat.py `event_stream`**
   - 总 deadline = now + `timeout_s`；`asyncio.wait_for(q.get(), timeout=min(心跳间隔, 剩余时间))`
   - `TimeoutError` → yield 心跳并 continue（总 deadline 到才报 `f"超时（{timeout_s}s 无输出）"`）
4. **测试** `tests/test_chat_stream_timeout_399.py`
   - `get_chat_stream_timeout`：默认 300、server-config 覆盖、非法值回退
   - DB 轮询分支：心跳推送 + 动态超时文案（小超时 + 小心跳间隔）
   - SSE 分支：无输出期间心跳 + 总超时动态文案

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 配置来源 | env + server-config.json | 与 `IM_INSTANCE_TIMEOUT`/`read_server_config` 模式一致 |
| 默认超时 | 300s | 覆盖多数慢查询，可调 |
| 心跳周期 | 15s | 平衡反馈频率与噪声，复用「思考中」type 前端已兼容 |
