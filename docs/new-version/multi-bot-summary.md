# 多飞书 Bot 支持方案总结

> 实现 Chat Agent 和 VOD Agent 同时连接飞书，每个 Agent 使用独立的飞书应用凭证，
> 共享同一个 asyncio 事件循环，无竞态、无死锁。

---

## 一、最终架构

```
uvicorn 主事件循环 (单线程)
  │
  ├── asyncio task: Chat Bot WS
  │     ├── _lark_ws.loop = 主循环   ← 只设一次，全员共享
  │     ├── await client._connect()
  │     │     └── loop.create_task(_receive_message_loop) ← 在主循环上
  │     └── _handle_message() → process_chat() → 回复
  │
  └── asyncio task: VOD Bot WS
        ├── 共享同一个 _lark_ws.loop
        ├── await client._connect()
        └── _handle_vod() → process_vod_message() → 回复

  HTTP 请求处理（与其他 task 协作运行）
```

### 关键决策

| 决策 | 方案 | 原因 |
|------|------|------|
| 并发模型 | **单事件循环 + 多 async task** | 无竞态、零全局变量冲突 |
| WS 库 | `lark_oapi.ws.Client` | 原生支持飞书 Protobuf 二进制帧 |
| Bot 启动 | `asyncio.get_event_loop().create_task()` | uvicorn 同一事件循环 |
| 消息处理 | ThreadPoolExecutor | 不阻塞事件循环 |

---

## 二、踩坑记录

### 坑 1：`lark_oapi.ws.client.loop` 模块级全局变量竞态

**症状**：两个 WS 线程中只有一个能成功连接，另一个报 `RuntimeError: This event loop is already running`

**根因**：

```python
# lark_oapi/ws/client.py
loop = asyncio.get_event_loop()  # 模块级变量

class Client:
    async def _connect(self):
        loop.create_task(self._receive_message_loop())  # 使用模块级 loop

    def start(self):
        loop.run_until_complete(self._connect())  # 也使用模块级 loop
```

两个线程各自设置 `_lark_ws.loop = 自己的loop`，后者覆盖前者。两个线程都使用了同一个 loop，导致 `create_task` 在错误的 loop 上创建任务。

**解决**：**不用多线程，用单事件循环 + async task**。两个 bot 的 task 共享同一个主事件循环，`_lark_ws.loop` 只设置一次。

### 坑 2：同步 HTTP 阻塞事件循环

**症状**：async task 创建后无任何日志，任务"卡住"不执行

**根因**：

```python
# lark_oapi/ws/client.py
def _get_conn_url(self):
    response = requests.post(url, ...)  # ← 同步 HTTP，阻塞事件循环！
```

`_connect()` 内部调用 `_get_conn_url()`，其中使用 `requests.post()` 做同步 HTTP 请求。在 async task 中执行此操作会**阻塞整个事件循环**，导致其他 task（包括 HTTP 请求处理）全部暂停。

**解决**：在调用 `client._connect()` 之前，预先用异步方式获取 WS URL 并注入：

```python
# 异步获取 WS URL（通过 run_in_executor 在线程池中执行同步 HTTP）
from src.im.feishu_bot import _get_ws_endpoint_async
ws_url = await _get_ws_endpoint_async(app_id, app_secret)

# 注入 client 绕过 _get_conn_url 的同步请求
client = lark_oapi.ws.Client(...)
client._get_conn_url = lambda: ws_url
```

### 坑 3：`import asyncio` 未导入

**症状**：async task 创建后立即崩溃，`NameError: name 'asyncio' is not defined`

**根因**：`start_ws_async` 和 `start_vod_ws_async` 使用 `asyncio.get_running_loop()`、`asyncio.Event().wait()`，但模块顶部没有 `import asyncio`。之前依赖旧函数的局部导入，重构时丢失。

**解决**：在 `feishu.py` 顶部添加 `import asyncio`。

### 坑 4：飞书 WS 协议使用 Protobuf 二进制帧

**症状**：用原生 `websockets` 库连接飞书 WS，收不到任何消息（消息被过滤）

**根因**：

```
飞书 WS 协议：
  二进制帧 → Protobuf(Frame) → 解码 → JSON(event)
  Frame { method, payload, ... }
```

`async for raw in ws:` 默认接收文本帧，而飞书发送的是**二进制 Protobuf 帧**。`lark_oapi` 内部使用 `Frame.ParseFromString(msg)` 解码。

**解决**：**不要自己实现 WS 连接**，始终使用 `lark_oapi.ws.Client`。它已经处理了 Protobuf 编解码、握手、心跳重连等复杂逻辑。

### 坑 5：`asyncio.create_task` 在 uvicorn lifespan 中不执行

**症状**：task 创建日志出现，但 task 内的代码从未执行

**根因**：uvicorn 的 lifespan 机制可能导致某些版本中 `create_task` 的任务不被主循环调度。但在当前 Python 3.12 + uvicorn 版本中，这是**可以工作**的（已验证）。

**踩坑过程**：

| 尝试 | 结果 |
|------|------|
| `asyncio.create_task()` | ❌ 最初以为不行（其实是 `import asyncio` 缺失导致） |
| `asyncio.ensure_future()` | ❌ 与 create_task 等效 |
| `loop.create_task()` | ❌ 与 create_task 等效 |
| 测试 task 验证 | ✅ **可以运行**（`await asyncio.sleep` 任务正常） |
| `threading.Thread + asyncio.run()` | ✅ 可以但会引入模块级 loop 竞态 |

**最终结论**：`asyncio.create_task` **可以工作**，前提是 `import asyncio` 已导入。

### 坑 6：lambda 闭包捕获循环变量

**症状**：多线程启动 bot 时，所有 bot 都使用了最后一个 bot 的 app_id

**根因**：

```python
# ❌ 错误写法
for bot_app_id in rows:
    threading.Thread(target=lambda: asyncio.run(start(bot_app_id))).start()
    # bot_app_id 是引用，循环结束后值为最后一个元素
```

**解决**：使用默认参数捕获当前值：

```python
# ✅ 正确写法
threading.Thread(
    target=lambda a=bot_app_id: asyncio.run(start(a))
).start()
```

### 坑 7：VOD handler 缺少 `callable` 检查

**症状**：VOD Bot 连接成功但从不处理消息

**根因**：

```python
# ❌ 错误
event_type = getattr(header, 'event_type', None)
if event_type != "im.message.receive_v1":  # event_type 可能是 callable
    return  # 永远 return

# ✅ 正确
event_type = getattr(header, 'event_type', None)
if callable(event_type):
    event_type = event_type()
if event_type != "im.message.receive_v1":
    return
```

**根因**：`lark_oapi` 库的 `event_type` 在不同版本中可能是字符串或 callable。`_handle_message` 已有此检查，但自己实现的 handler 容易漏掉。

### 坑 8：编辑器误删 `app = FastAPI()`

**症状**：`Error loading ASGI app. Attribute "app" not found in module`

**根因**：多次重写 `main.py` 时，`app = FastAPI(...)`、路由注册、异常处理器、health 端点等关键代码被意外删除。

**教训**：重写文件后需检查关键结构是否完整。

### 坑 9：`_get_conn_url` 中 `requests.post` 使用 `requests` 而非 `urllib`

**症状**：WS 连接卡住，事件循环被阻塞。

**根因**：`lark_oapi` 的 `_get_conn_url()` 使用 `requests.post()`（同步 HTTP）。在 async task 中调用会阻塞事件循环。

**修复**：预获取 WS URL + monkey-patch client。

### 坑 10：`stop_ws()` 函数签名变更导致无法停止 WS

**根因**：`stop_ws()` 从接收 `account_name` 改为接收 `bot_key`，但调用处未同步更新。

**解决**：统一 `_ws_clients` 的 key 格式为 `chat_{app_id}` / `vod_{app_id}`。

---

## 三、数据表设计

### `im_bot_config` — 统一 IM 凭证

```sql
CREATE TABLE im_bot_config (
    bot_app_id TEXT PRIMARY KEY,
    bot_app_secret TEXT NOT NULL,
    bot_type TEXT NOT NULL,           -- 'chat_bot' / 'vod_bot'
    created_at TEXT DEFAULT (datetime('now'))
)
```

### `agent_sessions` — 新增 `vod_bot_app_id`

```sql
ALTER TABLE agent_sessions ADD COLUMN vod_bot_app_id TEXT DEFAULT '';
-- 指向 im_bot_config.bot_app_id
```

### `vod_conversations` — VOD 群会话管理

```sql
CREATE TABLE vod_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_session_id TEXT NOT NULL,
    feishu_chat_id TEXT NOT NULL,
    feishu_chat_name TEXT DEFAULT '',
    oc_session_id TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    last_active_at TEXT DEFAULT (datetime('now')),
    UNIQUE(agent_session_id, feishu_chat_id)
)
```

---

## 四、涉及的核心文件

| 文件 | 职责 |
|------|------|
| `src/im/feishu.py` | `start_ws_async()` / `start_vod_ws_async()` — async WS 启动 |
| `src/im/feishu_bot.py` | `_get_ws_endpoint_async()` — 异步获取 WS URL |
| `src/im/handler.py` | `process_chat()` — Chat 消息处理 |
| `src/im/vod_handler.py` | `process_vod_message()` — VOD 消息处理 |
| `src/api/main.py` | lifespan — `create_task` 启动所有 bot |
| `src/api/routes/agents.py` | PUT — VOD IM 凭证配置 |
| `src/api/routes/chat.py` | PUT — Chat IM 凭证配置 |

---

## 五、N 个 Bot 时的扩展性

| 指标 | 单事件循环 + async task |
|------|------------------------|
| 额外内存/bot | ~2MB（协程栈） |
| 10 个 bot 内存 | ~20MB |
| 全局变量竞态 | **无**（共享同一 loop） |
| 消息处理 | 运行在 `run_in_executor` 线程池（不阻塞事件循环） |
| WS 重连 | `lark_oapi` 内置 `auto_reconnect=True` |

当前架构天然支持 N 个 bot。添加新 bot 只需：
1. 在 `im_bot_config` 插入 `{bot_app_id, bot_app_secret, bot_type}`
2. 重启服务 → lifespan 自动读取并创建对应 task

---

## 六、验证 checklist

| 场景 | 预期 |
|------|------|
| 0 个 bot 配置 | lifespan 跳过，无报错 |
| 1 个 chat_bot | WS 连接，消息正常处理 |
| 1 个 vod_bot | WS 连接，消息正常处理 |
| chat_bot + vod_bot 同时 | **两个 WS 同时连接，互不干扰** |
| 凭据错误 | 该 bot 打印 warning，其他 bot 正常 |
| 服务停止 | `task.cancel()` 优雅关闭所有 WS |
