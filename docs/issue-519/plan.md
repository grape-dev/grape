# Plan: Issue #519 — bot token 健康检查异步化

## 改动清单（单文件 + 测试）

### src/runtime/bot_token_health.py

1. `_verify_repo_bot_tokens()`（:122）改 `async def`：
   - 模块 docstring 顶部补 Issue #519 改造说明。
   - DB 查询逻辑不变；查询成功即打 `started` 日志（原 `checking` 日志升级为 started 语义，含仓库数）。
   - 批内循环：`for repo_key, provider in batch` 串行调用 → `await asyncio.gather(*(asyncio.to_thread(_verify_single_repo_token, repo_key, provider) for repo_key, provider in batch))`，逐结果检查异常记 warning。
   - `time.sleep(REPO_BATCH_INTERVAL)` → `await asyncio.sleep(REPO_BATCH_INTERVAL)`。
2. `bot_token_health_loop()`（:331）：`_verify_repo_bot_tokens()` → `await _verify_repo_bot_tokens()`。
3. 模块头 `time` import 保留（`_update_health_status` 仍用 `time.strftime`）。

### tests/test_bot_token_health.py

新增 `TestVerifyRepoBotTokensAsync`（asyncio 测试，mock `_verify_single_repo_token` + DB 层）：

- `test_started_log_with_repo_count`：打 started 日志含仓库数（caplog）。
- `test_covers_all_repos_in_batches`：N=25 仓库全被验证，分 3 批（10/10/5）。
- `test_batch_interval_uses_asyncio_sleep`：`time.sleep` 不被调用，`asyncio.sleep` 被调用 2 次（3 批 2 间隔）。
- `test_batch_concurrency`：批内 10 个验证重叠执行（并发 >1 证明），整体耗时 < 串行。
- `test_event_loop_not_blocked`：检查运行期间，事件循环并发任务正常完成（无长阻塞）。
- `test_no_time_sleep_in_function`：inspect 源码断言 `time.sleep` 不出现在 `_verify_repo_bot_tokens` 中。
- `test_loop_awaits_repo_check`：`bot_token_health_loop` 中以 await 调用（patch `_verify_repo_bot_tokens` 为 AsyncMock 断言 awaited）。
- `test_single_repo_token_error_isolated`：单个验证抛异常不影响其他仓库（异常隔离）。
- DB 层 mock：patch `src.session_manager.db`/`db_lock`（函数内 import，patch 源模块属性）。

### 既有测试兼容

- `TestHealthLoop` 三个 loop 测试未 mock `_verify_repo_bot_tokens`，改造后 loop 内会真调 async 版本 → 这些测试抢锁成功路径会触达 `_verify_repo_bot_tokens` 协程（原来同步调用直接执行、内部 DB 查询失败即 return）。async 版本在无 DB 环境下同样 query failed → return，行为兼容；`test_loop_lock_released_on_exception` 已有 `_verify_global_bot_token` 抛异常路径先行覆盖。跑全量确认。

## TDD 执行顺序

1. **红**：先写 `TestVerifyRepoBotTokensAsync`，对当前同步实现跑 → `TypeError: _verify_repo_bot_tokens() takes 0 positional arguments...`（await 同步返回值 None）或 started 日志缺失 → 红。提交 `test(#519)`。
2. **绿**：改 `src/runtime/bot_token_health.py`，新测试转绿 + 全量回归绿。提交 `feat(#519)`。
3. push → 自审查 → PR → 评论 → A2A QA。

## 回归范围

- `pytest tests/test_bot_token_health.py`（全量）
- `pytest tests/ -k "bot_token"` 相邻
- 视时长选择性跑 `tests/test_api_main*`（启动路径引用 start_bot_token_health，不涉改动）
