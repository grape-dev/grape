# IM WebSocket 多 Bot 启动修复方案

## 问题概述

当前 `src/api/main.py` 的 startup 阶段虽然从 `im_bot_config` 表遍历了所有 bot，但 `src/im/feishu.py` 中的 `start_ws()` 和 `start_vod_ws()` 存在设计缺陷，**无法同时启动多个 IM Bot 的 WebSocket 连接**。

---

## 问题链详细分析

### 问题 1：`start_ws()` 硬编码 `LIMIT 1`，忽略调用方传入的参数

**位置**：`src/im/feishu.py:585-654`

```
main.py (遍历所有 bot)                 feishu.py start_ws (忽略参数)
┌─────────────────────────┐           ┌────────────────────────────┐
│ SELECT DISTINCT         │           │ def start_ws(callback,     │
│   bot_app_id, ...       │           │            account_name):  │
│ FROM im_bot_config      │           │                            │
│                         │           │ # 内部又重新查 DB          │
│ for bot in rows:        │  ──→      │ SELECT ... FROM            │
│   Thread(target=        │           │   im_bot_config            │
│     start_chat_ws,      │           │   WHERE bot_type='chat_bot'│
│     args=(process_chat,)│           │   LIMIT 1  ← 硬编码        │
│   )                     │           │                            │
│   # app_id 根本没传     │           │ # app_id/app_secret        │
└─────────────────────────┘           │ # 只用第一个 bot 的凭据    │
                                      └────────────────────────────┘
```

**后果**：遍历循环启动了多个线程，但每个线程内部都取了同一个 bot 的凭据。多个 chat_bot 实际只连了第一个。

---

### 问题 2：`lark_oapi.APP_ID` / `lark_oapi.APP_SECRET` 是模块级全局变量

**位置**：`src/im/feishu.py:632-633`、`src/im/feishu.py:757-758`

```python
# feishu.py:632-633（start_ws 中）
lark_oapi.APP_ID = app_id
lark_oapi.APP_SECRET = app_secret

# feishu.py:757-758（start_vod_ws 中）
lark_oapi.APP_ID = app_id
lark_oapi.APP_SECRET = app_secret
```

即使启动了多个 WS 线程，每个线程都会覆盖 `lark_oapi` 包级别的全局变量 `APP_ID` 和 `APP_SECRET`。**已建立的连接会因此失效**。

---

### 问题 3：`_lark_ws.loop` 是模块级全局事件循环

**位置**：`src/im/feishu.py:590-591`、`src/im/feishu.py:696-697`

```python
import lark_oapi.ws.client as _lark_ws
_lark_ws.loop = loop  # 覆盖模块级全局变量
```

每个 WS 连接需要独立的事件循环。但这里设置的是 `lark_oapi.ws.client` 模块的全局 `loop` 变量。**多线程竞争覆盖，导致事件循环混乱**。

---

### 问题 4：Chat WS 启动未传入 `bot_app_id`

**位置**：`src/api/main.py:31`

```python
Thread(target=start_chat_ws, args=(process_chat,), ...)
```

即使 `start_ws()` 修复为接受参数，这里也没传。导致 `_ws_clients` 中所有 chat bot 都以 `account_name="default"` 作为 key（line 642），**后面的会覆盖前面的**。

---

### 问题 5：单层 try-except 包裹整个启动循环

**位置**：`src/api/main.py:40-41`

```python
except Exception as e:
    log.warning(f"IM Bot WS start skipped: {e}")
```

一个 bot 启动失败会导致循环中断，后续 bot 全部跳过。

---

### 问题 6：`start_vod_ws()` 同样存在全局变量覆盖问题

**位置**：`src/im/feishu.py:691-768`

与 `start_ws()` 相同的三个问题（lark_oapi.APP_ID 全局覆盖、_lark_ws.loop 全局覆盖、内部 DB 查询）。

---

### 问题 7：VOD WS 的 `_ws_clients` key 格式不一致

**位置**：`src/im/feishu.py:642` vs `src/im/feishu.py:766`

```python
# chat WS — key 是 account_name
_ws_clients[account_name] = client  # → "default"

# vod WS — key 是 f"vod_{bot_app_id}"
_ws_clients[f"vod_{bot_app_id}"] = client  # → "vod_cli_xxx"
```

chat bot 的 key 没有区分不同 bot，vod bot 的 key 虽然区分了但风格不一致。`stop_ws()` 传入 `account_name` 时无法准确停止指定 chat bot。

---

## 修复方案

### 方案总览

| 文件 | 修改内容 |
|------|---------|
| `src/im/feishu.py` | 重写 `start_ws()` / `start_vod_ws()` / `stop_ws()` — 消除全局变量依赖 |
| `src/api/main.py` | 修正启动循环 — 传递参数 + 独立 try-catch |

---

### 修改 1：`src/im/feishu.py` — `start_ws()` 重写

**当前**（585-654 行）：

```python
def start_ws(on_message_callback, account_name: str = "default"):
    # ① 内部查 DB（LIMIT 1）
    # ② 写全局 APP_ID/APP_SECRET
    # ③ 写全局 _lark_ws.loop
```

**改为**：

```python
def start_ws(on_message_callback, bot_app_id: str, bot_app_secret: str):
    """启动 WebSocket 长连接。不查询 DB，凭据由调用方传入。"""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    global _ws_clients

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

    # 创建独立的 WS Client（不设置 lark_oapi 模块级全局变量）
    from lark_oapi.ws import Client as WsClient
    client = WsClient(
        app_id=bot_app_id,
        app_secret=bot_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )

    # 使用独立的 key 存储
    ws_key = f"chat_{bot_app_id}"
    _ws_clients[ws_key] = client

    log.info(f"Feishu WS client starting (bot_app_id={bot_app_id[:30]}...)")
    try:
        loop.run_until_complete(client._connect())
    except Exception:
        loop.run_until_complete(client._disconnect())
        if client._auto_reconnect:
            loop.run_until_complete(client._reconnect())
        else:
            raise
    loop.create_task(client._ping_loop())
    loop.run_until_complete(client._select())
```

**关键变更**：

| 变更 | 说明 |
|------|------|
| 函数签名 | `(callback, bot_app_id, bot_app_secret)` — 调用方传入凭据 |
| 移除内部 DB 查询 | 不再 `SELECT ... LIMIT 1` |
| 移除 `lark_oapi.APP_ID` 赋值 | 创建 Client 时传入参数即可 |
| 移除 `_lark_ws.loop` 赋值 | 使用 `asyncio.new_event_loop()` 创建线程独立 loop |
| 使用独立 WS Client | `from lark_oapi.ws import Client` 不共享模块变量 |
| WS key 统一格式 | `chat_{bot_app_id}` — 可区分不同 bot |

---

### 修改 2：`src/im/feishu.py` — `start_vod_ws()` 重写

**当前**（691-768 行）：

```python
def start_vod_ws(bot_app_id: str):
    # ① 内部查 DB（重新查询凭据）
    # ② 写全局 APP_ID/APP_SECRET
    # ③ 写全局 _lark_ws.loop
```

**改为**：

```python
def start_vod_ws(bot_app_id: str, bot_app_secret: str):
    """启动 VOD Agent 的 WebSocket 连接。凭据由调用方传入。"""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    global _ws_clients

    from src.im.vod_handler import process_vod_message

    def _handle_vod(event):
        try:
            header = getattr(event, 'header', None)
            if header is None:
                return
            event_type = getattr(header, 'event_type', None)
            if isinstance(event_type, str) and event_type == "im.message.receive_v1":
                log.info(f"VOD WS: im.message.receive_v1 received")
            event_body = getattr(event, 'event', None)
            if event_body is None:
                return
            message_dict = {
                "event": {
                    "message": {
                        "message_id": getattr(getattr(event_body, 'message', None), 'message_id', ''),
                        "chat_id": getattr(getattr(event_body, 'message', None), 'chat_id', ''),
                        "message_type": getattr(getattr(event_body, 'message', None), 'message_type', ''),
                        "content": getattr(getattr(event_body, 'message', None), 'content', ''),
                    },
                    "sender": {
                        "sender_id": {
                            "open_id": getattr(getattr(getattr(event_body, 'sender', None), 'sender_id', None), 'open_id', ''),
                            "union_id": getattr(getattr(getattr(event_body, 'sender', None), 'sender_id', None), 'union_id', ''),
                            "user_id": getattr(getattr(getattr(event_body, 'sender', None), 'sender_id', None), 'user_id', ''),
                        }
                    }
                }
            }
            process_vod_message(message_dict, bot_app_id)
        except Exception as e:
            log.warning(f"VOD WS handle error: {e}")

    import lark_oapi
    handler = (lark_oapi.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_handle_vod)
               .build())

    from lark_oapi.ws import Client as WsClient
    client = WsClient(
        app_id=bot_app_id,
        app_secret=bot_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )

    ws_key = f"vod_{bot_app_id}"
    _ws_clients[ws_key] = client

    log.info(f"VOD WS client starting (bot_app_id={bot_app_id[:30]}...)")
    try:
        loop.run_until_complete(client._connect())
    except Exception:
        loop.run_until_complete(client._disconnect())
        if client._auto_reconnect:
            loop.run_until_complete(client._reconnect())
        else:
            raise
    loop.create_task(client._ping_loop())
    loop.run_until_complete(client._select())
```

---

### 修改 3：`src/im/feishu.py` — `stop_ws()` 增强

**当前**（672-688 行）：

```python
def stop_ws(account_name: str | None = None):
```

**改为**（逻辑不变，但注释确认 key 格式）：

```python
def stop_ws(bot_app_id: str | None = None):
    """停止指定或所有 WS 连接。
    
    Args:
        bot_app_id: 传 bot_app_id 则停止对应的 chat 和 vod WS；
                    传 None 则停止全部。
    """
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

---

### 修改 4：`src/api/main.py` — 修正启动循环

**当前**（19-41 行）：

```python
c.execute("SELECT DISTINCT bot_app_id, bot_app_secret, bot_type FROM im_bot_config")
rows = c.fetchall()
from src.im.feishu import start_vod_ws
from src.im.handler import process_chat
from src.im.feishu import start_ws as start_chat_ws
for bot_app_id, bot_app_secret, bot_type in rows:
    if bot_type == "chat_bot":
        t = threading.Thread(target=start_chat_ws, args=(process_chat,), daemon=True,
                             name=f"chat-ws-{bot_app_id[:20]}")
        t.start()
        log.info(f"Chat WS started for bot_app_id={bot_app_id[:40]}")
    elif bot_type == "vod_bot":
        t = threading.Thread(target=start_vod_ws, args=(bot_app_id,), daemon=True,
                             name=f"vod-ws-{bot_app_id[:20]}")
        t.start()
        log.info(f"VOD WS started for bot_app_id={bot_app_id[:40]}")
```

**改为**：

```python
c.execute("SELECT DISTINCT bot_app_id, bot_app_secret, bot_type FROM im_bot_config")
rows = c.fetchall()
from src.im.feishu import start_vod_ws, start_ws as start_chat_ws
from src.im.handler import process_chat
started = 0
for bot_app_id, bot_app_secret, bot_type in rows:
    try:
        if bot_type == "chat_bot":
            t = threading.Thread(target=start_chat_ws,
                                 args=(process_chat, bot_app_id, bot_app_secret),
                                 daemon=True,
                                 name=f"chat-ws-{bot_app_id[:20]}")
            t.start()
            log.info(f"Chat WS started: bot_app_id={bot_app_id[:40]}")
            started += 1
        elif bot_type == "vod_bot":
            t = threading.Thread(target=start_vod_ws,
                                 args=(bot_app_id, bot_app_secret),
                                 daemon=True,
                                 name=f"vod-ws-{bot_app_id[:20]}")
            t.start()
            log.info(f"VOD WS started: bot_app_id={bot_app_id[:40]}")
            started += 1
    except Exception as e:
        log.warning(f"IM WS start failed for {bot_app_id[:20]}: {e}")
log.info(f"IM Bot WS: {started}/{len(rows)} connections started")
```

**关键变更**：

| 变更 | 说明 |
|------|------|
| 传入 `bot_app_id, bot_app_secret` | 每个 bot 的凭据由 main.py 传入，而非内部查询 |
| 每个 bot 独立 try-catch | 一个 bot 失败不影响其他 bot |
| 统计启动成功数 | 便于排查问题 |
| VOD WS 也传入 app_secret | 与 start_vod_ws 新签名一致 |

---

## 修改文件清单

| 文件 | 修改行 | 变更类型 |
|------|--------|---------|
| `src/im/feishu.py` | 585-654 → 重写 `start_ws()` | 函数重写 |
| `src/im/feishu.py` | 691-768 → 重写 `start_vod_ws()` | 函数重写 |
| `src/im/feishu.py` | 672-688 → 增强 `stop_ws()` | 函数修改 |
| `src/api/main.py` | 19-41 → 修正启动循环 | 逻辑修改 |

**不涉及文件**：
- `src/im/handler.py` — 无需修改，`process_chat` 回调不依赖 bot 标识
- `src/im/vod_handler.py` — 无需修改，`process_vod_message` 已接受 `bot_app_id`
- 数据库表 `im_bot_config` — 结构不变

---

## 验证要点

| 场景 | 预期结果 |
|------|---------|
| 0 个 bot 配置 | 跳过启动，无报错 |
| 1 个 chat_bot | WS 正常连接，与当前行为一致 |
| 2 个 chat_bot | 两个 WS 独立连接，消息分别处理 |
| 1 个 chat_bot + 1 个 vod_bot | 两个 WS 独立连接，互不干扰 |
| 某个 bot 凭据错误 | 该 bot 启动失败打 warn，其他 bot 正常启动 |
| 服务停止 | `stop_ws(None)` 停止所有连接 |
