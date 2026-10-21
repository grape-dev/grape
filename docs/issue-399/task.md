# Task: Issue #399

- [ ] T1 config.py 新增 `CHAT_STREAM_TIMEOUT`/`CHAT_STREAM_HEARTBEAT_INTERVAL` 常量 + `get_chat_stream_timeout()` 动态读取
- [ ] T2 chat.py `_poll_db_stream`：替换 120s deadline + 15s 心跳 + 动态超时文案
- [ ] T3 chat.py `event_stream`：短超时心跳 + 总超时（配置）+ 动态超时文案
- [ ] T4 新增测试 `tests/test_chat_stream_timeout_399.py`（配置读取 / DB 轮询心跳 / SSE 心跳）
- [ ] T5 回归相关现有测试（test_config.py、test_chat_routes.py 等）
- [ ] T6 自审查 → 提交 → PR（base=功能分支）
