# IM Bot 异步连接方案：机制分析与优化方向

> 版本：v1.0
> 日期：2026-08-30
> 范围：`src/im/feishu_async.py`（手写 WebSocket 多 Bot 方案）vs `lark_oapi.ws.Client`（飞书官方 SDK）机制对比、差异定位、问题根因与后续优化方向。
> 现状：生产部署于 `new-version3` 分支，`new-version2` 为上一部署分支。

---

## 1. 连接机制总览

```
uvicorn 主事件循环 (asyncio)
  │
  ├── 每 bot 一个 asyncio.Task (run_bot)
  │     ├── 短 HTTP POST /callback/ws/endpoint  → 拿到一条 wss:// URL（动态，含 fpid/aid/service_id/device_id/ticket）
  │     ├── websockets.connect(wss_url)         ← WebSocket 双工长连接（每 bot 一条）
  │     ├── _ping_keepalive：每 120s 发应用层 PING 帧保活（new-version3 另有 idle>300s 强制断开重连）
  │     ├── 收帧循环：CONTROL(PING→回 PONG) / DATA(EVENT/CARD→JSON→ack→seen 去重→handler)
  │     └── 断开→指数退避重连（new-version3: 2s→15s；new-version2: 5s→300s）
  │
  └── BotManager 统一编排（load / lease sweep / 健康检查 / 热加载）
```

- 全部复用 `lark-OAPI` 的 protobuf 帧定义（`Frame/FrameType/MessageType`），但**绕过 SDK，自行实现连接生命周期**。
- 虽然 endpoint 拿到的 `fpid/aid/service_id` 在所有 bot 完全相同（走同一飞书 Connection 网关），但**代码为每个 bot 建立独立 WebSocket TCP 连接**，不是单连接复用。

---

## 2. 与官方 lark-oapi ws.Client 的机制对比

### 2.1 协议层（相同）

| 环节 | 官方 SDK | 手写实现 |
|---|---|---|
| endpoint 获取 | POST `/callback/ws/endpoint`，body `{"AppID","AppSecret"}` | 同 URL、同 body |
| 帧协议 | `Frame (method=CONTROL/DATA) + headers(type) + payload` | 复用同一 protobuf 定义 |
| 事件类型 | `EVENT` / `CARD`，header `sum/seq/message_id/trace_id` | 同 header 解析 |
| 消息发送 | `im/v1/messages/{id}/reply` | 相同 |

### 2.2 生命周期与管理（差异集中在 key）

| 维度 | 官方 `ws.Client` | 手写 `feishu_async` | 风险 |
|---|---|---|---|
| 事件循环 | 模块级单例 `loop`；每 client 独立线程 + `loop.run_until_complete` | **单事件循环 + 每 bot create_task** | 官方多 client 多线程互抢 `loop`；手写无此问题 |
| endpoint 阻塞 | `requests.post`（同步阻塞） | aiohttp 异步 POST（共享连接池） | 手写更优 |
| 保活 | `_ping_loop` 每 `PingInterval`(服务端下发，默认120s) 发 PING，**永不主动断开** | 硬编码 120s PING；new-version3 **idle>300s 主动断开重连** | ⚠ new-version3 idle 重连为官方没有的行为 |
| CONTROL 帧 | PING→忽略；PONG→读 payload 更新 `ClientConfig` | PING→**回 PONG 帧** | ⚠ 与官方语义方向相反 |
| DATA ack | 业务处理完，把结果回填帧 payload（含 biz_rt）返回 | **收到即 ack**（不等业务结果） | ⚠ 时序/内容都不同 |
| 大消息合包 | `_combine` 按 `sum/seq` 重组 | **无合包** | ⚠ 大分片直接丢 |
| CARD 帧 | 直接忽略 | 尝试解析卡片命令 | 不同取舍 |
| 重连参数 | 服务端 `ClientConfig` 下发：ReconnectCount/Interval/Nonce/PingInterval | 全硬编码（new-version3 2s→15s） | ⚠ 不协商，易与平台漂移 |
| 多 bot 编排 | 官方无 BotManager（每 client 独立） | BotManager + lease + 热加载 | 手写更完整 |

### 2.3 结论

协议层同构，但手写在**连接生命周期协议语义**上是官方能力的**简化子集**：
- 合包缺失、ack 语义偏差、PONG/ClientConfig 不消费、重连参数不协商、new-version3 自造 idle 强制断开。

---

## 3. 现状问题根因（new-version3 失效推演）

**现象**：WS 连接建立成功、keepalive PING 正常、但 `received message` / `[WS] event_type` 全程为 0。

**根因——new-version3 新增的 idle 强制重连打乱长连接**：

```python
# feishu_async.py _ping_keepalive（new-version3 独有）
idle_secs = time.time() - ctx.metrics.last_user_message_at   # 仅用户消息刷新
if idle_secs > 300:
    await ws.close()          # ← 主动断开
    break                     # → 每 ~6 分钟全量重建所有 bot 连接
```

- 低频 bot（没有用户消息）每连接满 5 分钟即被断开 → ENDPOINT 重新获取 → 建立新连接。
- 11 个 bot 每 ~6 分钟一轮批量 `forcing reconnect`（远端日志：99 次连接、单 bot 各 6 次），形成**高频连接重建风暴**。
- 飞书 Connection 服务对同网关高频重建判定连接/会话不稳定 → **事件通道被冻结**（连接仍可建、PING 仍可发，但零事件帧）。
- 该机制 new-version2 **不存在**（new-version2 只 PING，永不主动断开）→ 与"昨天 new-version2 正常、今天 new-version3 挂"吻合。

---

## 4. 优化方向（保持多 Bot 支持能力）

> 目标：在保留"单事件循环 + 每 bot 一条协程 + BotManager 编排"多 bot 能力的前提下，把连接生命周期语义对齐官方协议，消除造成失效/丢消息的机制。

### 4.1 移除/放开 idle 强制重连（P0，止血）

- **立即**：删除 `_ping_keepalive` 中的 `idle>300s → ws.close()` 逻辑，恢复 new-version2 行为——只 PING，永不主动断开。
- 若担心"WS 假死"（TCP 未断但事件不再到达）：
  - 改为**基于事件收帧超时**判定（一段时间未收到任何帧含 PING/PONG/CONTROL/DATA），而不是基于用户消息；
  - 或用飞书官方"PONG 中含配置推进"亦可。
  - 触发后**单 bot 重连**，不要整批重建。

### 4.2 消费服务端 ClientConfig（P1）

- `get_ws_endpoint` 解析响应里的 `ClientConfig`（PingInterval / ReconnectCount / ReconnectInterval / ReconnectNonce）。
- `_ping_keepalive` 用 `PingInterval` 替代硬编码 120s；重连退避用服务端参数替代写死值。
- 收 `PONG` 帧时，若 payload 有 ClientConfig 则更新本地配置（对齐官方 `_handle_control_frame`）。

### 4.3 PING/PONG 语义对齐官方（P1）

- CONTROL 收到 PING：**保持连接即可**（官方选择忽略；若飞书实践要求回执，则保留回 PONG 但不要因此更新配置向）。
- CONTROL 收到 PONG：**读取 payload 中 ClientConfig 更新本地参数**。

### 4.4 ack 语义对齐（P1）

- 改为**业务处理完成后回 ack**：把 handler 返回结果（成功 `{"code":200}` / 失败 500）回填 DATA 帧 payload 再发送，并带 `biz_rt`（与官方一致）。
- 保留"立即 ack 防重推"考虑时，可两者结合：收到即回最小 ack + 业务完成后回业务 ack（需与飞书 Composer 约定确认）。

### 4.5 大消息合包（_combine）（P2）

- DATA 帧 header 含 `sum>1`/`seq`：实现按 `message_id` 的分片缓存重组（5s 窗口，官方 `_combine` 同款）。
- 缺失时大分片（长文本消息）整段丢弃，必须补。

### 4.6 保留/强化多 Bot 编排（不降级）

- **保留**：单事件循环 + 每 bot 一条 `run_bot` 协程；`BotManager`（load / lease sweep / health / hot-reload）。
- **保留 lease**：多实例部署时避免同 bot 多实例并发连接（lease 是正确能力，与问题无关，不删）。
- **建议**：为 lease sweep / 5 分钟 reload 加"连接已建立则不重建"的稳定性保护（避免每次 reload 引起不必要状态抖动）。
- 可选项：将每 bot 连接收敛为**按网关(fpid/service_id)复用连接 + 帧内路由**——但这是大重构，官方协议是否支持多 bot 单连接需向飞书确认后再做；默认保持"每 bot 一连接"（与官方一致）。

### 4.7 观察/监控（P2）

- 指标：`messages_received/sent/failed`、`reconnect_count`、`last_error` 已存在（`BotMetrics`）——接入 `/metrics` 或健康接口方便预警。
- 增加"收到任何帧(含 PING/PONG)"的计数，区分"连接假死"与"平台停推"。

---

## 5. 优化优先级汇总

| 优先级 | 动作 | 影响 |
|---|---|---|
| P0 | 删除 idle 强制重连，回归只 PING | 止血，最可能解决事件零到达 |
| P1 | 消费 ClientConfig（Ping/Reconnect）+ PONG 更新 | 连接参数与平台同步 |
| P1 | ack 语义对齐（处理完成后回执 + biz_rt） | 平台连接状态判定正确 |
| P2 | 大消息合包 `_combine` | 消除大消息丢帧 |
| P2 | 多 bot 连接复用（按网关） | 待飞书协议确认；可选 |
| P2 | 监控/健康告警增强 | 可观测性 |

> ⚠️ 以上为分析/方向，均未改代码。实施前建议先用 P0 止血并观察日志 `received message` 是否恢复，再逐项对齐协议。

---

## 附：参考文件

- `src/im/feishu_async.py`（手写实现）
- `lark_oapi/ws/client.py`（官方 SDK，本地 site-packages）
- `docs/new-version/im-ws-async-redesign.md`（历史 SDK 方案设计背景）
- `docs/new-version/feishu-dual-impl-remediation.md`（双实现收敛背景）