# Issue #3698 Spec：飞书渠道 3001 并发注册冲突的归一、归因与退避

> 需求权威来源：Issue #3698 正文（Grafana 日志）+ issue agent 已发布的 Spec（评论 202756113）。
> 目标合入分支：`arch-weekly-11-ljt`；开发分支：`feat/issue-3698`。

## 目标

飞书 IM 渠道在飞书侧返回 `3001 (registered) too many connections` 时，不再被 Supervisor
打成 `[im-engine] channel crashed (unexpected)` 全栈 ERROR，也不再以 15s 固定间隔无限重试；
该类故障必须**可归因、可退避、可告警**，且同一 `app_id` 不产生并发注册连接。

## 日志事实（Issue 正文，2026-11-21 12:34:02 ~ 12:35:34 UTC）

- 7 条 ERROR 全为同一安装 `inst=63710215-49b4-4bb4-b0c3-4c3820307330`、`type=feishu`；
- 相邻间隔 15.235 / 15.241 / 15.251 / 15.272 / 15.314 / 15.321s ≈ `BACKOFF_MAX=15.0`
  + 建连开销 → 退避已饱和，期间从未注册成功；
- 异常链：`feishu/channel.py:156 async for raw in ws` →
  `websockets.exceptions.ConnectionClosedError: received 3001 (registered) too many connections`；
  其中 12:35:03 一条外层先是 `BrokenPipeError: [Errno 32] Broken pipe`，被同一条 3001 覆盖。

## 契约违背

- `adapters/feishu/channel.py` `connect()` 文档（L96-101）与 `channel/channel.py` Channel 契约
  （L72-73）都约定「链路断开 → 抛 ChannelError」，但飞书适配器的 `async with ws_connect(...)`
  与接收循环外层**没有异常归一化**；`adapters/slack/channel.py` 同位置已有
  `except Exception -> ChannelError` 包装。
- 因此 websockets 原生异常直接冒泡到 `engine/supervisor.py` 的通用 `except Exception`，
  被记成 "channel crashed (unexpected)" + 全栈，且沿用 15s 封顶退避。

## 范围内 / 范围外

**范围内**：飞书 `connect()` 传输层异常归一；Supervisor 对 3001 的独立归因、退避升级与告警；
同一 `app_id` 的连接级去重校验；回归用例。

**范围外**：飞书侧并发连接配额调整；slack/dingtalk/wecom/telegram 适配器行为变更；
消息去重、卡片渲染等无关逻辑。

## 验收标准

- [x] 同类断连不再出现 `channel crashed (unexpected)` 全栈 ERROR；断连按 `ws_register_conflict`
      / `ws_transport` 归因，WARNING 记录本次重试间隔。
- [x] 单测覆盖：飞书 `connect()` 遇 `ConnectionClosedError(3001)` / 普通关闭码 / `BrokenPipeError`
      均抛 `ChannelError`（3001 为 `ChannelRegistrationConflict`），`CancelledError` 原样放行。
- [x] 连续 3001 冲突的退避从 5s 起翻倍至 300s 封顶（突破 15s），连续 5 次后升级为 ERROR 告警。
- [x] 同一 `app_id` 的两个安装行只建一条连接、只占用一个租约，被跳过方留显式 WARNING。
- [x] 现有 `tests/runtime/im` 用例保持通过（含一条历史 stale 断言按 `FLAP_CAUSES` 校正）。