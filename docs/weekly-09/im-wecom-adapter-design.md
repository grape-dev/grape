# 设计：企业微信（WeCom）适配器 —— 接入 / 发送 / 卡片三段一并落地

> 2026-11-06｜基线 `feat/im-dingtalk-interactive-card`（b3686642a = origin/weekly-09-IM + 钉钉卡片批 5）
> 需求见 `docs/weekly-09/im-wecom-adapter-requirements.md`
> 协议事实来源：上游 multica `server/internal/integrations/wecom/`（Go 实现，约 140 文件）+
> 官方文档 `developer.work.weixin.qq.com/document/path/101463`（aibot 长连接）、`/101031`、`/101138`

## 1. 一段话结论

企微走「智能机器人 + 长连接（WebSocket）」，与钉钉 Stream 是同一类形态（出站入站共用一条连接、无公网回调要求），
所以我们**不需要新建任何基础设施**：只要补一个 `WecomChannel`（接入）+ `WecomSender`（发送）+ `WecomCardRenderer`（卡片），
再在装配层注册。引擎侧（Supervisor/租约/去重/指标/handler）零改动。

## 2. 现状代码事实（三段落点，行号基于 b3686642a）

| 断面 | 契约 | 现有实现 | 企微要做的 |
|---|---|---|---|
| 接入 | `src/im/channel/channel.py:66` `Channel`（`type/connect/disconnect/send`）；**入站不在接口上**，适配器 `connect()` 内跑接收循环并调注入的 `inbound_handler`（依赖倒置，注释见文件头） | `feishu/channel.py`(303) ＋ `feishu/inbound.py`(108)；`dingtalk/channel.py`(346) ＋ `dingtalk/inbound.py`(270) | `wecom/channel.py` + `wecom/inbound.py` |
| 发送 | `engine/replier.py:21` `ChannelSender` Protocol（`send`/`capabilities`）、`:26` `Replier`；`engine/egress.py:78` `HandlerEgress` 按能力位决定降级 | `feishu/outbound.py`(82)、`dingtalk/outbound.py`(551) | `wecom/outbound.py`（Sender） |
| 卡片 | `channel/card.py` Card IR（Card/Block/Action/Tone + `action_payload`）；`channel/renderers/<渠道>.py` 渲平台形态 | `renderers/feishu.py`(144)、`renderers/dingtalk.py`(74) | `renderers/wecom.py` + `wecom/cards.py`（模板卡片投递/回调） |
| 能力位 | `channel/capability.py`（声明不协商） | 飞书 7 位；钉钉 `TEXT|RICH_CARD`（`:34` 注释：actionCard 无回调） | 企微申报见 §4.1 |
| 解析 | 各适配器 `resolvers.py`（chat/sender → 平台用户） | feishu(32)、dingtalk(39) | `wecom/resolvers.py` |
| 装配 | `engine_wiring.py:1130/1132` `registry.register(Type.*)`；`:987` `_card_renderer_for`；`:955-967` `_msg_id_validator_for`；`:1308`/`:1317` 两个工厂，`:1328` `_channel_factory`（飞书/钉钉共用的 Router 装配） | 已注册 FEISHU/DINGTALK | 新增 `_wecom_factory` + 注册 + 两处映射 |

现有适配器规模（作为估算基准）：**钉钉 1731 行**（channel 346 / inbound 270 / outbound 551 / cards 211 / resolvers 39 / markdown 99），
飞书 639 行。企微预计 **1200~1500 行**（多出媒体解密与流式，少掉卡片模板平台校验的探索成本）。

## 3. 企微协议事实（决定实现形状的硬约束）

### 3.1 连接与帧

- 端点：`wss://openws.work.weixin.qq.com`（全网一个，靠订阅帧里的 `bot_id`+`secret` 认机器人）
- 握手后首帧：`aibot_subscribe {bot_id, secret}`，**10s 内**须收到 `errcode=0` ack（否则按连接失败退避重连）
- 心跳：客户端每 **30s** 发 `ping`（服务端回 `pong`）；**读超时 90s** 无字节即判定断链，交 Supervisor 重连
- 信封：`{cmd, headers:{req_id}, body}`，每条请求要带唯一 `req_id`

**入站 cmd**：`aibot_msg_callback`（用户消息）、`aibot_event_callback`（事件，`body.event.eventtype`）
**出站 cmd**：`aibot_subscribe`、`ping`、`aibot_respond_msg`（回复窗口内）、`aibot_send_msg`（主动推送）

### 3.2 两个出站命令的能力边界（最容易踩的一条）

| 命令 | 可用 msgtype | 用途 |
|---|---|---|
| `aibot_respond_msg` | `text` / `markdown` / `stream` | **收到消息后 5 秒窗口内**的回复 |
| `aibot_send_msg` | **仅 `markdown` / `template_card`（不接受 text）** | 窗口外的主动推送；需 `chatid` + `chat_type`(1 单聊 / 2 群) |

> 直接后果：我们的 Sender 若照飞书习惯发 text，会在 `aibot_send_msg` 上被拒。
> 设计：**统一用 markdown 承载**（企微客户端把纯文本走 markdown 渲染，无需额外转义）。

### 3.3 流式气泡

`aibot_respond_msg` 支持 `stream`（`stream.id` 复用同一气泡 + `stream_content` 增量，上限 **20480 字节**）。
可实现"打字机"体验，但需要状态机（轮次开始/增量/封口），v1 不做，见 §10 Q2。

### 3.4 媒体

- 图片/文件/视频：回调里给 **腾讯 COS URL（5 分钟有效）** + 该 URL 专属 **aeskey**（长连接模式每条 URL 一个新 key，无部署级 EncodingAESKey，无需轮换）
- **必须在收帧时立即下载落盘**，否则 5 分钟后 403；密钥随帧携带，不落库
- 语音：企微侧已做语音转文字，回调直接给文本（**不下载、不解密**）

### 3.5 卡片（关键结论）

- 出站可发 `template_card`（`aibot_send_msg` 支持）
- 按钮点击回 **`aibot_event_callback`，`eventtype=template_card_event`** → **这是真回调**：带操作人 `userid`、卡片 `card_id`/按钮 key
- 与钉钉的对比：钉钉 actionCard 只能跳 URL、拿不到点击人；**企微的模板卡片能给真回调** → 能力位可申报 `CARD_ACTION`，
  卡片可原地更新（`update_template_card`）
- 代价：模板卡片需先在企微侧有模板（`template_id` / 卡片结构），属"配置前置"而非"脚本生成"，
  风险等级低于钉钉 widgetInfo（具体待真机验证，见 §10 Q1）

## 4. 适配器设计

### 4.1 能力位申报（`wecom/outbound.py`）

```
WECOM_CAPABILITIES = Capability.TEXT | Capability.RICH_CARD | Capability.CARD_ACTION
                   | Capability.ATTACHMENT | Capability.TYPING_INDICATOR
```

不申报：`REACTION`（无表情回执）、`QUOTE_REPLY`（引用在正文渲染，不是平台引用）、`THREAD_REPLY`（企微无话题）、
`MESSAGE_EDIT`（v1 不做流式/更新；做流式后再评估）。
`CARD_ACTION` 的前提是模板卡片模式可用（与钉钉同构的三件齐守卫），否则降级 `RICH_CARD`。

### 4.2 接入：`wecom/inbound.py`

帧解析 → `InboundMessage` 映射表：

| 企微字段 | 我们的字段 | 备注 |
|---|---|---|
| `body.msgtype` | `MsgType` | text/voice → TEXT；image/file/video → IMAGE/FILE/MEDIA；混合图文 → 拼多段文本 |
| `body.chattype`（1/2） | `ChatType.P2P/GROUP` | 群聊时 `chatid` 才是出站 address |
| `body.from.userid` | `Source.SenderID` | 群成员 userid |
| `body.chatid` | `Source.ChatID` | 单聊可能为空 → 用 sender 作为路由 |
| `body.msgid` | `MessageID`/`EventID` | **去重键**（as 字符串，不假设对象形态） |
| `body.quote` | 引用块（正文渲染） | 只镜像用户文字/媒体占位，不解析成平台引用 |
| `aibot_event_callback` + `template_card_event` | `MsgType.CARD_ACTION`（转成 `/xxx n` 命令） | 复用 `_handle_selection` |
| 未知 msgtype | 发"暂不支持的类型"回执 | **不静默丢**（钉钉事故教训） |

**防坑清单（直接来自钉钉两次事故）**：① `body` 可能是 **JSON 字符串**，先 `json.loads` 再判类型；
② 任何解析异常不得穿透到接收循环（Supervisor 不认非 `ChannelError`，会导致"通道静默死亡"）；
③ 解析失败记 `ws_transport` 计数 + 打帧日志（`GRAPE_IM_DEBUG_FRAMES=1` 同口径）。

### 4.3 发送：`wecom/outbound.py`（`WecomSender`）

```
async def send(out: OutboundMessage) -> SendResult
```

决策表：

| 情形 | 路径 |
|---|---|
| 有 `reply_ctx`（本次消息的回复令牌/气泡 id，5s 内） | `aibot_respond_msg`（text 或 markdown） |
| 无 reply_ctx / 超窗口 / 主动推送（任务完成、通知） | `aibot_send_msg`（**强制 markdown**，需 chatid + chat_type） |
| 卡片 | `aibot_send_msg` + `template_card`（无模板 → markdown 降级 + 计 degraded） |
| 媒体 | 先上传换 `media_id`，再随 markdown/文件消息发送 |
| 空内容 | 硬护栏拒绝（同 `card_empty_guard` 口径） |

长度：单条 markdown 按平台上限截断并附"已截断"提示（流式 20480 字节上限作为参考基准）。

### 4.4 卡片：`renderers/wecom.py` + `wecom/cards.py`

- **业务层零改动**：Card IR 不变；`WecomCardRenderer.render(card) -> RenderedCard(payload, degraded)`
- 渲染映射：`title/分块/字段/divider` → markdown 文本段；`select/row_block` 的 `Action(id,params)` → 模板卡片按钮（带 `key=action_payload 序列化`）
- 有 `CARD_ACTION` → 模板卡片按钮走回调；无模板 → 按钮降级为"回复数字"文本提示并计 `card_action_unsupported`
- 回调入口：`template_card_event` → `inbound` → `InboundMessage(CARD_ACTION)` → 既有 `_handle_selection`（**同一份代码路径**，不另写分支）
- 台账：复用 `channel_outbound_card`（#1095 批 5 已建）+ `engine/outbound_cards.py`（内存→DB 两层），保存 `card_id → chat` 反查

### 4.5 解析：`wecom/resolvers.py`

- `chat_id`：群聊取 `chatid`；单聊取 `sender userid`（企微单聊可能不给 chatid）
- 平台用户映射：`userid` → `unified_user_mapping`/`channel_sender_identity`（复用飞书/钉钉已用路径）

### 4.6 装配：`engine_wiring.py`

```
registry.register(Type.WECOM, self._wecom_factory)   # 1130/1132 同处
_card_renderer_for("wecom") -> WecomCardRenderer()   # :987
_msg_id_validator_for("wecom") -> _always_valid      # :955（chatid 路由，不校验消息 id 形态）
```

`_wecom_factory` 复用 `_channel_factory`（`:1328`）：同一套 Router 装配 + 去重（`_built_apps` 按 Supervisor 活渠道收敛），
只换 `WecomSender` 与 `WecomChannel`。

### 4.7 前端（`im_channel.js` + `chat.py`）

- 去掉 `wecom.disabled`（`im_channel.js:22-27`），卡片可点
- 凭据表单：`bot_id` + `secret`（企微无扫码，只能手填；secret 提交后后端掩码回显 `********`，同飞书口径）
- 状态徽标：`connected` 取 `/api/im/installations` 的 `connected`（Supervisor 活渠道），不臆造
- 回显：`chat.py:1483` 的占位换真实现（`_im_echo("wecom")`）

## 5. 三条链路数据流（逐跳）

**入站**
`WS aibot_msg_callback` → `WecomChannel.connect()` 读循环 → `inbound.py`（JSON 字符串容错、媒体下载、@ 判定）→
`InboundMessage` → 注入的 `inbound_handler` → `Router`（去重 `InboundDedup` / 身份 / `channel_chat_session_binding`）→
`handler_async`（legacy 兼容）→ `HandlerEgress` → `Replier` → `WecomSender.send` → `aibot_respond_msg`

**主动推送（长任务）**
worker 完成 → `HandlerEgress.send_*`（无 reply_ctx）→ `Replier` → `WecomSender.send` → `aibot_send_msg(markdown)` → 失败计 failed + 日志

**卡片点击**
`WS aibot_event_callback(template_card_event)` → `inbound.py` → `InboundMessage(CARD_ACTION, text="/bind 2")` →
Router → `handler_async._handle_selection` → 写库（`repo_im_group_binding` 等）→ 结果卡 → renderer → `template_card`/markdown

## 6. 分批落地（每批可独立验收，不留半成品）

| 批 | 内容 | 验收 | 预估 |
|---|---|---|---|
| 1 | 骨架 + 文本接入/回复：`channel.py`（订阅/心跳/读循环/重连）+ `inbound.py`（text）+ `outbound.py`（respond_msg / send_msg 守卫）+ 装配注册 + 能力位 | 私聊问"你好"有回复；群 @ 有回复；`channels=1` | 约 550 行 |
| 2 | 媒体：入站下载解密（aeskey）、语音转写文本、出站 media_id 上传 | 发图能读、发语音能读、agent 产物能回传 | 约 380 行 |
| 3 | 卡片：`renderers/wecom.py` + `cards.py`（模板卡片投递 + `template_card_event` 回调 + 台账） | `/repo` 卡片按钮可点，点完换仓（与回数字一致） | 约 420 行 |
| 4 | 前端：解禁卡片、凭据表单、状态回显、解绑写穿 | UI 四项真实（配置/启停/解绑/状态） | 约 120 行 |
| 5 | 流式气泡（可选）：`stream.id` 增量 + 封口 | 长答复打字机效果 | 约 250 行 |

批 1 完成即可"能用"，批 3 完成才对齐飞书/钉钉的交互体验。

## 7. 文件清单（新增 / 修改）

```
新增  src/im/adapters/wecom/__init__.py
新增  src/im/adapters/wecom/channel.py      # Channel 实现 + WS 读循环 + 订阅/心跳/重连
新增  src/im/adapters/wecom/inbound.py      # 帧 → InboundMessage（含媒体/引用/@/卡片事件）
新增  src/im/adapters/wecom/outbound.py     # WecomSender（respond_msg / send_msg / 媒体）
新增  src/im/adapters/wecom/cards.py        # 模板卡片投递 + 回调处理 + 台账
新增  src/im/adapters/wecom/resolvers.py    # chatid/userid → 平台映射
新增  src/im/adapters/wecom/markdown.py     # 企微 markdown 子集（链接相邻、表格降级）
新增  src/im/channel/renderers/wecom.py     # Card IR → 企微 renderer
修改  src/im/engine_wiring.py               # 工厂 + registry + renderer/validator 映射（约 40 行）
修改  src/api/static/js/pages/im_channel.js # 解禁 + 凭据表单 + 状态（约 80 行）
修改  src/api/routes/chat.py                # 回显 + 保存写穿（约 40 行）
新增  tests/im/test_wecom_inbound.py
新增  tests/im/test_wecom_outbound.py
新增  tests/im/test_wecom_card.py
新增  tests/im/test_wecom_channel.py
```

## 8. 测试策略（含钉钉事故教训）

1. **fixture 必须是真实报文形态**：字符串 `data`/`body`、无 `eventtype`、带 `chattype`、群聊带 `chatid` —— 钉钉两次事故都是"单测 fixture 全用 dict，抓不到协议实际是字符串"。
2. **解析不静默**：未知 msgtype/缺字段 → 回执或明确日志 + 计数，禁止 `return None` 无声黑洞。
3. **接收循环不猝死**：任何非 `ChannelError` 异常都要被兜住（钉钉 #1008 引入过"connect 任务静默死亡、不重连不计数"）。
4. **出站守卫**：`aibot_send_msg` 传 text 必须被拦（单测直接断言 payload）。
5. **多副本**：两进程抢同一 bot，只有一个 `channels=1`，另一个 0 且无重复回复。
6. **DB/网络隔离**：单测用 sqlite 内存 + 假 WS（不打真企微）。

## 9. 边界与兼容

- 引擎侧零改动 → 现有 314 个 `tests/im` 用例应保持全绿（新增渠道不得改变既有路径）。
- `channel_installation` 无 schema 变更；`channel_type='wecom'` 首次写入由前端保存产生。
- 飞书/钉钉行为不变；`_built_apps` 去重逻辑天然支持第三种渠道（按 app_id/bot_id）。
- 回滚：删除 `registry.register(Type.WECOM, ...)` 一行 + 前端恢复 `disabled`，渠道即静默（不建连、报错不外泄）。

## 10. 开放问题（需主人拍板，决定批 3/5 的范围）

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| Q1 | 卡片走**模板卡片**（真回调、可原地更新，但需先在企微侧有模板）还是先**纯 markdown**（按钮=回数字，零配置） | A 模板卡片 / B 纯 markdown 先行 | **A**，但批 3 先做"无模板自动降级 B"，保证不卡在配置 |
| Q2 | v1 是否做**流式气泡**（打字机） | A 做（+250 行）/ B 不做 | **B**，先保证送达与卡片，流式排批 5 |
| Q3 | 凭据入口只支持**手填 bot_id + secret** 可否 | A 手填 / B 等扫码或免登 | **A**（企微无扫码链路，手填最省） |
| Q4 | 首批范围只做 **user 轨（Chat Agent 页）**，还是同时 **repo 轨（DevOps 群）** | A user 轨先行 / B 双轨同批 | **A**，repo 轨依赖群权限与 @ 规则，第二批再验 |

## 11. 工作量与风险总评

- 代码量约 **1900~2100 行**（含测试），与钉钉适配器（1731 行 + 测试）同量级；因为协议形态同族（长连接 + JSON 帧），复用度高。
- 最大不确定性在**模板卡片**（Q1）与**多副本单连接**（租约是否够用），这两处建议各自留一批做真机验证，不要和文本链路混在一批。
- 与钉钉相比预期更顺的一点：企微卡片有真回调（能拿点击人、能更新卡片），不需要再走 URL 跳转那条退化路线。
---

## 12. 实现落地记录（2026-11-06，PR 随单提交）

本节记录编码阶段与 §3/§4 设计稿的**差异与依据**，全部以官方文档
`developer.work.weixin.qq.com/document/path/101463`（2026-10 版）为准。

### 12.1 回复窗口：24h，不是 5s

§3.2 记「`aibot_respond_msg` 只有约 5s 窗口」——官方文档实际口径是
「收到消息回调后 **24 小时内**可以往该会话回复消息」；5s 约束的是
`aibot_respond_welcome_msg`（欢迎语）与 `aibot_respond_update_msg`（卡片更新）。

因此实现取 `REPLY_WINDOW_SECS = 24h`，否则 2 分钟以上的长任务、以及媒体出站
（主动推送只支持 markdown/template_card，文件必须走回复通道）会在设计稿的 5s
窗口下**必然失败**，直接违背 FR-3 / FR-5。

「窗口外自动切 `aibot_send_msg`」的机制保留（`WecomSender._reply_req_id`），
窗口与时钟均可注入，单测覆盖窗口内（respond）与窗口外（send 主动推送）两条路径。

### 12.2 模板卡片：内联 card_type，无需预建模板

§3.5 担心「模板卡片需先在企微侧有模板（template_id）」——智能机器人的模板卡片是
**随消息内联声明** `card_type + 结构体`（`text_notice` / `button_interaction`），
不需要 template_id，也没有后台配置前置。点击回 `template_card_event`，
`event_key` 即按钮 key（本实现写入 `action_payload` 序列化，如 `{"cmd": "/bind 2"}`），
带操作人 `from.userid` —— CARD_ACTION 能力位成立。

降级开关保留（`GRAPE_WECOM_TEMPLATE_CARD=off` 或渲染失败/结构为空）：
按钮降级为「回复 `/cmd`」文本 + 计 `card_action_unsupported`，内容不丢。

### 12.3 出站渲染载荷形状（egress 空载荷护栏约束）

`engine/egress.py:_card_payload_empty` 按 `msgtype` 判定可见内容，未知 msgtype 走
lark 形状扁平化 → 企微模板卡片若不带 markdown 兜底会被判为空、卡片永远发不出去。
故 renderer 统一产出：

```json
{"msgtype": "markdown", "markdown": {"text": "<压平文本>"}, "template_card": {...}}
```

sender 优先用 `template_card`，无/被关闭时用 `markdown.text`。既有钉钉/飞书路径不受影响。

### 12.4 出站媒体：三步分片上传

§3.4 只写了「先上传换 media_id」。实际协议是 WS 上的三步分片：
`aibot_upload_media_init`（type/filename/total_size/total_chunks/md5）→
`aibot_upload_media_chunk`（base64_data，≤512KB/片，≤100 片）→
`aibot_upload_media_finish` → `media_id`（3 天有效）。实现按此落地，
上传失败/超限一律 `SendResult(ok=False)`，不静默。

出站媒体走 `OutboundMessage.metadata["attachments"] = [{data|path, filename, media_type}]`。

### 12.5 回填修正（不改 schema，但必须改回填）

`scripts/migrations/001_channel_tables.py:backfill_installations` 先前**恒按 feishu**
写 `channel_installation.channel_type`。企微配置（`chat_agent_session.im_provider='wecom'`）
在进程重启后会被回填成 feishu 安装行，引擎就会拿企微 bot_id 当飞书 app_id 建连。
现按 `im_provider` 落 channel_type，未知/空值回落 default（历史语义不变）。

### 12.6 前端与路由

- `im_channel.js`：解除 `wecom.disabled`，provider 识别加 wecom，字段标签按渠道切换
  （飞书 App ID / 钉钉 AppKey / 企微 Bot ID + Secret）。
- `chat.py`：provider 白名单收敛为单一常量 `_IM_PROVIDERS`（回显归一 / 保存校验 /
  解绑清理三处共用），`wecom` 回显走 `_im_echo("wecom")`（真值，不再占位）。

### 12.7 未做（与 §10 开放问题一致）

流式气泡（批 5，`stream.id`）、卡片原地更新（`aibot_respond_update_msg` /
MESSAGE_EDIT）、欢迎语、用户反馈事件、表情回执、加密 userid 换明文。
能力位不申报这些（不虚报）。
