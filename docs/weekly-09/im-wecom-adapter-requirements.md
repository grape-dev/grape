# 需求：企业微信（WeCom）IM 渠道接入

> 状态：待评审（2026-11-06）｜关联设计：`docs/weekly-09/im-wecom-adapter-design.md`
> 基线：`feat/im-dingtalk-interactive-card`（= origin/weekly-09-IM + 钉钉交互卡片批 5）
> 目标集成分支：`weekly-09-IM`

## 1. 背景与现状

### 1.1 现有 IM 引擎的分层（代码事实）

新 IM 引擎（#742 / #760 / #825 / #1008 / #1095）把渠道差异压在三个断面里，核心零平台名：

| 断面 | 契约位置 | 说明 |
|---|---|---|
| **接入（入站）** | `src/im/channel/channel.py` `Channel.connect/disconnect` + 适配器 `inbound.py` | **入站故意不在接口上**（依赖倒置）：适配器在自己的接收循环里调注入的 `inbound_handler`，核心绝不轮询渠道 |
| **发送（出站）** | `Channel.send()` + 适配器 `outbound.py`（Sender）+ `src/im/engine/replier.py`（Replier 门面）+ `src/im/engine/egress.py`（HandlerEgress） | 出站统一 `OutboundMessage → Replier → Channel.send`，业务层不再直连平台 API（#1008 已收敛） |
| **卡片** | `src/im/channel/card.py`（Card IR：Card/Block/Action/Tone）+ `src/im/channel/renderers/<渠道>.py` | 业务层只构造中立 IR；**平台私有格式禁止出现在业务层**（`tests/im/test_card_ir.py` 静态检查守着） |

横切契约两条：`capability.py`（能力位声明，核心不做降级决策）与各适配器 `resolvers.py`（chat/sender → 平台用户映射）。
引擎侧（`Supervisor` / `Router` / 租约 / 去重 / 指标）**不需要为新渠道改动**——这正是新增渠道成本可控的原因。

### 1.2 现状缺口

- 适配器只有 `feishu`（`src/im/adapters/feishu/`）与 `dingtalk`（`src/im/adapters/dingtalk/`）。
- `src/im/channel/types.py:19` 的 `Type.WECOM` 枚举**已存在**，但 `engine_wiring.py:1130/1132` 只注册了 FEISHU/DINGTALK → 企微安装行即使写进 `channel_installation` 也不会建连。
- 前端 `src/api/static/js/pages/im_channel.js:22-27` 企微卡是 `disabled: true`（"暂未开放"）；后端 `src/api/routes/chat.py:1483` 只有 `{"connected": false, "app_id": ""}` 占位。
- **历史文档的企微方案已过期**：`docs/new-version/tech-design.md:1263-1347` 描述的是 IM 引擎重构前的单文件 webhook 方案（`src/im/wecom.py` + `user_accounts.wecom_webhook_url/wecom_webhook_token`），该文件已不存在，且与现引擎"渠道 = Channel 实现 + 装配注册"的模型不兼容。**本需求不是"启用旧方案"，而是新写适配器。**

### 1.3 参考实现

上游 multica（本引擎的架构来源）已有企微适配器：`server/internal/integrations/wecom/`（约 140 文件），
采用企业微信「智能机器人」**长连接（WebSocket）**形态。本文档的协议事实均取自该实现与官方文档
（`developer.work.weixin.qq.com/document/path/101463`、`/101031`、`/101138`）。

## 2. 目标

用户在**企业微信**里就能用 grape：私聊机器人或群里 @ 机器人提问 → 收到回复；发图片/文件 → 能读懂；
需要选择仓库/会话时，卡片按钮可点、点了就生效。

### 2.1 功能需求

| 编号 | 需求 | 验收标准 |
|---|---|---|
| FR-1 | **接入**：用企微「智能机器人」的 `bot_id` + `secret` 建立长连接，收到私聊与群聊消息 | 配置后 ≤30s 内 `/health/realtime` 的 `channels` +1；在企微发"你好"收到回复；`channel_inbound_message` 落行 |
| FR-2 | **群聊约束**：群里只有 @ 机器人的消息才触发；私聊全部触发 | 群里不 @ 时引擎不派发任务（与飞书一致）；@ 后正常回复 |
| FR-3 | **发送**：回复走长连接；长任务（超过平台回复窗口）走主动推送 | 3 秒内的短答复与 2 分钟的长答复都能送达；推送失败有 `degraded/failed` 计数 |
| FR-4 | **媒体入站**：图片 / 文件 / 语音可用 | 发一张图，agent 拿到可读内容；发语音拿到转写文字（企微侧已转写） |
| FR-5 | **媒体出站**：agent 产出的文件能发回对话 | 生成的报告/图片在企微对话里可直接打开 |
| FR-6 | **卡片**：选择仓库 / 会话 / 任务的卡片可点，点完动作真实生效 | 点"2"后仓库真切换，`repo_im_group_binding` 落行，与"回复数字"路径结果一致 |
| FR-7 | **卡片回调**：能拿到点击者身份，并原地更新卡片 | 点击事件里有操作人 userid；卡片状态可更新（不要求 v1，见设计文档开放问题） |
| FR-8 | **前端入口**：IM 渠道页企微卡可配置、可启停、可解绑，状态真实回显 | 前端填 `bot_id`/`secret` 保存后连接建立；解绑后渠道断开；状态徽标反映真实连接 |

### 2.2 非目标（本需求不做）

- 企微「群机器人 Webhook」通道（只能发不能收，且需要公网回调）——除非后续确认为轻量通知场景单独开需求。
- 企微文档/日程/审批等办公能力接入。
- 表情回执、已读回执（平台能力缺失，能力位不申报，不虚报）。
- 多 bot 负载均衡（企微一个机器人只允许一条长连接，水平扩展靠租约，不靠多连接）。

## 3. 改动范围（预期）

| 层 | 文件 | 类型 |
|---|---|---|
| 适配器 | `src/im/adapters/wecom/{__init__,channel,inbound,outbound,cards,resolvers,markdown}.py` | 新增 |
| 卡片渲染 | `src/im/channel/renderers/wecom.py` | 新增 |
| 装配 | `src/im/engine_wiring.py`（工厂 + registry 注册 + renderer/validator 映射） | 修改（约 40 行） |
| 前端 | `src/api/static/js/pages/im_channel.js`（解禁 + 凭据表单 + 状态）、`src/api/routes/chat.py`（回显） | 修改（约 120 行） |
| 文档 | 本文件 + 设计文档 + README 索引 | 新增 |
| 测试 | `tests/im/test_wecom_{inbound,outbound,card,channel}.py` | 新增（约 450 行） |

三段落点与分批见设计文档 §4/§6。

## 4. 改动后数据流（一句一跳到哪）

**入站**：企微 WS 帧 `aibot_msg_callback` → `WecomChannel.connect()` 接收循环 → `inbound.py` 解析（图文混排/引用/媒体 aeskey）→ `InboundMessage` → 注入的 `inbound_handler` → Router（去重/身份/绑定）→ `handler_async`（legacy 兼容层）→ 回复经 `HandlerEgress → Replier → WecomSender.send` → WS `aibot_respond_msg` / `aibot_send_msg`。

**卡片点击**：企微 WS 帧 `aibot_event_callback`（`eventtype=template_card_event`）→ `inbound.py` 转 `InboundMessage(MsgType.CARD_ACTION)` → 走**同一份** `_handle_selection` → 结果卡经 renderer 渲染再发。

## 5. 测试用例设计

### 6.1 单元（帧解析）
- 真实帧 fixture（**教训：钉钉曾因 `data` 是 JSON 字符串而非对象，整条消息被静默丢弃**）：字符串/对象两种形态都要覆盖
- 私聊 / 群聊 / 图文混排 / 引用回复 / 语音 / 图片（带 aeskey）/ 未知 msgtype（应发"不支持的类型"回执而不是静默）
- 卡片事件：`template_card_event` → `MsgType.CARD_ACTION`，缺 chat 信息时忽略而非猜测

### 6.2 单元（出站）
- 短答复走 `aibot_respond_msg`；超出窗口走 `aibot_send_msg`
- **`aibot_send_msg` 只接受 markdown / template_card（不接受 text）** 的守卫：传 text 必须显式转换，不得发出被拒帧
- markdown 长度上限（`stream.content` 20480 字节 / 单条消息上限）截断策略
- 空内容硬护栏（`card_empty_guard` 同口径）

### 6.3 单元（卡片）
- Card IR → 企微模板卡片载荷快照；无卡片能力时降级为 markdown（不丢内容、不静默）
- 渲染层平台字段不泄漏：业务层源码静态检查仍通过（`test_card_ir.py`）

### 6.4 装配与 DB
- `registry.register(Type.WECOM, ...)` 后能按安装建渠道；同一 bot 只建一条连接（租约）
- 凭据落 `channel_installation.config`（`bot_id`/`secret`），不在日志明文
- 解绑/停用 → 30s 内 `channels` 归零

### 6.5 API
- `GET /api/im/status`、`GET /api/im/installations` 出现 wecom 安装且 `connected=true`
- `PUT /api/im/installations/{id}` 启停生效
- `POST /api/chat/config` 保存企微凭据写穿到新表

### 6.6 端到端（真机，主人验收）
- 私聊 `/repo` → 卡片 → 点数字 → 换仓成功
- 群聊 @ 机器人提问 → 回复；不 @ 不回复
- 发图片 → agent 描述图片内容
- 长任务（>5s）→ 主动推送到达

## 7. 向后兼容

- 新增渠道，不改动现有飞书/钉钉任何行为；`Type.WECOM` 枚举起就是预留位，无迁移。
- `channel_installation.channel_type='wecom'` 的历史脏数据：目前无（`chat.py:1483` 只是回显占位）；实现后前端才可能写入。
- 前端平台表 `disabled` 去掉后，老用户界面只是多一个可点卡片，不影响已配置渠道。
- 数据库**无 schema 变更**（凭据走 `config` JSON，与飞书/钉钉同构）。

## 8. 边界与风险

| 风险 | 影响 | 处置 |
|---|---|---|
| **一个机器人只能一条长连接** | 多副本部署会互相踢线、双回复 | 依赖现有 `Supervisor` 租约（TTL 180s + 续约）；实现时加多副本验证用例 |
| **`aibot_send_msg` 不接受 text** | 照飞书/钉钉习惯写会直接发失败 | 出站守卫 + 单测覆盖；统一走 markdown |
| **回复窗口很短（约 5s）** | 长任务回复丢失 | 窗口外自动切 `aibot_send_msg`，失败计 failed 并留日志 |
| **媒体 URL 只有 5 分钟有效 + 每条 URL 独立 aeskey** | 延迟下载就 403 / 解密失败 | 收帧时立刻下载落盘，密钥随帧携带不落库 |
| **模板卡片需先在企微侧建模板** | 卡片能力卡在配置，与钉钉 widgetInfo 同类风险 | 见设计文档 §10 开放问题 Q1：v1 允许纯 markdown 降级，卡片分批推进 |
| **群聊不 @ 也会收到帧** | 误回复、噪音 | 适配器侧判定 @ 机器人才派发（与飞书同口径） |
| **语音是企微侧转写** | 方言/噪声转写质量不可控 | 按文本处理，不承诺准确率 |

## 9. 验收清单（主人可逐项打勾）

- [ ] 私聊：`/repo` 卡片可点、点完换仓
- [ ] 群聊：@ 才有回复，不 @ 安静
- [ ] 图片：发图能读
- [ ] 语音：发语音能读（转写文本）
- [ ] 文件：agent 产出文件可回传
- [ ] 长任务：2 分钟答复不丢
- [ ] 前端：配置/启停/解绑/状态四项真实
- [ ] 多副本：两条实例只有一个持有连接，无双回复
---

## 10. 实现落地与验收映射（2026-11-06）

| 需求 | 实现落点 | 单测覆盖 | 真机验收（需主人配凭据） |
|---|---|---|---|
| FR-1 接入 | `wecom/channel.py`（订阅 10s ack / 30s ping / 90s 读超时）、`engine_wiring.py` 注册 + `_wecom_factory` | `test_wecom_channel.py::test_golden_message_flow_dispatches_translated_inbound`、`test_subscribe_*`、`test_ping_keepalive_is_sent_periodically` | 配置后 ≤30s `/health/realtime` channels +1；发「你好」有回复 |
| FR-2 群聊 @ | `inbound.py::strip_mention` + `addressed_to_bot` | `test_group_chat_text_at_bot_is_stripped_and_addressed`、`test_group_chat_without_at_is_not_addressed` | 群内 @ 才回复 |
| FR-3 发送 | `outbound.py` respond / send 分流 + 20480 字节分块 + 失败计 failed | `test_reply_within_window_*`、`test_after_window_switches_to_send_msg_with_chat_type`、`test_long_markdown_is_chunked_within_20480_bytes` | 3s 短答复与 2 分钟长答复均送达 |
| FR-4 媒体入站 | `inbound.media_items` + `resolvers`（收帧即下载 + AES-256-CBC 解密 + 缓存）、语音取企微转写 | `test_media_is_downloaded_and_decrypted_at_frame_time`、`test_voice_is_text_with_transcript_and_no_media_refs` | 发图可读、发语音得转写 |
| FR-5 媒体出站 | `outbound.upload_media` 三步分片 → media_id → file 帧 | `test_attachment_within_window_uploads_then_sends_file`、`test_attachment_outside_window_fails_explicitly` | agent 产出文件可在企微打开 |
| FR-6 卡片动作 | `renderers/wecom.py` + `cards.build_template_card`（button_interaction，key=action_payload） | `test_select_options_become_clickable_buttons_with_decodable_keys`（key → `/bind 2`） | `/repo` 卡片点「2」真换仓、`repo_im_group_binding` 落行 |
| FR-7 卡片回调 | `inbound.template_card_event_to_inbound`（event_key → CMD_ACTION，带 from.userid） | `test_template_card_event_becomes_card_action`、`test_card_event_without_route_is_ignored` | 点击事件里有操作人 userid |
| FR-8 前端 | `im_channel.js` 解禁 + 标签、`chat.py` provider 白名单与真实回显 | `test_chat_im_provider_routes.py::test_wecom_provider_saved_with_write_through`、`test_wecom_provider_*` | 配置/启停/解绑/状态四项真实 |
| 兼容 | 引擎三段契约零改动；回填按 provider 落 channel_type | `pytest tests/im` 341 全绿（含既有 314+ 用例） | 飞书/钉钉行为不变 |
| 多副本 | 复用 Supervisor 哈希环 + 租约；`disconnected_event` → ChannelError 触发重新归属 | `test_disconnected_event_raises_channel_error`、`test_lease_mismatch_returns_without_connecting` | 两实例只有一个 channels=1，无双回复 |

**设计稿口径修正**（详见设计文档 §12）：回复窗口按官方 24h（5s 只约束欢迎语/卡片更新）；
模板卡片为内联 card_type（无需预建模板）；出站媒体走 `aibot_upload_media_init/chunk/finish`。
