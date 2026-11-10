# Issue #760 方案设计（plan）

## 1. 安装枚举切换（§5.3 第 2 步）

```
Supervisor.installation_provider
  P0（现状）：load_eligible_bots 包装（legacy 双轨直读）
  P1（本 PR）：SqliteInstallationProvider
                └─ SELECT ... FROM channel_installation WHERE status='active'
                   （asyncio.to_thread 包裹阻塞 sqlite 读）
  对账：reconcile_installations(conn)
          legacy = chat_agent_session(user 轨, app 去重) ∪ devops_agent_session(repo 轨)
          new    = channel_installation 行
          → {'missing_in_new': [...], 'stale_in_new': [...]}   # diff 告警零触发为验收
```

- provider 不感知表创建；建表/回填仍走 `scripts/migrations/001_channel_tables.py`。
- legacy 侧查询语义与 `backfill_installations` 完全一致（防陷阱 11：vod 轨 app 共享语义）。

## 2. MediaWorkQueue（P4：VLM 挪 worker）

```
Router._handle ⑦ 前置步：
    media_refs 非空 → media_worker.enqueue(installation_id, binding_key, media_refs)
                      （非阻塞：只入 asyncio.Queue，流水线立即继续）

MediaWorkQueue:
    enqueue(...)              # 非阻塞
    _worker()                 # 常驻消费：await describe(ref) → await on_result(...)
    describe: Callable[[MediaRef, dict], Awaitable[str]]   # VLM 理解（worker 侧）
    on_result: Callable[[installation_id, binding_key, MediaRef, str], Awaitable[None]]
               # 描述补进会话上下文（TTL 缓存语义由调用方保留）
    失败：记日志不扩散（媒体失败不移位，multica 陷阱 7）
```

Router 兼容旧替身：`media_worker` 带 `enqueue` 属性走新路径，纯 callable 保留 P0 行为。

## 3. FeishuChannel.connect()（run_bot A6 原样迁入）

```
connect(ctx):
    seen = _load_seen_msg_ids(app_id)                       # A3
    endpoint, err = await endpoint_fetcher(app_id, secret)
    err ∈ {10002,10003,10014} → raise PermanentChannelError # A5
    lease_checker() is False → return（优雅让渡，L1174 语义）
    async with ws_connect(endpoint, ping_interval=None) as ws:   # 协议级 ping 禁用
        起 _ping_keepalive 任务（120s 应用层 PING；空闲>300s 主动断开重连）
        async for raw in ws:
            CONTROL+PING → 回 PONG（飞书应用层保活，帧头原位改写）
            DATA(EVENT/CARD) → 先 ack 再处理（解析失败也 ack，防重投死循环）
            _parse_event / _parse_card_action → parsed_to_inbound → InboundMessage
            msg_id ∈ seen → skip；否则 seen.add + _persist_seen_msg_id（A3，上限 2000）
            create_task(_safe_handle)（semaphore 限流 fire-and-forget，A4）
    ws 关闭 → raise ChannelError（Supervisor 指数退避 2s→15s 重连）
    ctx.cancelled → return（可重入：新 ctx 再调 connect）
```

注入点（全部可替身）：`endpoint_fetcher`、`ws_connect`、`lease_checker`、
`seen_loader/persist/cleanup`（默认绑 feishu_async 同名函数——复用不复制）。

## 4. 测试设计

| 文件 | 覆盖 |
|------|------|
| tests/im/test_installations.py | 枚举/status 过滤/双向 diff/幂等对账 |
| tests/im/test_media_worker.py | 入队不阻塞、describe→on_result 链路、失败隔离 |
| tests/im/test_feishu_channel_lifecycle.py | 永久错误短路 / ack 先行 / PONG 回写 / 去重 drop / lease 丢失退出 / handler 收到 InboundMessage / ctx 取消可重入 |

全部使用替身（fake ws / fake endpoint / 内存 sqlite），不触网。
