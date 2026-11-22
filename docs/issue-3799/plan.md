# Issue #3799 方案设计：渠道健康状态机 + 风控告警 + 降级路由 + 日志聚合

## 分层与数据流

```
ilink getupdates → WechatApiError(ret=-14)
        │
        ▼
WechatChannel._on_session_paused()            ← 状态机入口（不再只有 WARNING + sleep）
        │  ChannelHealthTracker.record_pause()
        ├──→ 内存状态（per installation，实例维度）
        ├──→ grape_t_channel_health（runtime 域，跨进程/管理台可见）
        ├──→ EngineMetrics.channel_paused_by_instance{inst}  （/health/realtime im 段）
        └──→ 阈值判定 → ERROR 告警行 alert=wechat_channel_unavailable + alert_state=firing
        │
        ├── 暂停等待改为「可唤醒 sleep」：ctx.wait() 与 retry_wake（人工 retry_now）二选一先到
        ▼
下一次 getupdates 成功 → record_resume() → paused=0 / alert_state=resolved / notifystart 重新执行

通知侧：
notify_user() ── 逐 target 检查 ChannelHealthTracker.is_paused(inst)
        ├── 未暂停 → 原路径 _handle_target()
        └── 已暂停 → 不投递，记 channel_paused；同一次扇出里其它健康渠道的 success = 降级结果，
                     无其它渠道 → outbox 行保留 pending（内容不丢）并落 degraded 记录
        ▼
outbox 消费者：_deliver 返回 reason=channel_paused → release_sending（不计 attempt，不算失败）
```

## 关键设计

### 1. 状态载体：`grape_t_channel_health`（owner=runtime）

- 列：`installation_id`(PK)、`channel_type`、`instance_id`、`paused`、`last_pause_at`、`next_retry_at`、
  `pause_count`、`consecutive_pauses`、`last_pause_reason`、`last_resume_at`、`alert_state`、
  `alerted_at`、`alert_reason`、`degraded_count`、`last_degraded_at`、`last_degraded_to`、
  `last_degraded_ok`、`last_recover_action`、`last_recover_at`、`updated_at`。
- 为什么不复用 `config`：`config` 参与 Supervisor 指纹（除 `cursor` 外任一键变化即热重建连接），
  写健康态会自我触发重建；独立表也满足「进程重启/多实例一致可见」。
- 时间统一 UTC epoch REAL；API 层输出 `YYYY-MM-DD HH:MM:SS`（与 outbox/bot_lease 口径一致）。

### 2. 告警阈值（可配置常量）

| 条件 | 判定 | 覆盖场景 |
|------|------|----------|
| 单实例连续 ≥2 次 | 距上次暂停 ≤ `PAUSE_REPEAT_WINDOW_SECONDS=7200` 内再次暂停 | 小时级「暂停→恢复→再暂停」循环 |
| 全实例 1h 均暂停 | `PAUSE_ALERT_WINDOW_SECONDS=3600` 内 paused 数 == 已知微信安装数（≥2） | 本次 11 实例同时被风控的事故形态 |
| 首次单实例暂停 | 只记事件（WARNING + 健康表 + 管理台），`alert_state=pending` | 抑制单次抖动 |

告警可检索：ERROR 日志 `[im-channel-health] alert=wechat_channel_unavailable inst=... type=wechat instance=... reason=...`
+ `GET /api/im/channels/health` 返回 `alert_state/alerted_at/alert_reason`。

### 3. 日志聚合（`HourlyLogAggregator`）

- 键 `(instance, reason)`，窗口 3600s；同一窗口内只累加，**窗口滚动时**输出上窗口 1 条汇总
  （`count` + 首条样本 + 时间范围）；`flush()` 在 disconnect/shutdown 时补输出当前窗口（count>1）。
- 落到 4 个失败点：`channel._notify_start/_notify_stop`、`WechatSender.notify_start/notify_stop`。

### 4. 降级路由（`notify.py`）

- `notify_user` 先按健康态把 targets 分成 `active / paused`；paused 不投递（避免把 outbox 行打成 failed）。
- 降级结果 `record_degraded_delivery(inst, ctype, to=<健康渠道，空=无>, ok=<该渠道是否 sent>)`。
- 同一次扇出里其它健康渠道的 success 即「改用 X 且成功」；一个都没有 → `no_fallback`，outbox 行保持
  pending（恢复后由消费者补投），并在健康表里可见 —— 不静默丢内容。

### 5. 人工恢复（`POST /api/im/installations/{id}/recover`）

| action | 语义 |
|--------|------|
| `retry_now` | 唤醒暂停等待（`channel.request_retry()`），立即重试 getupdates；成功即 `record_resume`（paused 清除、alert resolved） |
| `relogin` | `retry_now` + `supervisor.kick()`（不等 30s 扫描窗口）+ 标记 `last_recover_action=relogin`；凭据若仍失效会再次 paused 并按阈值继续告警（不假装恢复） |
| `clear_pause` | 只清暂停态/连续计数（不动连接），用于人工确认「已另行处理」后复位看板 |

恢复过程 `notifystart` 由既有 `connect()` 生命周期再次执行；`getupdates` 恢复由首次成功轮询判定。

### 6. 测试 seam

- `ChannelHealthTracker`（纯逻辑 + 可注入 `store/metrics/clock/installations_provider/alert_sink`）；
- `HourlyLogAggregator`（可注入 clock）；
- `WechatChannel` 既有 seam（`update_fetcher` / `sleep` / `metrics`）+ 新增 `health`；
- `notify_user(targets=[...], engine=FakeEngine)` 既有 seam；
- mgmt 路由用 `TestClient` + 依赖覆盖。

## 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 健康检查误判把正常发送降级 | High | 无健康态（tracker 缺失/未暂停）→ 原路径，零行为变化；tracker 异常一律 fail-open |
| 健康表写阻塞事件循环 | Med | 暂停/恢复低频（小时级）+ 短事务；读侧在 API 线程 |
| 跨域引用（runtime 写 mgmt 表） | Med | 新表 owner=runtime，SQL 只在 `runtime/im/channel_health.py`；管理台经 runtime public 契约读 |
| 恢复入口在无引擎进程被调用 | Low | 返回 `engine_unavailable`，不改状态（不假装成功） |