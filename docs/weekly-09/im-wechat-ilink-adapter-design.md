# 设计：微信（Weixin / ilink Bot 通道）适配器 —— 接入 / 发送 / 卡片降级三段

> 2026-11-09｜基线 `weekly-09-IM` @ `e3214174e`
> 目标集成分支：`weekly-09-IM`
> 协议事实来源：腾讯官方 `@tencent-weixin/openclaw-weixin`（v2.4.10，LICENSE MIT，
> 仓库 `github.com/Tencent/openclaw-weixin`）内 `docs/protocol_zh_CN.md` + 插件源码 `src/channel.ts`（本地副本 `/tmp/pr.md`、`/tmp/pkg2/package/`）
> **注意**：该协议文档是插件作者按客户端代码反推的，不是服务端正式契约；腾讯可随时调整。

## 1. 一段话结论

微信官方从 2026-03 起提供了一条 **Bot 通道（ilink）**：微信扫码把某个微信号绑定到一个 Bot 账号，
微信里出现一个带 AI 标识的联系人（用户看到的名字类似「微信clawbot」）。通道就是
**HTTPS + JSON + 长轮询**（`POST /ilink/bot/getupdates` 35s 长轮询，游标 `get_updates_buf`），
与我们 Telegram 适配器的形状几乎一致，因此**引擎侧（Supervisor / 租约 / 去重 / 指标 / handler）零改动**，
只需补三段适配器 + 扫码绑定接口 + 装配 + 前端入口。

**硬约束先说清**：该通道 **没有卡片、没有按钮、没有群**（能力声明只有 `chatTypes:["direct"]`、`media`、`blockStreaming`），
卡片一律降级为「文本 + 编号选项」；也没有消息编辑接口，所以流式只能是分段连发，不是同一气泡刷新。

## 2. 目标与非目标

**目标（v1）**
- FR-1 扫码绑定：网页端出二维码，微信扫码确认后凭证落库，30s 内引擎接管
- FR-2 入站：文本 / 图片 / 文件 / 语音（用微信侧转写文本）/ 视频；引用消息（`ref_msg`）还原上下文
- FR-3 出站：文本（4000 分块）、图片、文件；`context_token` 正确回传；`client_id` 幂等
- FR-4 卡片降级：Card IR → 文本 + 编号 1..N；用户回数字走既有 `_handle_selection`
- FR-5 体验：typing 指示（`getconfig` → `sendtyping`）、`notifyStart/notifyStop` 生命周期
- FR-6 指标：沿用 `/health/realtime` 的 `total_messages_*`、`reconnects`、`degraded_by_reason`

**非目标（v1 明确不做）**
- 个人微信 hook（Wechaty/padlocal 路线）—— 违反协议、封号风险，不碰
- 群聊（`group_id` 只在类型定义里出现，插件只声明 direct；协议未承诺）
- 卡片/按钮（协议无此类型）
- 流式气泡刷新（协议无 edit 接口）
- 「他人绑定」（需 `bot_spam_ticket` + 微信支付实名，v1 只做本人扫码绑定）

## 3. 协议事实（决定实现形状的硬约束）

### 3.1 端点与鉴权

| 用途 | 方法/路径（base = `https://ilinkai.weixin.qq.com`） | 备注 |
|---|---|---|
| 取绑定二维码 | `POST /ilink/bot/get_bot_qrcode?bot_type=3` | body `{"local_token_list": []}`；返回 `qrcode`、`qrcode_img_content` |
| 轮询绑定状态 | `GET /ilink/bot/get_qrcode_status?qrcode=…` | **长轮询 35s**；可能返回 `need_verifycode`（需在手机上看 PIN 再提交） |
| 收消息 | `POST /ilink/bot/getupdates` | body `{"get_updates_buf": "<游标>", "base_info": {…}}` |
| 发消息 | `POST /ilink/bot/sendmessage` | body `msg{to_user_id, client_id, message_type:2, message_state:2, context_token, item_list}` |
| 取上传参数 | `POST /ilink/bot/getuploadurl` | `filekey/media_type/rawsize/rawfilemd5/filesize/no_need_thumb=true/aeskey` |
| 取配置 | `POST /ilink/bot/getconfig` | 返回 `typing_ticket`（base64） |
| 输入状态 | `POST /ilink/bot/sendtyping` | `status` 1 正在输入 / 2 取消 |
| 生命周期 | `POST /ilink/bot/msg/notifystart` / `notifystop` | 非零 `ret` 只警告，不阻断 |

请求头：`Content-Type: application/json`、`AuthorizationType: ilink_bot_token`、`Authorization: Bearer <bot_token>`、
`X-WECHAT-UIN`（随机 uint32 十进制再 base64）、`iLink-App-Id: bot`、`iLink-App-ClientVersion`（版本号编码）。
所有鉴权 POST 带 `base_info: {channel_version, bot_agent}`。

### 3.2 登录产出（`confirmed` 时）

`bot_token`（后续 Bearer）、`ilink_bot_id`（Bot 账号 ID）、`baseurl`（后续 API 基址，可能被服务端重定向）、`ilink_user_id`（扫码用户）。
失败/过期规则：`expired`、`verify_code_blocked` 需重新取码，刷新计数 > 3 停止；`scaned_but_redirect` 后轮询改到 `redirect_host`。

### 3.3 消息模型

`WeixinMessage{seq, message_id, from_user_id, to_user_id, client_id, create_time_ms, session_id, group_id,
message_type(1 用户/2 Bot), message_state(0 新/1 生成中/2 完成), item_list[], context_token, run_id}`

`MessageItem.type`：`1 text_item`｜`2 image_item`｜`3 voice_item`｜`4 file_item`｜`5 video_item`｜`11 tool_call_start`｜`12 tool_call_result`
公共字段 `msg_id`、`ref_msg`（引用）；`voice_item.text` 即微信侧语音转写。

### 3.4 媒体

- 入站：CDN 下载 → AES-128-ECB + PKCS#7 解密；图片优先 `image_item.aeskey`（32 位 hex）→ 其次 `media.aes_key`（base64）；
  语音/文件/视频缺 `media.aes_key` 时客户端跳过（我们也跳过并计 degrade）
- 出站：算 MD5/大小 → `getuploadurl` → 密文 `POST` 到 `upload_full_url`（`application/octet-stream`）→
  响应头 `x-encrypted-param` 必须非空（否则重试 ≤3 次）→ 把 `{encrypt_query_param, aes_key, encrypt_type:1}` 放进 media 引用发送

### 3.5 风控与错误

- `getUpdates` 返回 `ret/errcode = -14` → **账号会话暂停 1 小时**（我们照做：暂停轮询 3600s，计一次 degraded，不狂重试）
- 其余失败：指数退避重连（沿用 Supervisor 现有策略）
- 绑定码刷新上限 3 次；PIN 错误次数过多 → `verify_code_blocked`

## 4. 与现有引擎的映射（三段落点，行号基于 `e3214174e`）

| 断面 | 契约 | 现有实现 | 微信要做的 |
|---|---|---|---|
| 接入 | `src/im/channel/channel.py:62` `Channel`（`type/connect/disconnect/send`，入站由 `connect()` 内循环回调 `inbound_handler`） | feishu/dingtalk/wecom/slack/telegram 各自 `channel.py`+`inbound.py` | `wechat/channel.py`（getupdates 长轮询 + 游标持久化）+ `wechat/inbound.py` |
| 发送 | `src/im/engine/replier.py:21` `ChannelSender` / `:26` `Replier`；`engine/egress.py:78` `HandlerEgress` 按能力位降级 | 五套 `outbound.py` | `wechat/outbound.py`（sendmessage + client_id 幂等 + 4000 分块） |
| 卡片 | `channel/card.py` Card IR；`channel/renderers/<渠道>.py`；业务层禁用平台字段（`tests/im/test_card_ir.py` 有静态检查） | renderers/{feishu,dingtalk,wecom,slack,telegram}.py | `renderers/wechat.py`（纯文本 + 编号选项） |
| 能力位 | `channel/capability.py`（只声明不协商） | 飞书 7 位、钉钉 TEXT\|RICH_CARD、企微 4 位、Slack 9 位、Telegram 9 位 | `TEXT \| RICH_CARD(降级为文本) \| ATTACHMENT`；**不报 CARD_ACTION / MESSAGE_EDIT / REACTION / THREAD** |
| 解析 | 各适配器 `resolvers.py` | 五套 | `wechat/resolvers.py`（`from_user_id` ↔ 平台 user 映射，缓存 getconfig） |
| 枚举 | `channel/types.py:13` `Type` | 已有 FEISHU/DINGTALK/SLACK/WECOM/TELEGRAM | 新增 `WECHAT = "wechat"` |
| 装配 | `engine_wiring.py:1228..1236` `registry.register(...)`；`_card_renderer_for` / `_msg_id_validator_for` / `_channel_factory` | 五渠道已注册 | 新增 `_wechat_factory` + 注册 + 两处映射 |
| 白名单/前端 | `src/api/routes/chat.py:43` `_IM_PROVIDERS = ("feishu","dingtalk","wecom","slack","telegram")`；`static/js/pages/im_channel.js` 平台表 | telegram 刚加完 | 白名单加 `wechat`；前端平台卡 + 扫码绑定弹窗 |

**规模基准**：钉钉 1731 行、企微 ≈1400、Slack ≈1900、Telegram 3016（含文档）→ **微信预估 900–1000 行主体**，
比 Telegram 少（无握手、无按钮、无 edit），多（媒体 AES 加解密 + 扫码绑定）。

## 5. 详细设计

### 5.1 扫码绑定（v1 只做本人绑定）

新增两条接口（`src/api/routes/im_channels.py` 或 chat.py 内）：

```
POST /api/im/wechat/login/start      → {login_id, qrcode_img_content, expires_in}
GET  /api/im/wechat/login/status?id= → {status: wait|scaned|confirmed|expired, need_verifycode?, ...}
```

- `start`：调 `get_bot_qrcode`，把 `qrcode` 与 `bot_token 空` 状态存进程内 `_wechat_logins[login_id]`（TTL 10 分钟，单进程足够；
  重启丢二维码属可接受，前端重新点一次）
- `status`：服务端对 `get_qrcode_status` 做**短轮询代理**（每次请求一次上游长轮询太贵，改为后端每 `login_id` 单飞 + 2s 缓存）；
  `need_verifycode` 时前端弹输入框，用户填 PIN 后随下一次 status 透传 `verify_code`
- `confirmed`：写 `channel_installation`：
  `channel_type='wechat'`, `scope='user'`, `owner_user_id=<当前登录用户>`, `app_id=<ilink_bot_id>`,
  `config={bot_token, baseurl, ilink_user_id, bound_at}`，`status='active'`；随后 Supervisor 30s 扫描自动接管（无需重启）
- 凭证展示一律脱敏：`app_id` 列存 `ilink_bot_id`（非密），`bot_token` 只进 config 且日志走 `_safe_im_app_ref` 同款摘要

### 5.2 Channel：长轮询循环

- `connect()`：先 `notifystart`；循环 `getupdates`：`timeout = 响应里 longpolling_timeout_ms / 1000`（默认 35s），
  `get_updates_buf` 游标持久化在 `channel_installation.config["cursor"]`（每 N 条或每 30s 落库一次，避免频繁写）
- 有空 `msgs` 且 `get_updates_buf` 变化 → 只更新游标；`ret=-14` → 暂停 3600s（记 degrade，退出循环前 `notifystop`）
- 断线/超时：沿用 Supervisor 退避重连（不计 `ws_transport`，新增 cause `ilink_longpoll`）
- `disconnect()`：`notifystop` + 停循环

### 5.3 Inbound

`WeixinMessage → InboundMessage`：
- 去重键：`message_id`（数）→ `f"wechat:{ilink_bot_id}:{message_id}"`，落既有 `channel_inbound_dedup`
- `message_type=2`（Bot 自己）→ 直接丢弃，避免自回环
- `chat_id = from_user_id`（形如 `xxx@im.wechat`），`sender` 同值；用户映射进 `channel_sender_identity`
- `item_list` 拼接：文本直接拼接；`image/file/video` → 下载解密后交给既有附件存储；`voice` 用 `voice_item.text` 当正文（无转写则回「语音暂不支持」）
- `ref_msg` → 作为引用上下文（`quote`），保持 `context_token` 挂在入站消息上供出站回传
- 空内容（无可见项）→ 丢弃并计 `degraded_by_reason.inbound_empty`

### 5.4 Outbound

- 文本：按 4000 字符分块（与插件 `textChunkLimit: 4000` 一致），每块独立 `client_id`（`uuid4`），全部带 `context_token`
- `msg{from_user_id:"", to_user_id, client_id, message_type:2, message_state:2, context_token, item_list:[{type:1,text_item:{text}}]}`
- 失败：`ret != 0` 抛 `WechatApiError`；HTTP 5xx/超时重试 ≤3 次（指数退避）
- 媒体：§3.4 流程；失败降级为「文本说明 + 链接」（若原始 URL 可用），计 `degraded_by_reason.media_upload_failed`

### 5.5 卡片降级（renderers/wechat.py）

Card IR → 文本：

```
【标题】
正文行…
1. 选项一
2. 选项二
回复数字选择
```

- 同时把 `action_payload` 按序号写进 `channel_outbound_card` 台账（Telegram 短码那套复用），
  用户回 `1` 时由既有 `_handle_selection` 还原 —— 不新增选择逻辑，避免两条路漂移
- 无按钮/无回调 → `Capability` 不报 `CARD_ACTION`；`egress` 会按能力位自动降级，不额外写分支

### 5.6 typing / 生命周期

- 收到消息后：`getconfig(ilink_user_id, context_token)` → 缓存 `typing_ticket`（按用户，TTL 1h）→ `sendtyping{status:1}`；
  回复发出后 `sendtyping{status:2}`
- `notifystart/stop` 包在 connect/disconnect 里，失败只 WARNING

### 5.7 指标

复用既有键：`total_messages_received/sent/failed/degraded`、`reconnects`、`flap_by_cause`（新增 `ilink_longpoll`）、
`degraded_by_reason` 新增：`media_download_failed`、`media_upload_failed`、`inbound_empty`、`session_paused_risk`（-14）

## 6. 分批与工作量（量化预估）

| 批次 | 内容 | 预估行数 | 可独立验收 |
|---|---|---:|---|
| M1 | `login.py`（取码/轮询/凭证）+ `channel.py`（长轮询+游标）+ `inbound.py`（纯文本）+ `outbound.py`（文本+client_id+分块）+ `Type.WECHAT` + wiring 注册 | ≈ 480 | 单聊文本收发（真机） |
| M2 | 媒体：CDN 下载解密（图/文件/语音/视频）+ 上传加密（图/文件）+ 降级 | ≈ 300 | 收图/发图/收文件 |
| M3 | 卡片降级 renderer + `channel_outbound_card` 台账接线 + 引用消息 `ref_msg` | ≈ 120 | `/repo` 卡片出文本+编号，回数字生效 |
| M4 | 前端：平台卡 + 扫码绑定弹窗 + 状态回显（`chat.py` 白名单 + `im_channel.js` + 图标）+ typing | ≈ 200 | UI 全链路 |
| 测试 | `test_wechat_{login,channel,inbound,outbound,media,card}.py`（mock ilink HTTP + 真实帧形态 + AES 往返） | ≈ 520 | `pytest tests/im` 全绿 |
| **合计** | | **≈ 1620** | 4 批 |

## 7. 测试策略（照 Telegram 的教训）

- **fixture 用真实帧形态**：`getupdates` 响应按协议原文构造（含 `get_updates_buf`、`item_list`、`ref_msg`），不吃"简化 dict"
- mock 上游：`httpx.MockTransport` 或本地 aiohttp server，覆盖 200/`ret=-14`/429/超时/`x-encrypted-param` 缺失
- 媒体：固定 aeskey 做加密→解密往返，断言字节一致；缺 key 时走跳过分支
- 分块：3999/4000/4001 边界；`client_id` 唯一性断言
- 卡片：`test_card_ir.py` 的静态检查必须过（业务层不得出现平台字段）；渲染快照断言"标题+编号"
- 绑定：`wait→scaned→need_verifycode→confirmed` 状态机；`expired` 重取码 ≤3 次

## 8. 风险与对策

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| 1 | 协议是"按客户端代码反推"，非正式契约，腾讯可随时改 | 整条渠道失效 | 版本号如实上报（`bot_agent=grape/<ver>`）；字段解析全部容错；协议层集中在 `wechat/protocol.py` 单文件便于改 |
| 2 | `ret=-14` 账号会话暂停 1h（风控） | 渠道静默不可用 | 明确暂停 + 计 `session_paused_risk`；不重试不放大 |
| 3 | 绑定的是"某个微信号身份" | 账号资产/合规风险 | v1 只允许**本人绑定**；前端明确提示"绑定你的微信身份，随时可在微信侧解绑" |
| 4 | 无卡片/无按钮/无群/无编辑 | 体验弱于企微 | 卡片降级为文本+编号；产品预期先对齐；未来若腾讯开放再补 |
| 5 | 媒体 AES-128-ECB 细节（hex vs base64 key） | 图上不去/图打不开 | 收发都做双形态兼容（hex32 与 base64 16B），单测覆盖 |
| 6 | 多账号同 Gateway 时游标串号 | 消息重投/丢消息 | 游标按 `channel_installation.id` 独立存，不和 bot_token 混存 |
| 7 | 本机无公网 | — | 不需要公网：全部主动出站（长轮询 + POST），这点优于公众号 |

## 9. 待拍板（4 条）

1. **二维码交付**：前端弹窗展示（推荐，和现有 IM 渠道页一致）｜还是终端打印 ASCII 兜底（两者可都做，成本 +20 行）
2. **M1 范围**：先只做"文本收发"跑通（推荐）｜还是 M1+M2 一起交（一次到位，改动面更大）
3. **v1 媒体范围**：图片+文件（推荐）｜语音+视频也做（语音依赖微信侧转写，视频体积大、失败率高）
4. **卡片的编号上限**：对齐 Telegram 的 25 个｜还是收紧到 10 个（微信纯文本，太长可读性差）

## 10. 验收标准

- 单聊：文本、图片、文件双向通；`/repo` 卡片以"标题+编号"呈现，回 `1` 正确选中
- 绑定：网页扫码 → 30s 内 `channels` +1，重启后游标续接不重投
- 异常：`-14` 出现时渠道暂停且看板可见；断网 1 分钟后自动重连
- 指标：`/health/realtime` 的 `total_messages_*` 与日志一致，`degraded_by_reason` 各项可解释
- 回归：`pytest tests/im` 全绿（当前基线 525 passed，新增用例后应 ≥ 700）