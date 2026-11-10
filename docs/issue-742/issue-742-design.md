# Issue #742 — IM 渠道三层架构 P0 落地设计与实现

## 目标

将 grape IM 层抽象为「im_channel 契约 + im_engine 引擎 + 适配器」三层架构，
支持飞书/钉钉/Slack/wecom/telegram 多渠道扩展，达成「加平台 = 零改核心」。
完整设计见 Issue #742 附件《grape IM 渠道重构设计文档 v2.0》（双锚定版）。

## 本次交付范围（P0：契约层 + 数据模型 + 双轨回填，不动运行时）

### 1. im_channel 契约层（src/im/channel/，零平台依赖）

| 文件 | 内容 |
|---|---|
| `types.py` | Type / ChatType / Scope（双轨显式化）/ MsgType |
| `message.py` | InboundMessage（信封）/ OutboundMessage / MediaRef（P4 引用不内联）/ Source / ReplyCtx / SendResult |
| `capability.py` | Capability 位掩码（声明不协商） |
| `channel.py` | Channel ABC（connect/disconnect/send/capabilities）+ ChannelConfig（raw 不透明）+ PermanentChannelError（A5 短路）+ CancelContext |
| `registry.py` | Type→Factory 注册表（last-writer-wins，nil 静默拒绝） |

### 2. im_engine 引擎层（src/im/engine/，只 import im_channel）

| 文件 | 内容 | 资产迁移来源 |
|---|---|---|
| `hashring.py` | 一致性哈希环 50 vnodes | feishu_async.py L1148-1171（A1 原样） |
| `lease.py` | LeaseStore 协议（表名参数化解耦） | bot_lease.py（A2 三态接管保护，一行不改） |
| `dedup.py` | 两阶段去重 claim/mark/release | feishu_async.py L28-63（A3 跨重启持久化） |
| `session.py` | 统一绑定（BindingKey 规则 §5.2）+ RouteDecision（⑤b 双轨路由） | handler_async/vod_handler_async 公共骨架 |
| `router.py` | 统一九步入站流水线（§4.1：平台路由→安装→去重→群过滤→身份→路由→会话→持久化+Mark→命令→防抖） | P2 的解法 |
| `replier.py` | 出站降级（无富卡能力→纯文本）+ 绑定引导卡 | A4/A7 纪律 |
| `supervisor.py` | 安装枚举→环预筛→租约→Registry.build→connect；永久错误→revoked；fingerprint 热重建 | A1/A2/A5/A6/A7 |

### 3. 飞书适配器（src/im/adapters/feishu/，只 import im_channel）

| 文件 | 内容 |
|---|---|
| `inbound.py` | parsed dict → InboundMessage 翻译（含 card.action.trigger，P0 双写对比基线） |
| `outbound.py` | FeishuSender（update_multi: true 坑断言） |
| `channel.py` | FeishuChannel 契约骨架（run_bot 硬功夫 P1 全量迁入） |
| `markdown.py` | 复用既有 lark_md（不重写） |
| `resolvers.py` | 身份/媒体翻译（VLM 不内联，P4） |

### 4. 数据模型（scripts/migrations/001_channel_tables.py，幂等）

4 张表 + §5.3 第 1 步双轨回填：
- `channel_installation`：统一 user 轨（chat_agent_session，app_id 去重）+ repo 轨（devops_agent_session，app 可跨仓库共享）；
- `channel_chat_session_binding`：合并 repo_im_group_binding + vod_conversations；
- `channel_inbound_dedup`：两阶段（bot_seen_messages 双写期保留只读 7 天）；
- `channel_sender_identity`：external_identities 的 IM 视图。

### 5. 依赖铁律（哨兵测试 tests/im/test_raw_sentinel.py 静态守卫）

1. im_channel 不 import 平台 SDK / im_engine / 适配器；
2. im_engine 不 import 适配器；
3. 适配器永不 import im_engine 内部；
4. 核心永不读 InboundMessage.raw。

## 测试（tests/im/，50 条全绿）

- 契约层 9 条（§7.2 8 条 + config 不透明）；
- 哈希环 4 条（A1 行为固化，含扩容只迁邻近 <0.6）；
- 两阶段去重 5 条（A3 重启存活、infra 错误 release）；
- Router 流水线 13 条（§7.3：④先于⑤、命令不二次解释、防抖合并、OK reaction 先行、媒体不阻塞、repo 轨默认 vod、会话粘性、显式 agent 优先）；
- 飞书入站 7 条（golden 基线 + card.action.trigger）；
- 飞书出站 4 条（update_multi 坑）；
- 回填 4 条（§7.8：app_id 去重、app 共享、fingerprint 幂等/轮转）；
- 哨兵 4 条（依赖铁律）。

相邻既有 IM 测试（test_bot_manager_load / test_cluster_im_lease / test_feishu_async / test_feishu_handler）96 条全绿，零回归（本次全部为新增文件，零修改既有代码）。

## 后续阶段（本 Issue Spec §8）

- **P1**：run_bot 硬功夫迁入 FeishuChannel.connect；Supervisor 切换 channel_installation 枚举；VLM 挪 worker；灰度 48h。
- **P2**：钉钉适配器接入，验收 im_engine 核心 diff = 0。
- **P3/P4**：体验增强与多平台扩展。
