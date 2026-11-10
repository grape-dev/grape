# Issue #1728 · 微信（ilink Bot / ClawBot）第七渠道适配器 Spec

基线：`weekly-09-IM` @ `e3214174e`｜设计事实源：腾讯官方 `@tencent-weixin/openclaw-weixin`
v2.4.10 `docs/protocol_zh_CN.md` + `src/channel.ts`（MIT）。

## 1. 目标

为 IM 引擎新增微信 ilink Bot 渠道，对齐既有五渠道「接入 / 发送 / 卡片降级」三段契约：

- **接入**：网页出二维码 → 微信扫码绑定（支持 PIN）→ 凭证落 `channel_installation`，
  Supervisor ≤30s 自动接管；`getupdates` 长轮询，游标 `get_updates_buf` 持久化。
- **发送**：文本 4000 字符分块，每块独立 `client_id`（uuid4，幂等），
  `context_token` 回传；图片/文件经 CDN AES-128-ECB + PKCS#7 加密上传。
- **卡片降级**：微信无卡片/按钮/回调/编辑/群 → Card IR 渲染为「标题 + 正文 + 1..N 编号」，
  用户回数字走既有 `_handle_selection`（不新增选择逻辑）。

## 2. 非目标（v1 不做）

- 个人微信 hook（Wechaty/padlocal，违反协议、封号风险）；
- 群聊、卡片/按钮、流式气泡刷新（协议无此能力）；
- 「他人绑定」（需 `bot_spam_ticket` + 微信支付实名，v1 仅本人扫码）。

## 3. 功能需求

| 编号 | 需求 |
|------|------|
| FR-1 | 扫码绑定：`POST /api/chat/im/wechat/login/start` → 二维码；`GET .../status` 短轮询代理；`need_verifycode` 透传 PIN；`confirmed` 落库，30s 内 channels +1 |
| FR-2 | 入站：文本 / 图片 / 文件 / 语音（微信侧转写）/ 视频；`ref_msg` 引用上下文；Bot 自消息丢弃 |
| FR-3 | 出站：文本 4000 分块、图片、文件；`client_id` 幂等；`context_token` 正确回传 |
| FR-4 | 卡片降级：Card IR → 标题 + 正文 + 编号；回数字走 `_handle_selection` |
| FR-5 | 体验：typing（getconfig → sendtyping）、notifystart/stop 生命周期 |
| FR-6 | 指标：沿用 `/health/realtime`；`degraded_by_reason` 增 `media_download_failed` / `media_upload_failed` / `inbound_empty` / `session_paused_risk` |

## 4. 协议硬约束

- 端点 base 默认 `https://ilinkai.weixin.qq.com`，服务端可经 `baseurl` 重定向；
- 请求头：`Authorization: Bearer <bot_token>` + `AuthorizationType: ilink_bot_token`
  + `X-WECHAT-UIN` + `iLink-App-Id: bot`；鉴权 POST 带 `base_info{channel_version,bot_agent}`；
- `ret/errcode = -14` → 账号会话暂停 1h：暂停轮询、计 `session_paused_risk`，不狂重试；
- 绑定码 `expired` / `verify_code_blocked` 自动刷新 ≤3 次，仍失败即停止。

## 5. 验收标准

- 单聊文本 / 图片 / 文件双向通；`/repo` 卡片以「标题 + 编号」呈现，回 `1` 正确选中；
- 扫码 30s 内 `channels +1`；重启后游标续接不重投；
- `-14` 出现时渠道暂停且看板可见；断网 1 分钟后自动重连；
- `/health/realtime` 指标与日志一致；`pytest tests/im` 全绿。