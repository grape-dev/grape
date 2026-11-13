# 设计：Telegram 适配器 —— 接入 / 发送 / 卡片三段

> 2026-11-09｜基线 `origin/weekly-09-IM` @ `afd0a9d6a`（Slack 适配器已合入）
> 目标集成分支：`weekly-09-IM`｜目标流水线：马岩松的开发流水线-2（`6e2dba56-1c39-4885-8598-d4d7fd089a0f`）
> 协议事实来源：Telegram Bot API 官方文档（`core.telegram.org/bots/api`，经 python-telegram-bot v21.6
> SDK 文档交叉核对；官方站在境内直连超时，见 §9 风险 P0）

---

## 1. 一句话结论

Telegram 是**接入最简单、卡片最受限**的渠道：只有一个 Bot Token，接入走 `getUpdates` 长轮询
（零公网、零域名，与钉钉 Stream / 企微 aibot / Slack Socket Mode 同模型），出站走 `sendMessage`
一族 HTTP API，卡片只能表达为**文本 + 内联键盘按钮**（没有 header/tone/section 结构），
按钮回调是**真回调**（`callback_query`）但 `callback_data` 只有 **1–64 字节**，
这是本渠道唯一的硬设计约束。

---

## 2. 与已接入渠道对比

| 能力 | 飞书 | 钉钉 | 企业微信 | Slack | **Telegram（本设计）** |
|---|---|---|---|---|---|
| 凭据 | app_id + secret | AppKey + Secret | Bot ID + Secret | xoxb + xapp 双令牌 | **单 Bot Token**（@BotFather 发） |
| 入站 | WS 长连接 | Stream 长连接 | aibot WS | Socket Mode WSS | **getUpdates 长轮询**（或 webhook） |
| 需公网 | 否 | 否 | 否 | 否 | **否**（长轮询主动连出） |
| 出站 | OpenAPI | sessionWebhook / 机器人 API | aibot_send_msg | chat.postMessage | **sendMessage / sendPhoto / sendDocument** |
| 卡片结构 | lark 卡片 JSON | actionCard / 互动卡片 | template_card | Block Kit | **纯文本 + InlineKeyboardMarkup** |
| 按钮回调 | 有 | 互动卡片才有 | 有（事件令牌不能回复） | 有（block_actions） | **有（callback_query）** |
| 原地更新 | 有 | 有（批5） | 有 | 有（chat.update） | **有（editMessageText）** |
| 线程 | 有 | 无 | 无 | 有（thread_ts） | **部分（reply_to / 论坛 topic）** |
| 表情回执 | 有 | 无 | 无 | 有 | **有（setMessageReaction）** |
| 单条文本上限 | 较长 | 20KB 级 | 2048 字节 | 40k 建议 38000 | **4096 字符** |
| 卡片按钮数 | 多 | 5 | 若干 | 25 | **建议 ≤25（每行 ≤8）** |
| 主要坑 | lark 格式泄漏 | URL-only 按钮 | 事件令牌 | blocks 空载荷 | **callback_data 64 字节** |

**结论**：Telegram 的接入复杂度低于 Slack（单令牌、无握手协议），但卡片表达能力弱于 Slack
（无结构化块），且回调载荷空间只有 Slack 的 1/15。

---

## 3. 平台协议事实（硬约束）

### 3.1 凭据与接入

| 项 | 值 | 说明 |
|---|---|---|
| Bot Token | `123456:ABC-DEF…` | @BotFather `/newbot` 获得；**唯一凭据**，无 secret |
| API 基址 | `https://api.telegram.org/bot<token>/<method>` | 出站全部走这里 |
| 文件下载 | `https://api.telegram.org/file/bot<token>/<file_path>` | 需先 `getFile` 拿 `file_path` |
| 接入方式 A | `getUpdates(offset, timeout=25, allowed_updates=[…])` | **推荐**：长轮询，零公网 |
| 接入方式 B | `setWebhook(url)` | 需 HTTPS 公网地址；**与 getUpdates 互斥**，本设计不选 |

- 长轮询要点：`offset = last_update_id + 1` 推进；`timeout` 设 25s 做服务端挂起，返回空数组即继续；
  网络异常按 Supervisor 的退避重连处理。
- **同一 Bot Token 只能有一个 getUpdates 消费者**（多消费者会互相抢 update / 触发 409），
  与钉钉「一个 clientId 只能一个 Stream」同款约束 → 天然匹配「一安装一连接 + 租约」模型。
- `allowed_updates` 显式声明：`["message","edited_message","callback_query","my_chat_member"]`，
  未声明的事件不推送，省带宽也避免解析无关类型。

### 3.2 入站事件（Update）

| 事件 | 触发 | 备注 |
|---|---|---|
| `message.text` | 单聊/群聊文本 | 群内隐私模式下只有 `/cmd` 与 @bot 可见 |
| `message.photo` / `document` / `voice` / `video` | 媒体 | 取 `file_id` → `getFile` → 下载 |
| `callback_query` | 内联按钮点击 | 含 `from.id`、`data`、`message`；**必须 `answerCallbackQuery`** |
| `my_chat_member` | Bot 被拉入/移出群 | 用于维护可见群列表（可选，第一批可不接） |
| `edited_message` | 用户编辑 | 显式忽略（防同一条被当新消息重复派发） |

字段映射：

| Telegram | 我们的字段 | 备注 |
|---|---|---|
| `update_id` | 幂等键 / offset | **单调递增**，去重与断点续传同一把钥匙 |
| `message.chat.id` | `Source.ChatID` | 负数 = 群/频道，正数 = 单聊 |
| `message.from.id` | `Source.SenderID` | 身份映射与卡片回调归属 |
| `message.text` | `text` | 群消息要剥掉 `@botname` 前缀 |
| `message.message_id` | `MessageID` | 回复目标 / 编辑目标 |
| `message.reply_to_message.message_id` | `reply_to` | 引用回复 |
| `message.message_thread_id` | `thread_root` | 论坛超级群的 topic |
| `message.chat.type` | `ChatType` | `private`→P2P；`group/supergroup`→GROUP |
| `callback_query.data` | 卡片动作 | 64 字节内，需短码解码（§3.4） |

**防坑清单**：
1. `update_id` 必须持久化 offset，重启后从 `last+1` 继续，否则重投/漏投；
2. `callback_query` 不 `answerCallbackQuery` → 客户端按钮一直转圈，且 Telegram 会重投；
3. 群消息在 privacy mode 下收不到普通聊天 → 只能 @bot 或 `/cmd`（**需要在 @BotFather 关 /setprivacy 才能收全量，本设计不依赖**）；
4. 解析异常不得穿透轮询循环（Supervisor 只认 `ChannelError` 才重连）。

### 3.3 出站（HTTP API）

| 用途 | 接口 | 关键点 |
|---|---|---|
| 发文本 | `sendMessage(chat_id, text, parse_mode, reply_markup, reply_to_message_id)` | **text ≤ 4096 字符**（按实体解析后） |
| 发图/文件 | `sendPhoto` / `sendDocument` | 单文件发送上限 50MB（Bot API 侧） |
| 原地更新 | `editMessageText` / `editMessageReplyMarkup` | 只有 Bot 自己发的消息可编辑；文本同样 ≤4096 |
| 表情回执 | `setMessageReaction(chat_id, message_id, reaction)` | 对应 `REACTION` |
| 输入中 | `sendChatAction(chat_id, "typing")` | 对应 `TYPING_INDICATOR` |
| 删除 | `deleteMessage` | 可选，用于"处理中"占位卡清理 |

- **限流**：全局约 30 消息/秒，单聊/单群约 1 消息/秒；超限回 `429` + `parameters.retry_after`（秒）。
  → 发送器必须按 `chat_id` 节流 + 429 退避，长回答分块时尤其容易触发。
- **分块**：4096 字符上限，按 3900 切块（留实体余量），只在段落/换行边界切，块序保序。

### 3.4 卡片（InlineKeyboardMarkup）—— 本设计的核心约束

Telegram 没有结构化卡片，只有：

```
reply_markup = {"inline_keyboard": [[{"text": "1", "callback_data": "b:1"}, ...], ...]}
```

映射规则（Card IR → Telegram）：

| Card IR | Telegram 落地 |
|---|---|
| `title` | 文本首行加粗（`<b>…</b>`） |
| `text` block | 正文段落（HTML parse_mode） |
| `fields` block | `键：值` 行 |
| `divider` | `──────────` 分隔行 |
| `actions` / `row_block` / `button` | `inline_keyboard` 行；每行 ≤8 个，总数建议 ≤25 |
| `page` | 末行 `第 x/y 页` |
| `hint` | 末行提示 |

**硬约束：`callback_data` 1–64 字节（UTF-8）。** 我们的 `action_payload()` 产出
`{"cmd": "/bind 2"}` 已约 15–20 字节可用，但 autopilot 的
`{"cmd":"/finding-dispatch","exec_id":…,"finding_id":…,"scene_id":"6e2dba56-…"}` **必然超限**。

设计（与 Slack 同类问题的更严格版本）：

1. **短码编码**：`<verb>:<a>:<b>`，如 `b:2`（bind 第 2 项）、`h`（help）、
   `fd:12:34`（finding-dispatch 的 exec_id:finding_id），scene 等长参数不进 payload；
2. **超长回退**：短码表放不下的参数 → 生成 `cid`（8 位 base36），
   `callback_data = "c:<cid>"`，映射写入引擎既有 `channel_outbound_card` 台账
   （与钉钉互动卡片 `outTrackId` 反查同一套设施），回调时按 cid 回查完整参数；
3. **超限即降级不静默**：映射表也写不下时，按钮降级为文本「回复 `/命令`」并计
   `card_action_unsupported`（与 Slack >25 按钮的处理一致）；
4. **回答必须立刻 ack**：`answerCallbackQuery(callback_query_id)` 先发，再异步派发业务
   （Telegram 对 ack 有时限，且不 ack 会重投，与 Slack 3s ack 同类）。

按钮数上限：内联键盘每行最多 8 个；总按钮没有官方硬上限，但消息体积与客户端体验都在
25 左右显著下降 → **与 Slack 对齐 25**，超出转「回复 /cmd」文本。

### 3.5 文本方言

- 两套：`MarkdownV2`（转义字符极多：`_ * [ ] ( ) ~ \` > # + - = | { } . !` 都要转义）与 `HTML`（只需转义 `& < >`）。
- **本设计选 `parse_mode=HTML`**：转义面小、不会被用户内容里的符号打崩；
  渲染层把标准 Markdown 的 `**粗**` / `_斜_` / `[文字](url)` 转成 `<b>` / `<i>` / `<a href>`。
- HTML 解析失败（用户内容含未闭合标签）→ 捕获 `Bad Request: can't parse entities` 后以纯文本重发一次，
  计 `card_text_fallback`（内容不丢）。

### 3.6 媒体

- 入站：`message.photo[-1].file_id`（取最大尺寸）或 `document.file_id` → `getFile` →
  `https://api.telegram.org/file/bot<token>/<file_path>` 下载；**Bot API 下载上限 20MB**，
  超出标记为不可处理并回执提示。
- 出站：`sendPhoto` / `sendDocument`（50MB 上限）；URL 可直接传公开链接，本地文件用 multipart。
- 媒体"两跳"纪律与企微/钉钉/Slack 一致：入站只存引用，handler 侧真正用到时再下载，worker 侧再做理解，**绝不内联阻塞 ack**。

---

## 4. 三段落点（与现有四渠道完全同构）

| 断面 | 新增文件 | 装配改动 |
|---|---|---|
| **接入** | `adapters/telegram/{channel,inbound}.py` | `registry.register(Type.TELEGRAM, self._telegram_factory)`（`engine_wiring.py:1160-1166` 同处） |
| **发送** | `adapters/telegram/outbound.py`（Sender）+ `markdown.py`（HTML 转换 + 4096 分块） | `_channel_factory` 复用，只换 Channel/Sender |
| **卡片** | `channel/renderers/telegram.py` + `adapters/telegram/cards.py`（短码编解码 + inline keyboard） | `_card_renderer_for("telegram")`（`engine_wiring.py:1010`）+ `BUILTIN_CARD_RENDERERS`（`card.py:219`） |
| **横切** | `adapters/telegram/resolvers.py`（getChat / getChatMember 缓存） | `chat.py:_IM_PROVIDERS`（`:43`）加 `telegram`；前端平台表 + `telegram.svg`；凭据表单单栏 |

能力位申报：

```
TELEGRAM_CAPABILITIES = Capability.TEXT | RICH_CARD | CARD_ACTION
                      | ATTACHMENT | MESSAGE_EDIT | REACTION
                      | THREAD_REPLY | QUOTE_REPLY | TYPING_INDICATOR
```

不申报：`VOICE`（v1 不做语音转写，voice 按文件处理）。

**注意**：上报 `RICH_CARD` 意味着必须同时有 renderer，否则启动自检告警
（`validate_card_capability`，`card.py:222`）——这是我们自己定的护栏，落地时成对提交。

---

## 5. 三条数据流

### 5.1 入站

```
getUpdates(timeout=25) → Update{update_id, message}
  → offset 持久化（update_id+1）
  → telegram/inbound.py: 剥 @botname、映射 chat/from/text/files、过滤 edited_message
  → InboundMessage → 引擎 inbound_handler（与四渠道同一入口）
  → handler_async 命令/普通消息
```

### 5.2 卡片点击

```
用户点按钮 → callback_query{id, from.id, data="b:2", message{chat.id, message_id}}
  → 立刻 answerCallbackQuery(id)                       ← 先 ack
  → cards.py 解码 "b:2" → action_payload {"cmd": "/bind 2"}
  → 同一条 /bind 命令链路（与"回复 2"完全同一份 _handle_selection）
  → editMessageText / editMessageReplyMarkup 原地刷成"已绑定"
```

### 5.3 主动推送 / 定时任务

```
handler/worker → egress.send_card(Card IR)
  → TelegramCardRenderer: Card → text(HTML) + inline_keyboard
  → egress 空载荷护栏（注意：payload 形态 {text, reply_markup}，需确认护栏认识，见 §9 风险 2）
  → Replier → TelegramSender: 按 chat 节流 → sendMessage
```

---

## 6. 分批计划（4 批，可独立验收）

| 批 | 内容 | 验收 |
|---|---|---|
| **1** | 骨架：`channel.py` 长轮询 + `inbound.py` 文本 + `outbound.py` 文本 + 装配注册 + 前端凭据 | 单聊 `/help` `/repo` 返回**文本**；`channels` +1；长轮询 offset 重启不重投 |
| **2** | 媒体：photo/document → getFile → 下载缓存；出站 sendPhoto/sendDocument | 发图给 bot 能被 agent 理解；bot 能回图 |
| **3** | 卡片：`cards.py` 短码编解码 + `renderers/telegram.py` + `callback_query` 真回调 | 卡片按钮可点，点完原地变"已绑定"，日志有 `answerCallbackQuery` |
| **4** | 横切打磨：429 退避、HTML 解析失败回退纯文本、editMessageText 原地更新、reaction 回执 | 压测长回答分块不触发 429 洪泛；表情回执可见 |

---

## 7. 文件清单与规模估算

```
src/im/adapters/telegram/__init__.py        ~30
src/im/adapters/telegram/channel.py         ~260   长轮询循环 / 租约 / offset 持久化
src/im/adapters/telegram/inbound.py         ~200   Update → InboundMessage
src/im/adapters/telegram/outbound.py        ~380   Sender + 节流 + 429 + 媒体
src/im/adapters/telegram/cards.py           ~180   短码编码 / inline_keyboard 构造
src/im/adapters/telegram/markdown.py        ~150   Markdown → HTML + 3900 分块
src/im/adapters/telegram/resolvers.py       ~120   getChat 缓存 / 路由台账
src/im/channel/renderers/telegram.py        ~150   Card IR → text + keyboard
tests/im/test_telegram_{channel,inbound,outbound,card}.py  ~1100
                                     合计 ≈ 1700–1900 行
```

参考基准：钉钉 1731 行、企微 ~1400 行、Slack ~1900 行（含测试）。

---

## 8. 边界与风险

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| 1 | **境内网络不可达 `api.telegram.org`**（本机实测 curl 超时） | 整条渠道完全不可用 | **P0 前置**：需出口代理（`HTTPS_PROXY`）或部署在可达节点；先验证连通性再开工 |
| 2 | **空载荷护栏不认识 `{text, reply_markup}`** | 卡片全部被拦（Slack 刚踩过同款） | 落地时给 `_card_payload_empty` 加 text+reply_markup 分支 + 回归测试 |
| 3 | `callback_data` 64 字节 | 长参数动作无法表达 | 短码 + `channel_outbound_card` 台账回查（§3.4） |
| 4 | 群隐私模式 | 收不到非 @ 的群消息 | 文档明确"群内必须 @bot 或 `/cmd`"；可选引导关 privacy |
| 5 | 429 限流 | 长回答分块成片失败 | 按 chat 节流 + `retry_after` 退避 |
| 6 | 4096 字符 / 20MB 下载 | 截断或媒体不可用 | 3900 分块；超限明确回执不装成功 |
| 7 | MarkdownV2 转义 | 用户内容打崩整条消息 | 选 HTML + 解析失败回退纯文本 |
| 8 | 单 token 多消费者 | update 互相抢（409） | 一安装一连接 + 租约（现有 Supervisor 已保证） |

---

## 9. 待拍板事项

1. **网络出口**（P0）：生产/本机都无法直连 Telegram，是否需要为该渠道单独配置代理？
   若无代理，本渠道无法联调，设计可以先行但代码无法验收。
2. **接入方式**：确认走长轮询（推荐，零公网）；若未来要 webhook 需公网 HTTPS 域名。
3. **群消息策略**：只支持 @bot / 命令（默认 privacy mode），还是引导用户关闭 privacy mode 收全量？
4. **卡片上限**：按钮总数对齐 Slack 的 25 个，还是 Telegram 可以放宽到 40？（越多越接近消息体积上限）

---

## 10. 工作量总评

适配器本体 ≈ 700–900 行，含测试 ≈ 1700–1900 行，4 个可独立验收的批次。
复杂度低于 Slack（无握手、无双令牌、无复杂 mrkdwn），但卡片层需要一套**短码/台账**
机制来绕开 64 字节限制——这是本渠道唯一需要新设计的地方。
最大不确定性不在代码，而在**网络可达性**（§8 风险 1）。