# Issue #1613 Plan（分批与提交映射）

## 分批

| 批 | 内容 | 落点 |
|---|---|---|
| M1 接入 + 文本 | getUpdates 长轮询 / offset 持久化 / edited_message 忽略 / 群内剥 @botname / 文本收发 | `channel.py`、`inbound.py`、`outbound.py`、`markdown.py`、`engine_wiring.py`、`chat.py`、`im_channel.js` |
| M2 媒体 | 入站 `file_id` 引用 + getFile 下载（20MB 上限 + 超限回执）；出站 sendPhoto/sendDocument（50MB） | `inbound.py`、`resolvers.py`、`outbound.py` |
| M3 卡片 | 短码编解码 + `c:<cid>` 台账；InlineKeyboardMarkup；`callback_query` 先 ack 再派发；editMessageText 原地更新 | `cards.py`、`renderers/telegram.py`、`channel.py`、`outbound.py` |
| M4 打磨 | 429 `retry_after` 退避、HTML 解析失败回退纯文本、reaction 回执 | `outbound.py`、`markdown.py` |

## 装配（符号定位，不按行号）

- `registry.register(Type.TELEGRAM, self._telegram_factory)`
- `_telegram_factory` → 复用 `_channel_factory(cfg, "telegram", ..., sender_factory=...)`
- `_card_renderer_for("telegram") → TelegramCardRenderer`
- `BUILTIN_CARD_RENDERERS += ("telegram",)`
- `_card_payload_empty` 增加 `{text, reply_markup}` 分支
- `_IM_PROVIDERS += ("telegram",)`（单 Bot Token，GET 回显脱敏）

## 关键决策

- 接入走长轮询而非 webhook（零公网）；文本用 `parse_mode=HTML` 而非 MarkdownV2（转义面小）；
- 按钮总数 ≤25、每行 ≤8（与 Slack 对齐）；
- offset 持久化在 `channel_installation.config.telegram_offset`（引擎核心零改动）；
- 卡片 `cid → action_payload` 台账进程内共享（renderer 写入、channel 回调回查）。