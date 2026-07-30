# IM WebSocket 异步重构方案

> 将 IM Bot 的 WebSocket 连接从多线程模型重构为单事件循环 + 多 async task 模型。

---

## 一、现状与问题

### 当前架构

```
uvicorn 主事件循环 (asyncio)
  │
  ├── HTTP 请求处理
  │
  ├── Thread-1: Chat WS
  │     ├── asyncio.new_event_loop()
  │     ├── _lark_ws.loop = loop  ← 修改模块级全局变量
  │     ├── client.start()         ← 调用 loop.run_until_complete()
  │     └── 与 Thread-2 竞争 _lark_ws.loop
  │
  └── Thread-2: VOD WS
        ├── asyncio.new_event_loop()
        ├── _lark_ws.loop = loop  ← 覆盖 Thread-1 的 loop
        ├── client.start()         ← 使用 Thread-1 的 loop → 崩溃
        └── 竞争 _lark_ws.loop 导致连接失败
```

### 根因

`lark_oapi.ws.client` 模块有一个全局变量 `loop`：

```python
# lark_oapi/ws/client.py
loop = asyncio.get_event_loop()  # 模块级单例

class Client:
    def start(self):
        loop.run_until_complete(self._connect())  # 使用模块级 loop

    async def _connect(self):
        conn = await websockets.connect(url)
        loop.create_task(self._receive_message_loop())  # 也使用模块级 loop
```

多个线程竞争设置 `_lark_ws.loop`，导致：
1. 一个线程的 loop 被另一个线程覆盖
2. `run_until_complete` 尝试在已运行的 loop 上执行 → `RuntimeError`
3. `loop.create_task()` 在错误的 loop 上创建任务 → 连接失败

---

## 二、目标架构

```
uvicorn 主事件循环 (asyncio)
  │
  ├── HTTP 请求处理
  │
  ├── Task-1: Chat WS
  │     ├── _lark_ws.loop = 主事件循环  ← 只设置一次
  │     ├── await client._connect()      ← 异步等待连接
  │     └── 由主事件循环调度 _receive_message_loop
  │
  ├── Task-2: VOD WS
  │     ├── 共享主事件循环
  │     ├── await client._connect()      ← 异步等待连接
  │     └── 由主事件循环调度 _receive_message_loop
  │
  └── Task-3 to Task-N: 更多 IM Bot（如果需要）
        └── 全部共享主事件循环，无竞争
```

### 核心变化

| 维度 | 线程方案 | async 方案 |
|------|---------|-----------|
| **执行方式** | `threading.Thread` | `asyncio.create_task` |
| **事件循环** | 每个线程各自创建 | **共享主事件循环** |
| **`_lark_ws.loop`** | 多线程竞争覆盖 | **设置一次，全员共享** |
| **`lark_oapi.APP_ID`** | 每线程覆盖 | 在 `Client.__init__` 已传入，不依赖全局变量 |
| **启动方式** | `client.start()` (blocking) | `await client._connect()` (async) |
| **资源占用** | ~8MB/连接 (线程栈) | ~50KB/连接 (协程) |

---

## 三、详细设计

### 3.1 `start_ws()` 改为 async

```python
# 删除旧版：
def start_ws(on_message_callback, account_name: str = "default"):
    ...

# 新增：
async def start_ws_async(on_message_callback, bot_app_id: str, bot_app_secret: str):
    """在运行中事件循环上创建并连接 WS Client"""
    global _ws_clients

    # 设置模块级 loop 为当前运行中的事件循环（只做一次）
    import lark_oapi.ws.client as _lark_ws
    _lark_ws.loop = asyncio.get_running_loop()

    # 记录当前 WS 使用的 app_id
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('ws_feishu_app_id', ?)", (bot_app_id,))
            db.commit()
    except Exception:
        pass

    # 创建事件处理器
    def _handle_with_account(event):
        return _handle_message(event, on_message_callback, bot_app_id)

    import lark_oapi
    handler = (lark_oapi.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_handle_with_account)
               .build())

    # 创建 WS Client（不需要设置 lark_oapi.APP_ID）
    client = lark_oapi.ws.Client(
        app_id=bot_app_id,
        app_secret=bot_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )

    ws_key = f"chat_{bot_app_id}"
    _ws_clients[ws_key] = client

    log.info(f"Chat WS connecting (bot_app_id={bot_app_id[:30]}...)")

    # 异步连接
    try:
        await client._connect()
    except Exception as e:
        log.warning(f"Chat WS connect failed for {bot_app_id[:20]}: {e}")
        await client._disconnect()

    log.info(f"Chat WS connected (bot_app_id={bot_app_id[:30]}...)")

    # _connect() 已通过 loop.create_task 启动了 _receive_message_loop
    # 这里保持 task 存活，直到被取消
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log.info(f"Chat WS cancelled (bot_app_id={bot_app_id[:30]}...)")
```

### 3.2 `start_vod_ws()` 改为 async

```python
async def start_vod_ws_async(bot_app_id: str, bot_app_secret: str):
    """在运行中事件循环上创建并连接 VOD WS Client"""
    global _ws_clients

    import lark_oapi.ws.client as _lark_ws
    _lark_ws.loop = asyncio.get_running_loop()

    from src.im.vod_handler import process_vod_message

    def _handle_vod(event):
        # ... 同上，消息处理逻辑 ...
        pass

    import lark_oapi
    handler = (lark_oapi.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_handle_vod)
               .build())

    client = lark_oapi.ws.Client(
        app_id=bot_app_id,
        app_secret=bot_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )

    ws_key = f"vod_{bot_app_id}"
    _ws_clients[ws_key] = client

    log.info(f"VOD WS connecting (bot_app_id={bot_app_id[:30]}...)")

    try:
        await client._connect()
    except Exception as e:
        log.warning(f"VOD WS connect failed for {bot_app_id[:20]}: {e}")
        await client._disconnect()

    log.info(f"VOD WS connected (bot_app_id={bot_app_id[:30]}...)")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log.info(f"VOD WS cancelled (bot_app_id={bot_app_id[:30]}...)")
```

### 3.3 `main.py` lifespan 改为 create_task

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.config import log
    ws_tasks = []

    # 读取所有 IM 配置
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT DISTINCT bot_app_id, bot_app_secret, bot_type FROM im_bot_config")
            rows = c.fetchall()

        from src.im.feishu import start_ws_async, start_vod_ws_async
        from src.im.handler import process_chat

        for bot_app_id, bot_app_secret, bot_type in rows:
            try:
                if bot_type == "chat_bot":
                    task = asyncio.create_task(
                        start_ws_async(process_chat, bot_app_id, bot_app_secret),
                        name=f"chat-ws-{bot_app_id[:20]}"
                    )
                    ws_tasks.append(task)
                    log.info(f"Chat WS task created for {bot_app_id[:40]}")

                elif bot_type == "vod_bot":
                    task = asyncio.create_task(
                        start_vod_ws_async(bot_app_id, bot_app_secret),
                        name=f"vod-ws-{bot_app_id[:20]}"
                    )
                    ws_tasks.append(task)
                    log.info(f"VOD WS task created for {bot_app_id[:40]}")

            except Exception as e:
                log.warning(f"WS task create failed for {bot_app_id[:20]}: {e}")

        log.info(f"IM Bot WS: {len(ws_tasks)} tasks created")

    except Exception as e:
        log.warning(f"IM Bot WS startup failed: {e}")

    yield

    # 关闭
    log.info("Shutting down IM Bot WS...")
    for task in ws_tasks:
        task.cancel()
    for task in ws_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
```

### 3.4 `stop_ws()` 更新

```python
def stop_ws(bot_app_id: str | None = None):
    """停止指定或所有 WS 连接"""
    global _ws_clients
    if bot_app_id:
        for key in (f"chat_{bot_app_id}", f"vod_{bot_app_id}"):
            client = _ws_clients.pop(key, None)
            if client:
                try:
                    client.stop()
                except Exception:
                    pass
        return
    for name, client in list(_ws_clients.items()):
        try:
            client.stop()
        except Exception:
            pass
    _ws_clients = {}
```

### 3.5 保留旧函数兼容

旧版 `start_ws` 和 `start_vod_ws` 可以删除，因为它们仅在 `main.py` 中被调用。如果担心兼容性，可以保留为包装器：

```python
def start_ws(on_message_callback, account_name="default"):
    """兼容旧版——通过线程运行异步版本"""
    import asyncio
    asyncio.run(start_ws_async(on_message_callback, "", ""))
```

---

## 四、涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/im/feishu.py` | **重写** | `start_ws_async()` / `start_vod_ws_async()` 替代 `start_ws()` / `start_vod_ws()`；`stop_ws()` 更新 |
| `src/api/main.py` | **重写** | lifespan 用 `asyncio.create_task` 替代 `threading.Thread` |

不涉及：
- `src/im/handler.py` — 回调函数不变
- `src/im/vod_handler.py` — `process_vod_message` 签名不变
- `configs/` — 配置文件不变
- 数据库 — 表结构不变

---

## 五、验证要点

| 场景 | 预期 |
|------|------|
| 0 个 bot 配置 | 跳过，无报错，`ws_tasks` 为空 |
| 1 个 chat_bot | WS 正常连接，消息正常处理 |
| 1 个 chat_bot + 1 个 vod_bot | **两个 WS 同时连接，互不干扰** |
| N 个 bot | 全部在同一个事件循环上协作运行 |
| 凭据错误 | 该 bot 打印警告，其他 bot 正常 |
| 服务关闭 | `task.cancel()` 优雅关闭所有 WS |

### 6.2 10 个 Bot 时的资源对比

| 资源 | 线程方案 | async 方案 |
|------|---------|-----------|
| 线程/协程数 | 10 个 OS 线程 | **10 个协程**（单线程） |
| 额外内存 | ~80MB | **~500KB** |
| `_lark_ws.loop` 竞争 | ❌ 10 个线程互抢 | ✅ 共享主 loop |
| `lark_oapi.APP_ID` 竞争 | ❌ 互相覆盖 | ✅ 不依赖全局变量 |
