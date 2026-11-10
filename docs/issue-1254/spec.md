# Issue #1254 Spec（摘要）

> 完整需求与设计见 `docs/weekly-09/im-wecom-adapter-requirements.md`、
> `docs/weekly-09/im-wecom-adapter-design.md`（随本 PR 提交，含实现差异记录）。

## 目标

按现 IM 引擎「接入 / 发送 / 卡片」三段契约新增企业微信（WeCom）智能机器人渠道，
形态与钉钉 Stream 同族：`wss://openws.work.weixin.qq.com` 长连接 + `aibot_subscribe` 认证。
引擎侧（Supervisor / Router / 租约 / 去重 / 指标 / handler）零改动。

## 范围

| 项 | 内容 |
|---|---|
| 新增 | `src/im/adapters/wecom/{__init__,channel,inbound,outbound,resolvers,markdown,cards}.py`、`src/im/channel/renderers/wecom.py` |
| 修改 | `src/im/engine_wiring.py`（注册/工厂/渲染器/校验器）、`src/im/channel/card.py`（内置渲染器清单）、`src/api/routes/chat.py`、`src/api/static/js/pages/im_channel.js`、`scripts/migrations/001_channel_tables.py`（回填） |
| 测试 | `tests/im/test_wecom_{inbound,outbound,card,channel}.py` |
| 不做 | 流式气泡、卡片原地更新、欢迎语、repo 轨第二批、DB schema 变更 |

## 平台硬约束（单测固化）

1. `aibot_send_msg` 只接受 `markdown` / `template_card` —— text 一律转 markdown，payload `msgtype` 永不为 text。
2. `markdown.content` / `stream.content` ≤ 20480 字节，分块必闭合代码围栏。
3. 媒体 URL 5 分钟有效 → 收帧即下载 + AES-256-CBC/PKCS#7 解密，`aeskey` 不落库。
4. 未知 msgtype 不静默（回落 text，`parse_fail` / `unknown_msgtype` 计数）。
5. 非 ChannelError 异常不得穿透接收循环；`body` 可能是 JSON 字符串。