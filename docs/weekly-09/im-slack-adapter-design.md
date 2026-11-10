# 设计：Slack 适配器 —— 接入 / 发送 / 卡片三段一并落地

> 2026-11-06｜基线 `feat/im-dingtalk-interactive-card` @ `cbe620bef`（= `origin/weekly-09-IM`）
> 目标集成分支：`weekly-09-IM`
> 协议事实来源：上游 multica `server/internal/integrations/slack/`（channel/inbound/outbound/mrkdwn/slash_command/install）
> + Slack 官方文档（Socket Mode、Events API、Block Kit、Web API 限流）

## 1. 一段话结论

Slack 走 **Socket Mode（长连接）+ Web API**：入站事件与按钮回调都从同一条 WSS 回来，出站用 `chat.postMessage`。
它比钉钉/企微**更适合我们现有的三段模型**——按钮是 Block Kit 的 `block_actions` **真回调**（带操作人、可 `chat.update` 原地更新、
天然支持线程），因此不需要钉钉那种 URL 跳转退化，也不需要企微那种"事件不能当回复令牌"的绕行。
引擎侧（Supervisor / 租约 / 去重 / 指标 / handler）**零改动**，只补三段适配器 + 装配 + 前端凭据入口。

### 与已接入渠道的对比（决定设计取舍）

| 能力 | 飞书 | 钉钉 | 企业微信 | **Slack（本设计）** |
|---|---|---|---|---|
| 入站传输 | WS 长连接 | Stream 长连接 | aibot WS 长连接 | **Socket Mode WSS**（xapp） |
| 出站 | OpenAPI | sessionWebhook / 机器人 API | aibot_send_msg / respond_msg | **Web API chat.postMessage**（xoxb） |
| 卡片按钮回调 | 有（`card.action.trigger`） | **无**（只能 URL） | 有（`template_card_event`，但**不能**用事件 req_id 回复） | **有**（`block_actions`，带 user id，可直接 chat.update） |
| 线程回复 | 有 | 无 | 无 | **有（thread_ts）** |
| 原地更新卡片 | 有 | 无 | 有 | **有（chat.update）** |
| 表情回执 | 有 | 无 | 无 | **有（reactions.add）** |
| 公网要求 | 无 | 无 | 无 | **无**（Socket Mode 主动连出） |

## 2. 现状代码事实（三段落点，行号基于 `cbe620bef`）

| 断面 | 契约 | 现有实现 | Slack 要做的 |
|---|---|---|---|
| **接入** | `src/im/channel/channel.py:66` `Channel`（`type/connect/disconnect/send`）。**入站不在接口上**：适配器在 `connect()` 里跑接收循环、主动调注入的 `inbound_handler`（依赖倒置，见文件头注释） | `feishu/channel.py`(303)＋`feishu/inbound.py`(108)；`dingtalk/channel.py`(346)＋`inbound.py`(270)；`wecom/channel.py`(449)＋`inbound.py` | `slack/channel.py` + `slack/inbound.py` |
| **发送** | `engine/replier.py:21` `ChannelSender` Protocol（`send`/`capabilities`）、`:26` `Replier`；`engine/egress.py:78` `HandlerEgress` 按能力位降级 | `feishu/outbound.py`(82)、`dingtalk/outbound.py`(551)、`wecom/outbound.py`(586) | `slack/outbound.py`（Sender） |
| **卡片** | `channel/card.py` Card IR（Card/Block/Action/Tone + `action_payload`）；`channel/renderers/<渠道>.py` 渲平台形态；**业务层禁止平台字段**（`tests/im/test_card_ir.py` 静态检查） | `renderers/feishu.py`(144)、`dingtalk.py`(74)、`wecom.py` | `renderers/slack.py` + `slack/cards.py` |
| 能力位 | `channel/capability.py`（声明不协商，核心不做降级决策） | 飞书 7 位；钉钉 `TEXT|RICH_CARD`；企微 `TEXT|RICH_CARD|CARD_ACTION|ATTACHMENT` | 见 §4.1 |
| 解析 | 各适配器 `resolvers.py`（chat/sender → 平台用户映射） | feishu(32)、dingtalk(39)、wecom | `slack/resolvers.py` |
| 装配 | `engine_wiring.py:1133/1135/1137` `registry.register(Type.*)`；`:987` `_card_renderer_for`；`:955` `_msg_id_validator_for`；三渠道共用 `_channel_factory`（`:1374`） | 已注册 FEISHU/DINGTALK/WECOM | 新增 `_slack_factory` + 注册 + 两处映射 |
| 枚举/前端 | `channel/types.py:19` `Type.SLACK` **已存在**；前端 `im_channel.js` 平台表已有 Slack 卡但 `disabled` | — | 解禁 + 凭据表单 |

适配器规模基准：钉钉 1731 行、企微约 1400 行 → **Slack 预估 1100~1300 行**（多出 mrkdwn 转换与分块，少掉媒体解密与长连接握手细节）。

## 3. Slack 平台事实（决定实现形状的硬约束）

### 3.1 凭据与连接（双令牌）

| 令牌 | 前缀 | 用途 | 说明 |
|---|---|---|---|
| Bot token | `xoxb-` | Web API 出站（`chat.postMessage`/`chat.update`/`files.*`） | 安装到工作区时获得 |
| App-level token | `xapp-` | **Socket Mode 连接**（`apps.connections.open`） | 需开 `connections:write` scope |
| Signing secret | — | 仅 HTTP Events/Interactivity 模式需要 | **Socket Mode 用不到，不必收** |

- Socket Mode：先 `POST https://slack.com/api/apps.connections.open`（Bearer xapp）拿 WSS URL，
  连上后收到 `hello` → 之后每个事件是一个 envelope（`envelope_id` + `payload`）。
- **必须 3 秒内 ack**（回 `{"envelope_id": ..., "payload": {}}`），否则 Slack 重投（最多 3 次，同一 `event_id`）。
- 建议 30s 心跳 ping；断线自动重连（交 Supervisor 已有退避逻辑）。
- 一个安装一条 Socket Mode 连接 → 与我们"每条 installation 一条长连接 + 租约"的模型天然对齐。

### 3.2 事件（入站）

| 事件 | 触发 | 备注 |
|---|---|---|
| `app_mention` | 群里 @ 机器人 | 群聊主入口 |
| `message`（`message.channels`/`message.im`/`message.groups`） | 频道消息 / 私聊 | 需过滤 `subtype`（`bot_message`/`message_changed`/`message_deleted` 不派发）；`file_share` 子类型是媒体 |
| `block_actions`（interactive） | Block Kit 按钮点击 | **真回调**，payload 里有 `user.id` / `trigger_id` / `response_url` / `actions[].value` |
| `file_share` | 用户上传文件 | 走 `files.info`/`url_private_download`（**需带 xoxb 才能下载**） |

- 去重键：`event_id`（重投同 id）；用户消息另有 `client_msg_id`。**别用 `ts` 当唯一键**（编辑会产生同 ts 新事件）。
- 群聊：需判断是否 @ 机器人（`app_mention` 已保证；`message.channels` 需自行判断 mentions）。

### 3.3 出站（Web API）

| 用途 | 接口 | 备注 |
|---|---|---|
| 发消息 | `chat.postMessage`（`channel`+`text`/`blocks`+可选 `thread_ts`） | 单条 text 硬上限约 40k 字符，上游按 **38000 字符分块**（含余量）；我们沿用 |
| 线程回复 | 同上 `thread_ts=父消息 ts` | Slack 的"回复"= 同线程；顶层消息 `thread_ts` 为空 |
| 原地更新 | `chat.update`（`channel`+`ts`+新内容） | 用来把卡片从"处理中"刷成"已绑定" |
| 加表情 | `reactions.add` | 回执语义 |
| 上传文件 | `files.getUploadURLExternal` → PUT → `files.completeUploadExternal` | **旧 `files.upload` 已弃用**，不要再用 |
| 斜杠命令 | 命令回调带 `trigger_id`/`response_url`，**3 秒内**必须响应 | 超时用户看到 "operation timed out" |

- **速率限制**：`chat.postMessage` 属 Tier 3（大约 1 msg/s 每通道、短时可突发）；被限流回 429 + `Retry-After`。
  → 发送器要实现"每次发送前节流 + 429 退避重试"，否则长回答会成片失败。

### 3.4 卡片（Block Kit）

- 结构：`blocks[]`（`header` / `section` / `actions` / `context` / `divider` / `input`），
  按钮在 `actions.elements[]`：`{"type":"button","text":{...},"action_id":"bind","value":"{\"cmd\":\"/bind 2\"}"}`。
- 限制：单条最多 **50 blocks**；`section.text` 上限 3000 字符；`actions` 元素最多 25；
  总 payload 建议 < 50KB。
- 回调：点击 → `block_actions` → 里层 `actions[0].action_id` + `value` + `user.id`。
  **这正好对上我们的 `action_payload()`**（`{"cmd": "/bind 2"}`），回收后与"回数字"走同一份 `_handle_selection`。
- 更新：按钮回执可用 `response_url` 或 `chat.update`（后者更稳，能改整卡）。

### 3.5 文本方言（mrkdwn）

Slack 用 **mrkdwn** 而不是标准 Markdown：`*粗体*`（不是 `**`）、`_斜体_`、`~删除线~`、`<url|文字>`、
不渲染表格与标题（`#` 只是普通字符）。上游 `formatMrkdwn` 做的就是这层转换。
→ 我们必须在 **renderer/markdown 层**做转换，业务层继续写标准 Markdown（这是既有分层契约）。

## 4. 适配器设计

### 4.1 能力位申报（`slack/outbound.py`）

```
SLACK_CAPABILITIES = Capability.TEXT | Capability.RICH_CARD | Capability.CARD_ACTION
                   | Capability.THREAD_REPLY | Capability.MESSAGE_EDIT
                   | Capability.ATTACHMENT | Capability.REACTION
```

不申报：`QUOTE_REPLY`（Slack 用线程表达"针对某条"，不是引用块）、`TYPING_INDICATOR`、
`VOICE`（v1 不做语音转写，收到语音按文件处理）。
**这是所有渠道里能力位最全的一个**，也意味着 `HandlerEgress` 的降级分支在这条线上几乎不会触发。

### 4.2 接入：`slack/inbound.py` + `slack/channel.py`

`SlackChannel.connect()` 负责：拿 WSS URL → 连 → `hello` → 读 envelope → **3s 内 ack** → 解析 → `InboundMessage`。

字段映射：

| Slack 字段 | 我们的字段 | 备注 |
|---|---|---|
| `event.type` | 分派 | `app_mention`/`message`/`file_share` → 消息；`block_actions` → `MsgType.CARD_ACTION` |
| `channel` | `Source.ChatID` | 频道 id / 私聊 channel（D 开头） |
| `user` | `Source.SenderID` | 用于身份映射与卡片回调归属 |
| `text` | `text` | 群消息要剥掉 `<@U123>` 提及前缀 |
| `ts` | `MessageID` | **注意**：去重键用 `event_id`，`ts` 只作为"线程父节点/回复目标" |
| `thread_ts` | `thread_root` | 决定出站是否落线程 |
| `files[]` | 媒体 | 走 `url_private_download`（带 xoxb）下载到本地缓存 |
| `event_id` | `EventID`/去重键 | 幂等键（Slack 重投相同 id） |
| `block_actions.actions[].value` | 卡片动作 | JSON 串，反序列化成 `{"cmd": ...}`，与飞书/钉钉同源 |

**防坑清单（前两个渠道踩过的，直接写进实现）**：
1. envelope 的 `payload` **可能是字符串**（与钉钉 `data`、企微 `body` 同类坑）→ 先 `json.loads` 再判类型；
2. 任何解析异常不得穿透接收循环（Supervisor 只认 `ChannelError` 才会重连，其它异常必须内部兜住并计数）；
3. 幂等键用 `event_id`，不要用 `ts`（编辑消息会产生同 ts 新事件）；
4. 必须 3s 内 ack，否则重投 → 同一个按钮点击可能执行两次（我们已幂等，但仍应 ack-first）；
5. `message_changed` / `message_deleted` / `bot_message` 必须显式忽略，否则机器人会跟自己对话。

### 4.3 发送：`slack/outbound.py`（`SlackSender`）

```
async def send(out: OutboundMessage) -> SendResult
```

| 情形 | 实现 |
|---|---|
| 纯文本 | `formatMrkdwn(text)` → 按 38000 字符分块 → `chat.postMessage`（带 `thread_ts`） |
| 卡片 | `blocks=[...]` 走 `chat.postMessage`；按钮 `action_id` + `value=action_payload()` |
| 原地更新 | `chat.update(channel, ts, blocks=...)`（能力位 `MESSAGE_EDIT` 已申报，可真更新） |
| 媒体 | 三步上传（`getUploadURLExternal` → PUT → `completeUploadExternal`）后发文件消息 |
| 表情回执 | `reactions.add(channel, ts, name)` |
| 空内容 | 硬护栏拒绝（同 `card_empty_guard` 口径） |
| 429 限流 | 读 `Retry-After` 退避重试（最多 3 次），仍失败 → `failed` 计数 + 日志 |

线程策略：`reply_to`/`thread_root` 存在 → 落线程；否则顶层。**这一条让 Slack 的体验优于钉钉/企微**（回复天然聚合）。

### 4.4 卡片：`renderers/slack.py` + `slack/cards.py`

- 业务层零改动：`Card IR → SlackCardRenderer.render(card) -> RenderedCard(payload={"blocks": [...]}, degraded=[...])`
- 映射：`title` → `header` block；`text` → `section`（3000 字符切分）；`fields` → `section` 的 `fields`；`divider` → `divider`；
  `select/row_block` 的 `Action` → `actions.elements[].button`（`action_id=action.id`，`value=action_payload 序列化`）
- 按钮语义：受 25 元素上限约束，超出降级为"回复 /cmd"文本提示（计 `card_action_unsupported`，不静默丢）
- 回调：`block_actions` → `inbound.py` → `InboundMessage(CARD_ACTION, text="/bind 2")` → **同一份 `_handle_selection`**
- 原地更新：结果卡用 `chat.update` 把原卡刷成「✅ 已绑定」（能力位 `MESSAGE_EDIT` 支持，真更新而非追加）

### 4.5 解析：`slack/resolvers.py`

- `chat_id`：频道/私聊 id 直接用（不需要反查）
- 用户身份：`user.id`（U123…）→ `channel_sender_identity` / `unified_user_mapping`（复用既有路径），
  `users.info` 拿显示名（注意限流，进程内缓存）

### 4.6 装配：`engine_wiring.py`

```
registry.register(Type.SLACK, self._slack_factory)      # 与 :1133/1135/1137 同处
_card_renderer_for("slack") -> SlackCardRenderer()      # :987 加分支
_msg_id_validator_for("slack") -> _always_valid         # :955（channel+ts 路由，无 om_ 形态假设）
```

`_slack_factory` 复用 `_channel_factory`（`:1374`）：同一套 Router 装配、去重（`_built_apps` 按 Supervisor 活渠道收敛）、
`_repliers` 登记；只换 `SlackChannel` + `SlackSender`（xapp 给接入、xoxb 给出站）。

### 4.7 前端（`im_channel.js` + `chat.py`）

- 平台表已有 Slack 卡（`im_channel.js:28` 附近），去掉 `disabled` 并加图标（Slack 官方 SVG）
- 凭据表单：**两个输入框** —— `Bot Token (xoxb-…)`、`App Token (xapp-…)`；
  后端保存时脱敏回显（`********`），落 `channel_installation.config`
- 后端：`chat.py:42 _IM_PROVIDERS` 加 `"slack"`；`_im_echo("slack")` 回显；写穿桥按 provider 落 `channel_type='slack'`
- 连接状态徽标：`/api/im/installations` 的 `connected`（Supervisor 活渠道），不臆造

## 5. 三条链路数据流（逐跳）

**入站消息**
`Socket Mode envelope(message/app_mention)` → `SlackChannel.connect()` 读循环（**先 ack**）→ `inbound.py`
（字符串 payload 容错 / 剥 @ / 下载文件 / 忽略 bot 与编辑事件）→ `InboundMessage` → `inbound_handler` →
`Router`（去重 `event_id` / 身份 / `channel_chat_session_binding`）→ `handler_async` → `HandlerEgress` → `Replier` → `SlackSender.send` → `chat.postMessage(thread_ts?)`

**卡片点击**
`envelope(block_actions)` → `inbound.py`（`block_actions.actions[].value` → `/bind 2`）→ `InboundMessage(CARD_ACTION)` →
Router → `handler_async._handle_selection` → 写库 → 结果卡 → `SlackCardRenderer` → `chat.update`（原地变绿）

**主动推送（长任务）**
worker 完成 → `HandlerEgress` → `SlackSender.send`（无 reply 上下文）→ `chat.postMessage`（顶层或原线程）→ 429 退避

## 6. 分批落地（每批可独立验收）

| 批 | 内容 | 验收 | 预估 |
|---|---|---|---|
| 1 | 骨架 + 纯文本：`channel.py`（Socket Mode 连接/ack/重连）+ `inbound.py`（app_mention/message）+ `outbound.py`（postMessage + 38000 分块 + mrkdwn 转换）+ 装配注册 + 能力位 | 私聊/频道 @ 机器人有回复；`channels` +1 | 约 520 行 |
| 2 | 线程 + 编辑 + 表情 + 媒体：`thread_ts` 落线程、`chat.update`、`reactions.add`、三步文件上传/下载 | 回复落线程；卡片能原地更新；发图能读、产物能回传 | 约 330 行 |
| 3 | 卡片：`renderers/slack.py` + `cards.py`（Block Kit + `block_actions` 回调 + 25 元素上限降级） | `/repo` 卡片按钮可点，点完换仓并原地更新 | 约 380 行 |
| 4 | 前端：解禁 Slack 卡 + 双令牌表单 + 状态回显 + 解绑写穿 | UI 四项真实（配置/启停/解绑/状态） | 约 130 行 |
| 5 | 斜杠命令（可选）：`/grape` 之类 + `trigger_id` 3s 响应 + `response_url` | 命令有回执不掉线 | 约 180 行 |

批 1 完成即可"能用"（且含线程基础），批 3 完成才对齐飞书体验。

## 7. 文件清单（新增 / 修改）

```
新增  src/im/adapters/slack/__init__.py
新增  src/im/adapters/slack/channel.py       # Channel + Socket Mode 读循环（ack/心跳/重连）
新增  src/im/adapters/slack/inbound.py       # envelope → InboundMessage（事件/文件/去重键）
新增  src/im/adapters/slack/outbound.py      # SlackSender（postMessage/update/files/reactions/429）
新增  src/im/adapters/slack/cards.py         # Block Kit 组装 + block_actions 回调 → 命令
新增  src/im/adapters/slack/markdown.py      # 标准 Markdown → mrkdwn + 38000 分块
新增  src/im/adapters/slack/resolvers.py     # channel/user → 平台映射（users.info 缓存）
新增  src/im/channel/renderers/slack.py      # Card IR → Block Kit
修改  src/im/engine_wiring.py                # _slack_factory + registry + renderer/validator（约 45 行）
修改  src/api/static/js/pages/im_channel.js  # 解禁 + 双令牌表单 + 状态（约 90 行）
修改  src/api/routes/chat.py                 # provider 白名单 + 回显 + 写穿（约 45 行）
新增  tests/im/test_slack_{channel,inbound,outbound,card}.py
```

## 8. 测试策略（沿用前几个渠道的教训）

1. **fixture 用真实 envelope 形态**：`payload` 为字符串、`event_id` 重复（模拟 Slack 重投）、
   `subtype=bot_message`/`message_changed`、群消息带 `<@Uxxx>` 前缀 —— 钉钉/企微两次事故都是"单测 fixture 全用 dict 抓不到真实形态"；
2. **ack 时序**：断言"先 ack 再处理"（用假 WS 记录帧顺序），确保不会因超 3s 触发重投；
3. **分块边界**：38000 字符处不能切在 UTF-8 多字节中间（用中文长文断言）；
4. **mrkdwn 转换**：`**粗**`→`*粗*`、`[文字](url)`→`<url|文字>`、代码块保持原样；
5. **按钮上限**：>25 元素时降级为文本提示并计降级原因，绝不发出被 Slack 拒收的 payload；
6. **限流**：429 + `Retry-After: 2` 时按退避重试且最终成功；连续 429 超上限计 `failed`；
7. DB/网络隔离：单测用 sqlite 内存 + 假 WS/假 HTTP，不打真 Slack。

## 9. 边界与风险

| 风险 | 影响 | 处置 |
|---|---|---|
| **3 秒 ack 窗口** | 超时 → Slack 重投 → 重复执行 | 先 ack 再异步处理；去重用 `event_id` |
| **chat.postMessage 限流（Tier 3）** | 长回答成片 429 | 发送节流 + `Retry-After` 退避，超限计 failed |
| **mrkdwn ≠ Markdown** | `**粗体**` 原样显示 | 所有出站文本经 `formatMrkdwn`；业务层仍写标准 Markdown |
| **块/元素上限**（50 blocks、3000 字符/段、25 元素） | payload 被拒 | 渲染层切分/降级，单测覆盖边界 |
| **文件上传 API 已换代** | 用旧 `files.upload` 直接 410/拒绝 | 只实现三步新流程 |
| **用户消息事件 vs app_mention** | 同一句话可能来两次（频道里 @） | 按 `event_id` 去重；`app_mention` 优先，`message` 里跳过含 @bot 的 |
| **编辑/删除事件** | 同 ts 新事件可能被当新消息 | 显式忽略 `message_changed`/`message_deleted` |
| **多工作区/多 bot** | 一条连接对应一个 app token | 每安装一条 Socket Mode（与租约模型一致，无需新机制） |

## 10. 开放问题（需主人拍板）

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| Q1 | 首批范围：只 **user 轨（Chat Agent 页）** 还是同时 **repo 轨（DevOps 群）** | A user 轨先行 / B 双轨同批 | **A**（群权限与 @ 规则第二批再验） |
| Q2 | 斜杠命令（批 5）做不做 | A 做 / B 不做 | **B**，先把三段主线跑通 |
| Q3 | 卡片交互形态：Block Kit 按钮（真回调、可原地更新）确认沿用？ | A 是 / B 另有想法 | **A**（Slack 是唯一不需要退化的渠道） |
| Q4 | 凭据入口只支持**手填 xoxb + xapp** 可否（不做 OAuth 安装流程） | A 手填 / B 做 OAuth 安装 | **A**；OAuth 留作后续需求 |

## 11. 工作量与风险总评

- 代码量约 **1900~2100 行**（含测试），与企微（~1400 + 测试）同量级；因协议更规整（Web API + 事件 JSON），实现风险更低。
- **预期最顺的一个渠道**：三段能力全部原生（回调、线程、原地更新、表情、媒体），不需要钉钉那种"URL 跳转"退化，
  也不需要企微那种"事件不能当回复令牌"的绕行。
- 主要不确定性只有两处：**限流策略**（Tier 3）与**3 秒 ack**；两者都有明确解法，建议各留一条单测做守门。