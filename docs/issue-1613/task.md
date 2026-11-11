# Issue #1613 Task（任务拆解）

## M1 骨架
- [x] `adapters/telegram/channel.py`：getUpdates(timeout=25)、allowed_updates、offset=update_id+1、edited_message 忽略、租约/异常隔离
- [x] `adapters/telegram/inbound.py`：Update → InboundMessage、群内剥 @botname、文本
- [x] `adapters/telegram/outbound.py`：sendMessage、分块、HTML
- [x] `engine_wiring.py`：注册 `_telegram_factory`、offset 持久化
- [x] `chat.py` + 前端平台卡 + `telegram.svg` + 单 token 凭据表单

## M2 媒体
- [x] 入站 photo/document/voice/video → MediaRef(file_id)，20MB 超限标记
- [x] `resolvers.py`：getFile + 下载 + 缓存
- [x] 出站 sendPhoto / sendDocument（50MB，url 直传或 multipart）
- [x] 超限回执

## M3 卡片
- [x] `cards.py`：短码编解码、`c:<cid>` 台账、按钮上限降级
- [x] `renderers/telegram.py`：Card IR → `{text, reply_markup}`
- [x] `callback_query` 先 answerCallbackQuery 再异步派发
- [x] editMessageText 原地更新

## M4 打磨
- [x] 429 `parameters.retry_after` 退避
- [x] HTML 解析失败回退纯文本（`card_text_fallback`）
- [x] setMessageReaction 表情回执

## 测试
- [x] `tests/im/test_telegram_channel.py`
- [x] `tests/im/test_telegram_inbound.py`
- [x] `tests/im/test_telegram_outbound.py`
- [x] `tests/im/test_telegram_card.py`
- [x] 既有 `tests/im` 全量通过（522 passed）

## 待维护者确认（P0）
- [ ] 网络出口（HTTPS_PROXY 或可达节点）确认后做 Telegram 真机联调
  （境内直连 `api.telegram.org` 超时，本轮未勾选真机验收项）