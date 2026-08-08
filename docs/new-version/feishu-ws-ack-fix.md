# 飞书 WS 消息重复消费修复方案（缺 ack 确认）

> 现象：chat bot / vod bot 重复消费历史消息；同一用户发送的"你好"在 07:46 和 08:01 被处理两次（message_id 不同）。
>
> 根因：`feishu_async.py` 手写飞书 WS 长连接协议，但**未实现消息 ack 确认**。飞书对未 ack 的消息会用新的 message_id 反复投递。

---

## 一、问题描述

### 1.1 现象

用户 06:55 在 chat 群发送"你好"（此时服务因 opencode 故障已停止，WS 未连接，消息在飞书侧排队）：

| 时间 | 事件 | 说明 |
|------|------|------|
| 01:06 | 服务停止（修复 opencode） | WS 断开 |
| **06:55** | **用户发送"你好"** | 消息在飞书侧排队 |
| 07:43:14 | 服务重启，WS 连接 | 飞书开始重推积压消息 |
| 07:46:25 | chat 收到"你好" id=`...abd8a...` | 处理并回复（Done） |
| **08:00:59** | vod 收到"你好" id=`...81670...` | 重复投递（新 id） |
| **08:01:11** | chat 收到"你好" id=`...95918...` | 重复投递（新 id） |

### 1.2 关键证据

- **同一内容"你好"出现多个不同 message_id**：`...abd8a...`、`...81670...`、`...95918...`
- `bot_seen_messages` 表中 chat bot 有多条"你好"记录，各自独立
- 现有 seen 去重缓存**防不住**：它只防"同一 message_id 重复"，但飞书对未 ack 的消息用**新 message_id 重新投递**，缓存无法识别

### 1.3 结论

**这是真实的重复消费**，根因是**飞书 WS 长连接的 ack 确认机制缺失**，而非 seen 缓存失效。

---

## 二、根因分析

### 2.1 飞书 WS 协议要求 ack

飞书 WebSocket 长连接协议中，**每收到一条 DATA 帧（EVENT）必须回传 ack 帧**，否则飞书认为消息未送达，会在后续（尤其重连后）用新的 message_id 重新投递。

### 2.2 参考实现：lark-oapi SDK

`lark_oapi/ws/client.py:_handle_data_frame`（318-359 行）：

```python
resp = Response(code=http.HTTPStatus.OK)          # 337 行：构造 ack 响应
try:
    if message_type == MessageType.EVENT:
        result = self._event_handler._do_without_validation(pl)
    ...
    if result is not None:
        resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
except Exception as e:
    resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

frame.payload = JSON.marshal(resp).encode(UTF_8)   # 358 行：payload 替换为 resp
await self._write_message(frame.SerializeToString())  # 359 行：原帧回传
```

**ack 帧 = 把收到的原始帧原样序列化回传，payload 替换为 `{"code":200,...}`**。

### 2.3 当前实现缺失

`src/im/feishu_async.py:run_bot`（711-731 行）：

```python
msg_id = parsed.get("message_id", "")
if msg_id:
    if msg_id in ctx._seen_msg_ids:
        continue
    ctx._seen_msg_ids.add(msg_id)
    _persist_seen_msg_id(ctx.app_id, msg_id)
    ...
# 处理消息...
# ⚠️ 没有把 frame 回传（ack）！处理完直接进入下一个 async for 迭代
```

处理完 EVENT 后**没有 `await ws.send(frame.SerializeToString())`** 回传 ack。飞书因此反复投递。

### 2.4 为什么以前"似乎正常"

- 旧版 `feishu.py` 依赖 **lark-oapi SDK** 的 WS client，其内部自动 ack（`_handle_data_frame` 358-359 行）
- 异步重构 `feishu_async.py` 时**手写了 WS 协议解析**（复用 lark-oapi 的 protobuf Frame 定义），但**漏掉了 ack 回传逻辑**
- 空闲期飞书不会重推，只在**断线重连 / 积压消息**场景暴露（本次：服务停机 6 小时）

---

## 三、修复方案

### 3.1 核心改动：`src/im/feishu_async.py:run_bot`

在 EVENT 帧处理完成后，**把收到的帧原样回传**（payload 替换为 ack 响应）：

```python
# 在处理 EVENT 后、进入下一条消息前，补充 ack 回传：

# 处理消息（现有逻辑）...
parsed = _parse_event(event_data)
if not parsed or not parsed.get("text"):
    # 无文本也需 ack（否则飞书重推），放在 skip 分支外统一处理
    pass

# ── 新增：ack 回传（飞书 WS 协议要求，防止重复投递）──
try:
    resp_payload = json.dumps({"code": 200}).encode("utf-8")
    frame.payload = resp_payload
    await ws.send(frame.SerializeToString())
except Exception as e:
    log.warning(f"[{ctx.name}] send ack failed: {e}")
```

**关键点**：
1. ack 必须在**每条 EVENT 帧处理完**（无论是否命中 seen / 是否有效文本）都回传
2. 回传的是**原 frame**，仅替换 payload 为 `{"code":200}`，保持 headers 不变（飞书靠 headers 里的 message_id 关联 ack）
3. 用 `ws.send()`（websockets 库的发送 API，lark-oapi 用的是 `_write_message`，等价）

### 3.2 ack 位置与异常处理

- ack 应在 **EVENT 分支内**、消息处理后、`continue`/下一次迭代前
- 即使消息处理抛异常，也要回传 ack（否则飞书重推导致死循环）——参考 lark-oapi：处理失败也回 `code=500` 的 ack（client.py:356）
- 建议结构：把 ack 逻辑放在 EVENT 帧解析后统一处理，与 seen 检查分离

### 3.3 结构示意

```python
if ft != FrameType.DATA:
    continue
hs = frame.headers
type_ = _get_header(hs, "type")
if type_ != MessageType.EVENT.value:
    continue

# 解析
try:
    payload_str = frame.payload.decode("utf-8") if frame.payload else ""
    event_data = json.loads(payload_str)
except Exception:
    await _send_ack(ws, frame)   # 解析失败也 ack
    continue

parsed = _parse_event(event_data)

# 统一 ack（无论消息是否处理，必须回传）
try:
    await ws.send(_build_ack_frame(frame))
except Exception as e:
    log.warning(f"[{ctx.name}] ack send failed: {e}")

if not parsed or not parsed.get("text"):
    continue

msg_id = parsed.get("message_id", "")
if msg_id:
    if msg_id in ctx._seen_msg_ids:
        continue
    ctx._seen_msg_ids.add(msg_id)
    _persist_seen_msg_id(ctx.app_id, msg_id)
    ...

# 处理消息（现有逻辑）
```

### 3.4 辅助函数

```python
def _build_ack_frame(frame) -> bytes:
    """构造 ack 帧：payload 替换为 {"code":200}，其余不变。"""
    frame.payload = json.dumps({"code": 200}).encode("utf-8")
    return frame.SerializeToString()
```

---

## 四、验证计划

### 4.1 单元/集成测试

1. **ack 回传**：mock `ws.send`，收到 EVENT 帧后断言调用了 `send`，且 payload 为 `{"code":200}`
2. **seen 去重仍生效**：同 message_id 二次到达 → skip，不重复处理
3. **无文本消息也 ack**：`_parse_event` 返回 None / text 为空 → 仍回传 ack

### 4.2 现网验证

1. 重启服务，观察 WS 连接正常
2. 在服务停机期间发一条消息，重启后：
   - 首次投递被处理 + ack
   - **飞书不再重推**（对比修复前会出现 07:46 / 08:01 两次）
3. 观察 `bot_seen_messages` 不再出现同内容多 id 记录

### 4.3 回归

- `test_bot_manager_load.py` / `test_p2p_bind.py` / `test_feishu_handler.py` 不回归
- 全量 `pytest tests/`

---

## 五、相关文件

| 文件 | 改动 |
|------|------|
| `src/im/feishu_async.py` | `run_bot` 中 EVENT 帧处理加 ack 回传；新增 `_build_ack_frame` 辅助函数 |
| `src/im/feishu_async.py` 顶部 docstring | 补充"WS 协议帧 ack"说明 |

---

## 六、注意事项

1. **ack 是原帧回传**：`frame` 对象在 websockets 库中每次迭代是新的，直接修改其 payload 回传即可，不影响后续解析
2. **不要 ack PING**：PING 走 CONTROL 分支已 `continue`，无需处理；PONG 由飞书发来是保活响应，也无需回
3. **并发安全**：ack 在 `async for` 主循环内同步 `await ws.send`，与 `_safe_handle` 异步任务无冲突
4. **seen 缓存保留**：ack 修复后 seen 仍是第一道防线（防飞书极端情况下的同 id 重推），两者互补
