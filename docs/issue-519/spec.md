# Spec: Issue #519 — bot token 健康检查异步化，消除每小时 74 秒全站卡顿

> 来源：Issue Agent Spec（已发布于 Issue 评论）+ Coding Agent 代码考察确认 | 2026-10-29

## 背景与根因

- 现象：每小时一次全站卡顿约 74 秒（nginx 499、chat WebSocket 3003、日志断流），双主控轮流出现。
- 根因：`src/runtime/bot_token_health.py` 的 `bot_token_health_loop()` 是 async 协程，但其调用的 `_verify_repo_bot_tokens()`（src/runtime/bot_token_health.py:122）是同步函数：对 249 个仓库级 token 逐个同步 HTTP 验证（`_verify_single_repo_token` → `create_provider().verify_token()`，同步 requests 调用），批间 `time.sleep(1)`（每批 10 个、约 25 批）。同步 IO + `time.sleep` 长时间占住 asyncio 事件循环，API 服务在检查窗口内无法响应任何请求。

## 需求（用户可见行为）

1. 检查周期内 API 响应无卡顿：事件循环不再被仓库级 token 检查阻塞。
2. bot token 验证功能行为不变：仍覆盖全部仓库级 token（401 标记 expired / 200 清除 / 超时 5xx 不标记）。
3. 检查窗口缩短：批内 10 个 token 并发验证。
4. 可观测性：检查任务启动时打 `started` 日志（含仓库数）。
5. 双主控均生效（分布式锁逻辑不变，无需额外改动）。

## 范围

- In：`src/runtime/bot_token_health.py` 仅此一个文件（`_verify_repo_bot_tokens` 改 async + `bot_token_health_loop` 调用处加 await）；`tests/test_bot_token_health.py` 新增测试类。
- Out：`_verify_global_bot_token`/`_verify_bot_token`（仅 2 个全局 token，影响小）；VCSProvider.verify_token 异步化（保持同步，零侵入）；常量调整；DB schema。

## 方案（Spec 方案 B）

1. `_verify_repo_bot_tokens()` → `async def`：
   - DB 查询部分保持同步（耗时极短，可接受）；查询失败/空列表提前 return 不变。
   - `started` 日志（含仓库数）移到查询成功后（早于原 `checking` 日志位置，保证无 token 时也有探活开始痕迹可观测）。
   - 分批循环改 async：批内 `await asyncio.gather(*(asyncio.to_thread(_verify_single_repo_token, repo_key, provider) for ...))` 并发；单任务异常由 `_verify_single_repo_token` 内部 try/except 吞掉（现状即如此，`gather` 默认不 return_exceptions 也安全）；但为防御线程池包装层本身抛错（如 shutdown 竞态），gather 逐结果检查记 warning，不中断整轮检查。
   - 批间隔 `time.sleep(REPO_BATCH_INTERVAL)` → `await asyncio.sleep(REPO_BATCH_INTERVAL)`。
2. `bot_token_health_loop()`：调用处 `_verify_repo_bot_tokens()` → `await _verify_repo_bot_tokens()`。
3. `_verify_single_repo_token()` 保持同步不变：内部 DB 读 + `verify_token()` 同步调用移入线程池执行，不阻塞事件循环；`db_lock`（threading.Lock）在线程池中安全。

### 测试 seam（公开接口/行为边界）

- seam = `await _verify_repo_bot_tokens()` 协程的对外行为：不测内部线程调度细节。
- mock 边界：`_verify_single_repo_token`（模块内单仓验证函数，线程池执行单元）+ DB 查询层（`src.session_manager` 的 db/db_lock）。不 mock `asyncio.to_thread` 本身（测真实并发路径，行为级断言「批内并发 + 批间 sleep + 全覆盖」）。

## 任务拆解

| # | 任务 | 提交 |
|---|------|------|
| 1 | 红：新增 async 行为测试（started 日志、批内并发、asyncio.sleep、全覆盖、事件循环不阻塞、await 调用链）——对旧同步实现变红 | `test(#519)` |
| 2 | 绿：`_verify_repo_bot_tokens` 改 async（to_thread + gather + asyncio.sleep + started 日志），`bot_token_health_loop` 加 await | `feat(#519)` |
| 3 | 回归：全量 `tests/test_bot_token_health.py` + 相邻模块测试 | — |

## 验收标准

- [x] `_verify_repo_bot_tokens` 为 `async def`，函数体内无 `time.sleep`
- [x] 批内 10 个 token `asyncio.gather` 并发验证
- [x] 批间隔 `await asyncio.sleep`
- [x] started 日志含仓库数
- [x] `bot_token_health_loop` 中 `await` 调用
- [x] token 验证功能不变（覆盖全部仓库级 token、401/200/超时逻辑不变、`_verify_single_repo_token` 未改）
- [x] 检查期间事件循环不阻塞（测试：检查并发运行时事件循环心跳正常）
- [x] 现有测试全通过 + 新增测试覆盖

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 线程池内 DB 操作（db_lock） | db_lock 为 threading.Lock，线程池同步加锁安全；批内 10 个各自操作不同 repo_key 行 |
| gather 单任务异常传播 | `_verify_single_repo_token` 内部已 try/except；防御性逐结果检查 warning |
| `asyncio.to_thread` 需 Py≥3.9 | 项目 Python 3.11，且代码已用 `asyncio.to_thread`（src/api/main.py:315） |
| 分布式锁 TTL | LOCK_TTL=3000s 远大于检查时间，无影响 |
