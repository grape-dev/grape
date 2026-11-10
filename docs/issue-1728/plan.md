# Issue #1728 · 方案设计（plan）

## 1. 三段落点（对既有契约零改动）

| 断面 | 契约 | 微信实现 |
|------|------|----------|
| 接入 | `channel.Channel`（connect 内循环回调 inbound_handler） | `wechat/channel.py`：getupdates 长轮询 + 游标持久化（`channel_installation.config["cursor"]`） |
| 发送 | `engine/replier.ChannelSender` / `engine/egress` 能力位降级 | `wechat/outbound.py`：sendmessage + 4000 分块 + client_id + context_token + 媒体上传 |
| 卡片 | `channel/card.py` Card IR + `renderers/<渠道>.py` | `renderers/wechat.py`：纯文本「标题 + 正文 + 编号」，回数字复用 `_handle_selection` |
| 能力位 | `channel/capability.py` 只声明不协商 | `TEXT \| RICH_CARD \| ATTACHMENT`；不报 CARD_ACTION / MESSAGE_EDIT / REACTION / THREAD |
| 装配 | `engine_wiring.py` registry.register + 两处映射 | 新增 `_wechat_factory` + 注册 + renderer/msg_id_validator 映射 |
| 白名单/前端 | `api/routes/chat.py:_IM_PROVIDERS` / `im_channel.js` | 加 wechat；平台卡 + 扫码弹窗 + PIN + 状态回显 |

## 2. 模块拆分（7 文件 + renderer）

- `protocol.py`：端点 / 请求头 / `base_info` / 消息枚举 / 容错访问器 / `WechatClient`
  （Bearer + `ret` 校验 + 5xx/429 指数退避 + 长轮询）。协议知识唯一落点，便于腾讯改协议时单点修改。
- `login.py`：二维码状态机（wait → scaned → need_verifycode → confirmed；expired 刷新 ≤3），
  进程内 TTL 10min；凭证由 API 路由落 `channel_installation`。
- `channel.py`：长轮询循环 + 游标推进/持久化 + 去重 + `ret=-14` 暂停 + notifystart/stop + typing。
- `inbound.py`：`WeixinMessage → InboundMessage`；`chat_id = from_user_id`；
  item_list 文本/语音转写/图片/文件/视频；`ref_msg` 引用还原；Bot 自消息丢弃。
- `outbound.py`：`WechatSender`（分块 / client_id / context_token / 媒体）+ `WechatAdapterApi`
  （媒体两跳 / 身份 / typing / 主动推送）。
- `media.py`：AES-128-ECB + PKCS#7；key 兼容 hex32 / base64；getuploadurl → 密文 POST →
  `x-encrypted-param` 重试 ≤3；失败降级计 `media_upload_failed`。
- `resolvers.py`：context_token / message→chat 台账、用户显示名、typing ticket 缓存、媒体描述编解码。
- `channel/renderers/wechat.py`：Card IR → 文本；数字选项不重复编号；动作降级为「回复 `/cmd`」。

## 3. 关键权衡

- **游标按 installation 存**（`config["cursor"]`，不混 bot_token），多账号不串号。
- **`ret=-14` 不抛 ChannelError**：暂停后正常退出，避免 Supervisor 立即重连放大风控。
- **无按钮环境复用 pending selection**：`/repo` 卡片正文已含 1..N 编号，
  renderer 对带 `num` 参数的动作不再重复编号，回数字命中既有 `_handle_selection`。
- **媒体失败不丢内容**：上传失败发送「附件发送失败：文件名 + 链接」并计降级。

## 4. 分批

M1 接入/文本 → M2 媒体 → M3 卡片降级 → M4 前端扫码/typing；测试随批补齐。