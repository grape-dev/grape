# Issue #1613 Spec（摘要）

> 完整设计见 Issue #1613 评论「📋 Spec Document」与 `docs/weekly-09/im-telegram-adapter-design.md`。

## 目标

按现 IM 引擎「接入 / 发送 / 卡片」三段契约新增 Telegram 渠道适配器：
`getUpdates` 长轮询接入（零公网、零域名）+ Bot HTTP API 出站 + 文本与
InlineKeyboardMarkup 卡片（真回调）。引擎核心（Supervisor / 租约 / 去重 / 指标）零改动。

## 范围

| 项 | 内容 |
|---|---|
| 新增 | `src/im/adapters/telegram/{__init__,channel,inbound,outbound,cards,markdown,resolvers}.py`、`src/im/channel/renderers/telegram.py` |
| 修改 | `src/im/channel/card.py`（BUILTIN_CARD_RENDERERS）、`src/im/engine/egress.py`（空载荷护栏认识 `{text, reply_markup}`）、`src/im/engine_wiring.py`（注册/工厂/渲染器/offset 持久化）、`src/api/routes/chat.py`（provider 化 + 单 token）、前端平台卡与 `telegram.svg` |
| 不做 | webhook 接入、语音转写、结构化卡片、privacy mode 引导 UI |

## 分批验收

| 批 | 内容 | 验收 |
|---|---|---|
| M1 | 长轮询骨架 + 文本入站/出站 + 装配注册 + 前端凭据 | `/help` `/repo` 文本可达、`channels +1`、offset 重启不重投 |
| M2 | 媒体双向：`file_id` 引用 / getFile 下载（20MB 上限 + 超限回执）/ sendPhoto、sendDocument | 媒体引用与 20MB 回执单测通过 |
| M3 | 短码 + `callback_query` 真回调 + 原地更新 | 短码往返、`answerCallbackQuery` 先行、editMessageText 原地更新 |
| M4 | 429 `retry_after` 退避、HTML 解析失败回退、reaction 回执 | 429 退避、纯文本回退、表情回执单测通过 |

## 硬约束

- `callback_data` 1–64 字节：短码 `verb:参数...`，超长走 `c:<cid>` 台账回查，放不下降级
  「回复 `/命令`」并计 `card_action_unsupported`；
- 单条文本 4096 字符（按 3900 分块）；Bot API 下载 20MB、上传 50MB；
- 群聊 privacy mode：只响应 `@bot` 与 `/命令`；
- 境内直连 `api.telegram.org` 超时（P0）：未获网络出口前只交付代码 + 单测，真机验收留待维护者确认。