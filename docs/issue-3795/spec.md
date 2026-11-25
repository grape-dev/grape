# Issue #3795 Spec：飞书 WS 3003 ping timeout 被记为 unexpected crash

> 需求权威来源：Issue #3795 正文 + issue agent 已发布的 Spec 评论。
> 目标合入分支：`arch-weekly-11-ljt`；开发分支：`feat/issue-3795`。

## 目标

飞书 WS 传输层关闭（3003 ping timeout 等关闭码）不再被记为
`[im-engine] channel crashed (unexpected)` ERROR，而是按「可重连的链路关闭」归因：
WARNING 单行 + `ws_close_<code>` 指标维度。恢复「服务端主动关闭」与「适配器 bug」的可辨识度。

## 现场事实

- 生产 2026-11-23 00:19–00:50 CST：`channel crashed (unexpected)` 33 条/小时、25 个实例
  （master-003 17 / -004 14 / -001 1 / -002 1）；
- 异常原文 `ConnectionClosedError: received 3003 (registered) ping timeout; then sent 3003`；
- `ping_interval=None`（协议级 ping 已禁用，保活由应用层 PING 负责）→ 3003 属预期内关闭；
- 基线已含 #3698 归一（`connect()` 的 `async with` 整块 `except Exception → _ws_failure()`），
  但归一结果是无码 `ChannelError`，supervisor 只记 `ws_transport`，关闭码在归因中丢失。

## 范围

### In
1. `im/channel/channel.py`：新增 `ChannelWsClosed(ChannelError)`，携带 `close_code`，
   `str()` 含 `ws closed code=<code>`；
2. `im/adapters/feishu/channel.py`：`_ws_failure()` 对带关闭码（非 3001）的关闭返回
   `ChannelWsClosed`；无关闭码的传输异常保持普通 `ChannelError`；
3. `im/engine/supervisor.py`：新增 `except ChannelWsClosed` 分支 → WARNING 单行 +
   `ws_transport`（口径不断档）+ `ws_close_<code>`，永不 `log.exception`；
   对未归一而泄漏的 websockets `ConnectionClosed`（duck-typing 取码，engine 不 import
   websockets）同样走该 WARNING 路径；
4. `im/engine/metrics.py`：`ws_close_cause()` 有界分桶（数字码原样 / 4000+ → `ws_close_4xxx`
   / 无码 → `ws_close_unknown`），`record_ws_close()` 记关闭码维度抖动；
5. 单测：适配器归一、supervisor WARNING 归因、泄漏 `ConnectionClosed`、分桶与
   duck-typing 取码、真 bug 仍为 ERROR。

### Out
- 不改 websockets 版本与 `ping_interval` 策略；
- 不重构 #3735 的 ClientSession 生命周期；
- 不改其它渠道重连语义、握手与 ack 业务逻辑；
- 生产 LogQL 观测验证由 sre 发布后执行（不在本 PR 范围）。

## 验收标准

- [x] 单测：飞书 `connect()` 收到 `ConnectionClosedError(3003)` → 抛 `ChannelWsClosed` 且
      `close_code==3003`，非裸异常；
- [x] 单测：supervisor 收到该异常 → 无 `channel crashed (unexpected)`、WARNING 含
      `ws closed code=3003`；`flap_by_cause` 出现 `ws_close_3003`，`reconnects` 与
      `ws_transport` 同步 +1；
- [x] 单测：泄漏的 duck-typed `ConnectionClosed`（1006）同样 WARNING + `ws_close_1006`；
- [x] 单测：`AttributeError` 等真 bug 仍按 `channel crashed (unexpected)` ERROR 暴露；
- [x] 现有 `tests/runtime/im/` 无新增失败（13 个基线已红用例前后一致）；`make lint`、
      `scripts/check-arch.py`、silent-except ratchet 通过。