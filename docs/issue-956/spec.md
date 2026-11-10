# Issue #956 Spec：钉钉渠道产品化（前端录入 + 凭证落库 + 状态回显）

> 上游 Spec 由 issue Agent 发布（Issue #956 评论 id 198201409）。本文为 coding 侧落实现场的
> 需求基线快照（与上游 Spec 同口径），开发以本文 + plan.md 为准。

## Objective

IM 渠道页开放钉钉（去灰态）→ 填 AppKey/AppSecret → 保存落库
（`chat_agent_session.im_provider/im_app_id/im_app_secret` + `global_im_bot_config` 登记行 +
`channel_installation` 新表安装行）→ 引擎 ≤30s 建连可收发；解绑 ≤30s 归零；
飞书/钉钉双渠道隔离；全链路不回传 secret。

## In Scope

1. `GET /chat/config`（chat.py:1465-1473 区）回显 provider 化：读 `im_provider/im_app_id`
   真实值，按 provider 回显 feishu|dingtalk 的 `connected/app_id/app_secret_exists`，
   任何情况不回传 secret。
2. `PUT /chat/config`（chat.py:1520-1705 区）接受 `im.provider`（feishu|dingtalk，
   默认 feishu 兼容旧载荷）：落 `im_provider`；`sync_legacy_installation(..., channel_type=provider)`；
   登记行写入保持 `global_im_bot_config`（is_active=1 默认、bot_type='chat_bot'，租约前置）。
3. `_purge_chat_im_binding`（chat.py:1477）补 provider 维度：钉钉解绑跳过飞书专属表
   （`unified_user_mapping` 飞书三列、`feishu_onboard_task`），其余（`global_im_bot_config` /
   `chat_conversations` / `repo_im_group_binding` / `bot_seen_messages`）按渠道清理。
4. 前端 `im_channel.js`：去掉 dingtalk `disabled`（29-34 区）；表单与飞书同款；
   错凭证可读提示（沿用 400 detail.message 透传）；保存失败不清空已存凭证（失败不重载）；
   扫码按钮仅飞书显示。
5. 回滚：写路径仍受 `GRAPE_IM_WRITE_MODE` 控制，legacy 保持 #825 旧语义（钉钉仅 new 模式生效）。

## Out of Scope

- 附件/媒体透传（P3，能力位不得声明 ATTACHMENT）
- 钉钉扫码链路（表单录入即可）
- 引擎协议层改动（#936 已就绪）
- 企业微信渠道开放

## Acceptance Criteria（6 项）

1. 保存钉钉凭证 → `/health/realtime` im.channels ≤30s +1，重启自动重连（引擎既有行为）
2. 解绑 ≤30s 归零，`/chat/config` 回 `connected=false` 不回显 app_id
3. 错凭证可读报错（400 detail.message），服务不崩、无脏安装行
4. 双渠道 channels=2 会话不串（channel_type 分流，#936 既有引擎测试钉住）
5. `/chat/config` 全链路不回传 secret（仅 `app_secret_exists` 布尔）
6. `pytest tests/im` 全绿（基线 184）+ 新增路由/前端契约测试
