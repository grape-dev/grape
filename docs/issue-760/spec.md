# Issue #760 需求规格（spec）

## 目标

在 #742 P0 骨架（im_channel 契约层 / im_engine 管线 / feishu 适配器骨架，已合入 weekly-09-IM）基础上，落地 **P1 里程碑**：引擎从 `channel_installation` 枚举安装（双轨切换第 2 步）、飞书 WS 生命周期（run_bot 硬功夫 A6）迁入 `FeishuChannel.connect()`、VLM 媒体理解挪出 handler 协程进 worker（P4 修正）。为 P2 钉钉接入（im_engine 核心 diff=0）铺路。

## 范围（本 PR）

| # | 交付物 | 验收 |
|---|--------|------|
| 1 | `src/im/engine/installations.py`：`SqliteInstallationProvider`（Supervisor 安装来源切到 channel_installation）+ `reconcile_installations`（legacy 双轨对账，diff 输出） | 单测：活跃行枚举、status 过滤、legacy↔new 双向 diff |
| 2 | `src/im/engine/media.py`：`MediaWorkQueue`——Router 流水线 ⑦ 前仅入队 MediaRef，`describe`（VLM）异步执行，结果经回调补进会话上下文 | 单测：入队不阻塞流水线、结果回调、失败不扩散 |
| 3 | `src/im/adapters/feishu/channel.py`：`connect()` 实现——run_bot（feishu_async.py L1181-1431）A6 语义原样迁入：协议级 ping 禁用、应用层 PING 保活（120s）、空闲 5min 强制重连、ack 帧防重投（解析失败也 ack）、seen 去重持久化（A3）、fire-and-forget + semaphore、永久错误码 10002/10003/10014 → PermanentChannelError（A5）、重连后 lease 复查 | 单测（fake ws + fake endpoint）：永久错误短路、每 DATA 帧 ack、PING→PONG、去重 drop、lease 丢失优雅退出、InboundMessage 翻译进 handler |
| 4 | Router ⑦ 前媒体步从 P0 stub 接到真实 `MediaWorkQueue.enqueue` | 既有 test_media_vlm_not_in_handler 保持绿 |

## 非范围（明确不做）

- 不切换运行时入口（`src/api/main.py` 仍走 legacy BotManager；灰度切换/回滚开关属运维发布动作）；
- 不动 legacy `feishu_async.py`/`handler_async.py` 行为（双写期零 diff 要求）；
- 不做钉钉/Slack 适配器（P2）；
- 不引入 Redis/PG 租约（陷阱 13 仅预置触发条件注释）。

## 依赖铁律（继承 #742）

im_channel 零平台依赖；im_engine 只 import im_channel；适配器永不 import im_engine 内部；核心永不读 `InboundMessage.raw`。

## 参考

设计文档 v2.0（Issue #760 附件）：§4.2 Supervisor / §4.3 媒体预算 / §5.3 双轨迁移第 2 步 / §8 P1 验收。
