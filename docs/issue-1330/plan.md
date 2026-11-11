# Issue #1330 实现方案

## 1. 复用策略

PR #1000（head `feat/issue-1309` @ `9fa99aa2`，base `weekly-09-IM` @ `cbe620bef`）已含完整实现，
且与当前 `weekly-09-IM` tip 完全同基线。为避免重写，直接以该提交的文件内容为基线补齐：

```
git diff HEAD origin/feat/issue-1309   → 应用到 feat/issue-1330
```

应用后按当前基线复核装配、路由、前端与单测，`tests/im` 全量通过。

## 2. 模块落点

| 层 | 文件 | 说明 |
|---|---|---|
| 接入 | `src/im/adapters/slack/channel.py` | Socket Mode 连接、ack 先行、心跳、重连、租约让渡 |
| 接入 | `src/im/adapters/slack/inbound.py` | envelope/事件/卡片回调 → `InboundMessage` |
| 发送 | `src/im/adapters/slack/outbound.py` | `SlackSender` + `SlackClient` + `SlackAdapterApi` |
| 文本 | `src/im/adapters/slack/markdown.py` | Markdown → mrkdwn、38000 分块、围栏续接 |
| 卡片 | `src/im/adapters/slack/cards.py` | Block Kit 组装、回调值与命令解析 |
| 卡片 | `src/im/channel/renderers/slack.py` | Card IR → Slack Block Kit，元素上限降级 |
| 解析 | `src/im/adapters/slack/resolvers.py` | 路由台账、用户解析、媒体下载 |
| 装配 | `src/im/engine_wiring.py` | `_slack_factory` + 注册 + renderer/validator + `extra_config` |
| 前端 | `src/api/static/js/pages/im_channel.js` | Slack 平台卡、双令牌表单、状态 |
| 前端 | `src/api/static/img/slack.svg` | Slack 图标 |
| 后端 | `src/api/routes/chat.py` | provider 白名单、回显脱敏、双令牌写穿 |

## 3. 关键取舍

- **双令牌**：`im_app_id` 存 Bot Token（敏感，GET 脱敏为 `********`），`im_app_secret`
  存 App Token；安装 `config` 同时落 `bot_token` / `app_token` 语义键。
- **去重键**：`event_id`（不用 `ts`，编辑会产生同 ts 新事件）。
- **ack 先行**：读循环先回 envelope，再异步解析分发，避免 3 秒超时重投。
- **线程**：`thread_ts` 存在即落线程，顶层消息不传。
- **降级可见**：超 25 元素 / 50 blocks 记录降级原因，不发出会被 Slack 拒收的 payload。

## 4. 测试方案

- 4 组单测覆盖真实 envelope 形态（字符串 payload、重投 event_id、忽略 subtype、@ 前缀剥离）。
- 覆盖 ack 时序、分块边界（中文）、mrkdwn 转换、按钮上限、429 退避、文件三步上传。
- 运行 `python3 -m pytest tests/im` 验证新增与三渠道回归。