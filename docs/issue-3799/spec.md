# Issue #3799 Spec：微信渠道 ret=-14 风控暂停的可观测 / 降级 / 恢复与日志收敛

> 需求权威来源：Issue #3799 正文 + issue agent 已发布的 Spec（评论 202854271）。
> 目标合入分支：`arch-weekly-11-ljt`；开发分支：`feat/issue-3799`。

## 目标

微信 ilink 适配器遇到 `ret=-14`（session timeout，账号级风控）时，渠道不再以「WARNING + `sleep 3600`」
的形式静默失效：渠道不可用状态必须**可观测、可告警、可人工恢复**，暂停期间的通知要有**降级路径**，
`notifystart/notifystop` 失败日志要按渠道状态**聚合**（每实例每小时 ≤1 条）。

## 现场事实（Issue 正文）

- 自 2026-11-21 起 11 个微信 channel 实例每小时被 `ret=-14` 暂停一次，24h 累计 309 条
  `session paused by server`，24×7 实际不可用；
- `src/domains/runtime/im/adapters/wechat/channel.py::_on_session_paused` 只做：
  `session_paused += 1` → `record_degraded` → WARNING → `sleep(3600)`；不进管理台、不告警、
  上层无「渠道不可用」感知；
- `notifystart/notifystop` 失败（channel.py / outbound.py 各 2 处）逐条 WARNING，
  暂停期间每小时 30 条刷屏，与真实异常混叠；
- 通知扇出（`notify._deliver`）不看渠道健康：暂停期间仍逐渠道尝试 → 失败即落 outbox `failed`，
  微信触达静默丢失。

## 范围

### In
1. 渠道健康状态机（`active/paused`）+ 持久化状态（`last_pause_at`/`next_retry_at`/连续暂停次数/
   告警态），实例维度（`instance_id` + `installation_id`）。
2. `session_paused_risk` 指标与告警事件：
   - 同实例「连续 ≥2 次暂停」= 距上次暂停 ≤2h 内再次暂停（覆盖小时级循环）；
   - 或全实例 1h 内全部暂停（≥2 个安装）→ 立即告警（覆盖本次事故形态）。
   告警落 ERROR 结构化日志（含 `alert=wechat_channel_unavailable`）+ 健康表 `alert_state=firing`，
   管理台/API 可检索。
3. 管理台渠道卡片：`paused / last_pause_at / next_retry_at / consecutive_pauses / alert_state`
   （`GET /api/im/installations` 直接带出 + 新增 `GET /api/im/channels/health`）。
4. 人工恢复入口：`relogin / clear_pause / retry_now`（新增 `POST /api/im/installations/{id}/recover`），
   `retry_now` 唤醒暂停等待，重连成功后 `notifystart` 再次执行、`getupdates` 恢复并清除 paused。
5. 通知降级：微信 `paused` 时不投递，改由同一收件人其它健康 IM 渠道送达（同一次扇出内），
   落降级记录（`degraded_count / last_degraded_to / last_degraded_ok`）；无其它渠道时保留 outbox
   待投递行（不丢内容）并记为 `channel_paused`。
6. 日志收敛：`notifystart/notifystop` 失败按 `(instance, reason, 小时窗口)` 聚合，窗口滚动只发 1 条汇总。

### Out
- 微信协议层规避平台风控（`ret=-14` 账号级，代码不可自愈）；
- 其它 IM（飞书/钉钉/企微/Slack/Telegram）的重连语义（#3795/#3735/#3475）；
- 新增外部监控/告警系统，复用现指标与日志体系。

## 验收标准

- [ ] 暂停后状态 ≤5min 在管理台/API 可见（`paused/last_pause_at/next_retry_at`），并留下可检索事件；
- [ ] 触发阈值（单实例连续 2 次 / 全实例 1h 均暂停）时产出 ERROR 告警行 + `alert_state=firing`；
- [ ] 暂停期间的扇出有明确降级记录；有其它健康渠道时该渠道 `success`；
- [ ] 人工 `retry_now/relogin` 后 `notifystart` 成功、`getupdates` 恢复、paused 清除（有测试覆盖）；
- [ ] `notifystart/notifystop` 失败日志每实例每小时 ≤1 条汇总；
- [ ] 现有测试 + 新增测试通过。