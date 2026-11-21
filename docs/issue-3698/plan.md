# Issue #3698 方案设计：传输层归一 + 独立归因/退避 + 同 app 单连接

## 分层与数据流

```
飞书 WS（3001 关闭帧 / BrokenPipe）
   │  websockets.ConnectionClosedError
   ▼
FeishuChannel.connect()            ← 新增：接收循环 + 握手整体 try/except 归一
   │  ChannelRegistrationConflict(ChannelError)  或  ChannelError
   ▼
Supervisor._run()                  ← 新增：专用分支（先于 except ChannelError）
   │  record_reconnect("ws_register_conflict")
   │  5s→10s→20s→…→300s 退避；连续 5 次 → ERROR 告警
   ▼
EngineMetrics.flap_by_cause        ← 新增归因桶（/health/realtime im 段可见）
```

## 关键设计

1. **契约层只做加法**：`channel/channel.py` 新增 `ChannelRegistrationConflict(ChannelError)`，
   `ChannelError`/`PermanentChannelError` 语义不变（既有 except 分支顺序无需其它改动）；
   仍未归类为 `PermanentChannelError`（3001 可重试，不该 revoke）。
2. **归一位置选在 `async with ws_connect(...)` 之外层**：握手阶段与服务端注册被拒
   （飞书在建连时注册）同样覆盖，而不只是 `async for` 读循环。
   显式 `except asyncio.CancelledError: raise`（停机/让渡取消不得被包成链路失败），
   `except (ChannelError, PermanentChannelError): raise` 透传已归一的契约异常，
   其余 `except Exception` → `_ws_failure()`，`raise ... from exc` 保留原始 cause 链。
3. **关闭码提取**：`_ws_close_code()` 只认 websockets 关闭对象
   （`ConnectionClosed` 含 Error 子类、`frames.Close`）的 `.code`，并沿
   `__cause__`/`__context__` 上溯（带 visited 去环），因此「BrokenPipe 被 3001 覆盖」
   的现场也能命中 3001；非 WS 异常自带的同名 `.code`（HTTP/业务错误码）不再被误判。
4. **归因而非字符串匹配**：Supervisor 用异常类型分派，不解析日志文本；桶名
   `ws_register_conflict` 进入 `FLAP_CAUSES`（缺失键按 0 暴露，看板零改动）。
5. **退避分档**：`next_backoff(current, register_conflict=)` 纯函数——
   普通抖动 2s→15s（原行为不变），注册冲突 5s→300s。冲突计数按**上一轮 connect()
   的真实存活时长**（`last_session_secs`）判定：≥60s
   （`REGISTER_CONFLICT_STABLE_SECS`）即视为已恢复并重置计数/退避——冲突分支自身
   是握手期快速失败，不能用本轮耗时判断，否则健康长连接断开后再撞 3001 时计数会
   跨健康期残留（非连续冲突误升级 ERROR、恢复后仍沿用最长退避）。
6. **告警即日志级别分级**：前 4 次 WARNING（带 `consecutive` / `retry_in`），
   第 5 次起 ERROR 且带 `registration conflict escalated` 与 `cause=ws_register_conflict`
   —— Grafana 既有 ERROR 规则即可捕获，无需新增外部告警通道。
7. **同 app 单连接**：`Supervisor._ensure_channel` 按 `app_identity(config)`
   （各适配器真实读取的 `app_id`/`bot_id`/`ilink_bot_id`/`bot_token`）判重，第二个持有者
   直接跳过（**不建连接、不抢租约**），WARNING 记录 `app/inst/owner`；归属在
   让渡/revoke/停机时由 `_drop_app_ref()` 释放，被跳过的安装下一轮自然接上
   （不饿死、不静默删安装行）。日志里的 app 标识走白名单：仅飞书 `cli_*` / 钉钉 `ding*`
   公开形态明文，其余（企微 `bot_id`、微信 `ilink_bot_id`、任意凭据形态）一律 sha256 摘要，
   日志不落凭据片段。
8. **不重复造轮子**：`engine_wiring._built_apps` 的 app 去重仍在（装配层「同一 app 只建一条」），
   本改动补的是 Supervisor 层「多安装/僵尸连接并发注册」这一层缺失的校验——
   工厂被不同 scope 放过时才会真正撞 3001。

## 测试 seam

| seam | 位置 | 说明 |
|------|------|------|
| `ws_connect` / `endpoint_fetcher` | 飞书 Channel 既有注入点 | 假 WS 抛真实 `ConnectionClosedError`，不触网 |
| `supervisor._backoff_sleep` | 新增模块级函数 | 用例替身记账后立即返回，不真等 5~300s |
| `Supervisor._scan_once` / `_channels` / `_app_refs` | 既有内部观测点（既有用例同款） | 驱动一轮扫描断言装配结果 |
| `EngineMetrics.flap_by_cause` | 既有公开只读快照 | 断言归因桶 |

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 归一误吞真实适配器 bug | 只包传输层语句块（帧解析/分发仍在内部 try），cause 链与 `repr` 保留 |
| 300s 长退避导致恢复变慢 | 仅在"连续冲突"成立时升档；健康存活 ≥60s 立即重置为 5s |
| app 去重误伤合法多安装 | 仅挡「同一 app 身份 + 同一 Supervisor」；只记录不删安装行；owner 释放后接上 |
| 新增 flap 桶影响看板键集 | `snapshot()` 始终输出全桶（缺失给 0），legacy 键双发不变 |