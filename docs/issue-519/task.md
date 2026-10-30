# Task: Issue #519 — bot token 健康检查异步化

## 切片 1（红）：async 行为契约测试

- [x] `TestVerifyRepoBotTokensAsync` 测试类：started 日志 / 全覆盖分批 / asyncio.sleep / 批内并发 / 事件循环不阻塞 / 无 time.sleep / loop await 调用 / 异常隔离
- [x] 对旧同步实现跑 → 红（await 非 awaitable / started 日志缺失）
- [x] 提交 `test(#519)`

## 切片 2（绿）：最小实现

- [x] `_verify_repo_bot_tokens` → `async def` + `asyncio.to_thread(_verify_single_repo_token, ...)` 批内 `asyncio.gather` 并发
- [x] 批间隔 `await asyncio.sleep(REPO_BATCH_INTERVAL)`
- [x] started 日志（含仓库数）
- [x] `bot_token_health_loop` 调用处加 `await`
- [x] 新测试全绿
- [x] 提交 `feat(#519)`

## 切片 3：回归

- [x] `pytest tests/test_bot_token_health.py` 全量绿
- [x] 相邻模块 smoke（test_api_main* / admin 相关引用点不受影响）

## 收尾

- [x] 自审查（5 轴 + pre-pr 检查）
- [x] push feat/issue-519 → PR（base=weekly-09）→ Issue 评论 → A2A QA → tasks complete
