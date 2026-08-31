# web→飞书推送 aiohttp 跨事件循环修复方案

> 版本：v1.0
> 日期：2026-08-31
> 状态：**已分析，待实施**
> 现象：web chat 答复推送到飞书失败，日志 `send_card_to_user failed: Timeout context manager should be used inside a task`
> 关联：`feishu-chat-reuse-web-session.md`（飞书会话复用，已上线）

---

## 1. 现象

- web chat 任务正常完成，回复正常落库、SSE 正常；
- 但推送到飞书这一步失败，日志：

```
[chat_bot_cli_aae1bb5f] send_card_to_user failed: Timeout context manager should be used inside a task
Chat web->feishu push: conv=... open_id=... text_len=87   ← catch 后才打，实际飞书未收到
```

- **影响**：web 侧答复无法主动推送到飞书（用户 web 与飞书不同步时看不到该回复）。不影响 web 本身。

## 2. 调用链与根因

### 调用链（后台线程）

```
web chat submit → ChatTaskManager.submit → threading.Thread(_run)
  → agent_fn()（chat.py 后台线程执行）
    → finally:
      _aio.run(_push_reply_to_feishu(conv, user, text))       ← chat.py:1202
        → _push_reply_to_feishu:
            bm.api.send_card_to_user(ctx, open_id, ...)
              → _ensure_token / get_tenant_token
              → session = await self._get_session()            ← 复用 BotManager 共享 session
              → async with session.post(...) → 报错 ✗
```

### 根因

**跨事件循环复用一个共享 `aiohttp.ClientSession`**：

1. `BotManager.start()` 创建 `FeishuAsync(session=session)`，session 绑定 **uvicorn 主事件循环**（feishu_async.py:1185-1191）。
2. `agent_fn` 运行在 **ChatTaskManager 后台线程**（chat_task_manager.py:130,143）；其中 `_aio.run()` **在线程内新建事件循环**（chat.py:1202）。
3. aiohttp `ClientSession` 非跨线程/跨事件循环安全：在新 loop 里 `async with session.post()` 内部的超时 context 绑定原 loop → 抛 **`Timeout context manager should be used inside a task`**。

> 本质是 aiohttp 的已知限制：session 必须在**创建它的那个事件循环**内使用。

**为什么飞书直连分支没问题**：飞书消息处理（`process_chat_async`）跑在主事件循环的 asyncio task 里，session 与 loop 一致 → 正常。只有 **web→飞书推送**走 `_aio.run` + 后台线程，跨 loop 才炸。

## 3. 修复方案：投递到主事件循环执行推送

**原则**：不在后台线程新建 loop 跑 aiohttp，而是把 `_push_reply_to_feishu` **调度到主事件循环**上的 task 执行，与共享 session 同 loop 一致。

### 方案 A（推荐）：`asyncio.run_coroutine_threadsafe` 投递到主 loop

```python
# agent_fn finally（chat.py 中，后台线程内）
if _final_text:
    try:
        import asyncio as _aio
        bm = api_main.bot_manager            # 主进程单例
        loop = bm.loop if bm and bm.loop else None   # 主事件循环
        if loop:                             # 干净干净调度到主 loop
            asyncio.run_coroutine_threadsafe(
                _push_reply_to_feishu(conv, user, _final_text), loop)
        else:
            _aio.run(_push_reply_to_feishu(conv, user, _final_text))  # 兜底（极罕见）
    except Exception:
        pass
```

要点：
- `BotManager` 需暴露主事件循环引用（`loop`），或从模块级主 session 携带的 loop 获取。
- `run_coroutine_threadsafe` 线程安全，把协程提交到主 loop 排队执行，飞书 API 调用在主 loop 中与共享 session 同 loop → 修复跨 loop 错误。
- 因为是异步不等待（fire-and-forget），**不阻塞后台线程**，也不会等飞书网络往返，性能更优。

### 方案 B（备选）：在 bot 侧封装线程安全推送

- 在 `FeishuAsync`/`BotManager` 提供 `send_card_to_user_safe(...)`，内部用主 loop threadsafe 包装，供任意线程调用。
- 更内聚，但改动面稍大。

### 方案 C（不推荐）：后台线程独立 session

- 在后台线程新建独立 `aiohttp.ClientSession` 并在该 loop 内用完关闭。
- 缺点：每任务建/拆 session、连接未复用、token 缓存逻辑要各线程独立 → 更差。

> 采用 **方案 A**。改动最小、性能最优、与共享 session/主 loop 一致。

## 4. 性能影响评估（结论：无负面影响）

| 维度 | 现状 | 修复后 |
|---|---|---|
| 调用频率 | 任务完成一次，低频 | 同（未变） |
| 事件循环开销 | 后台线程 `asyncio.run` **新建 loop + session 跨 loop 抛错** | 主 loop 复用 session，微秒级入队 |
| 阻塞 | 后台线程同步等（实际异常失败） | **不阻塞**（fire-and-forget） |
| 可靠性 | 必失败（跨 loop）| 稳定送达 |

- 仅"投递回调入队 + 主 loop 一次 HTTP POST"，主 loop 本就常驻，几乎无额外负载。
- 对比现状反而是消除"每次新建 loop + 必然失败"的浪费。

## 5. 涉及文件（预估）

| 文件 | 改动 |
|---|---|
| `src/im/feishu_async.py` | `BotManager` 暴露主事件循环引用（或 `FeishuAsync` 持有原 loop） |
| `src/api/routes/chat.py:1198-1204` | `agent_fn` finally 用 `run_coroutine_threadsafe(..., main_loop)` 替代 `_aio.run` |
| （可选）`src/api/chat_task_manager.py` | 无直接改动（只需确保 agent_fn 拿到主 loop 引用） |

## 6. 验证

1. web chat 完成任务 → 日志无 `send_card_to_user failed`，出现 `Chat web->feishu push` 且**飞书实际收到**卡片（`[来自Web] ...`）。
2. 飞书会话复用（`feishu-chat-reuse-web-session.md`）行为不回归。
3. 高频并发 web chat 时服务稳定、主 loop 无异常。
4. web 回复仍正常落 DB + SSE。

## 7. 测试用例看护设计

> 目标：锁死"推送必须投递到主事件循环执行"这一行为，防止将来退回跨 loop 调用。把核心逻辑抽成可测纯函数/薄封装（如 `_dispatch_to_main_loop(loop, coro)`），便于单测。

### 7.1 单元测试（`tests/test_web_feishu_push.py`）

| 用例 | 断言 |
|---|---|
| 有主 loop 时投递到主 loop | mock `bot_manager.loop`，断言调用 `asyncio.run_coroutine_threadsafe(coro, loop)` 而非 `asyncio.run` |
| 无主 loop 时兜底 `asyncio.run` | mock loop=None，断言不抛且走 `_aio.run` 路径 |
| conv 无 im_channel_id 不推送 | `_push_reply_to_feishu({'im_channel_id': ''}, ..., 'text')` → 不调 `send_card_to_user` |
| conv 有 im_channel_id 且有 open_id → 推送 | mock `send_card_to_user`，断言调用且 text 前缀 `[来自Web] `、截断 1800 |
| 无 open_id 且 im 非 `ou_` → 不推送 | mock `_get_feishu_open_id` 返回空、im 为 `oc_...` → 不推送 |
| 无 app_id / bot ctx 不存在 → 不推送 | mock `get_bot` / im_app_id 查询为空 → 不推送 |
| 推送异常被捕获不抛出 | `send_card_to_user` 抛异常 → 仅 log warning，不向上抛 |

### 7.2 契约/集成（`tests/test_web_feishu_push_contract.py`）

| 用例 | 断言 |
|---|---|
| 后台线程触发时 session 与主 loop 同源 | 在 thread 中调封装，断言 `_get_session()` 返回的 session 归属主 loop（`session._loop 与 main_loop 同一对象`） |
| 推送使用正常 aiohttp 上下文不抛 timeout | 用真实/内存 loop 跑 `send_card_to_user`，断言无 `Timeout context manager should be used inside a task` |
| wrapper 返回后主 loop 已调度任务 | `run_coroutine_threadsafe` 返回 future；`asyncio.wait_for(fut)` 能完成 |

### 7.3 运行级回归（人工/脚本，`scripts/verify_web_feishu_push.sh`）

1. web chat 发一条 → 日志出现 `Chat web->feishu push`、**无** `send_card_to_user failed` / `Timeout context manager should be used inside a task`。
2. 飞书侧实际收到 `[来自Web]` 卡片。
3. 连续多轮 web chat，推送稳定、日志无异常增长。

### 7.4 运行命令

```
python -m pytest tests/test_web_feishu_push.py tests/test_web_feishu_push_contract.py -q
```

## 8. 附：关键代码位置

- `src/api/routes/chat.py:118 just>` `_push_reply_to_feishu`（aiohttp session 使用）
- `src/api/routes/chat.py:1198-1204` `agent_fn` finally（`_aio.run` 调用点）
- `src/api/chat_task_manager.py:130,143` `_run`（后台线程）
- `src/im/feishu_async.py:1185-1191` `BotManager.start`（共享 session 绑定主 loop）
- `src/im/feishu_async.py:445,464` `send_card_to_user`（`async with session.post`）
- 关联文档：`src/im/feishu_async.py` 顶部 docstring / `docs/new-version2/feishu-web-chat-unified-design.md`