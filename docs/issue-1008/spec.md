# Spec: Issue #1008 — IM 出站收敛为纯 Replier

> 来源：Issue #1008 正文 + 附件《grape IM 渠道重构设计文档 v2.0》§3.3/§4.4/§7 +
> 《im-egress-replier-refactor-design.md》实施备忘（开发者澄清：一次做完、不分批）。

## 目标

1. agent 出站统一唯一入口：`OutboundMessage → Replier.reply → Channel.send`；
   handler_async（44 处发送直连）/ vod_handler_async（23 处）全部改走 egress 门面，
   核心路径零平台名（grep `api.reply_*|send_text_to_user|add_reaction|...` = 0）。
2. 删除共享代码平台假设：`om_` 前缀判定 / open_id 定向 / card_v2 直拼 →
   改能力位（Capability）探测与适配器 `capabilities()/send()` 实现。
3. 修复钉钉回复静默丢弃事故：msgId 不以 om_ 开头 → 误入换 id 分支 →
   chat_id 空 → `deliver skipped` 静默丢；修复后钉钉按 conversationId 路由 +
   sessionWebhook 回复目标复用，空路由计 `metrics.failed` + 结构化日志。
4. 失败计数：`EngineMetrics` 增 degraded 维度；出站 sent/failed/degraded 键
   进 `/health/realtime` im 段。
5. 双模式灰度：`GRAPE_IM_EGRESS_MODE=replier|legacy`（默认 replier；
   env / data/im_egress_mode 覆盖文件可运行期切换，秒级回滚）。

## 范围（In）

- `src/im/engine/egress.py`（新增：HandlerEgress 出站门面 + 开关）
- `src/im/engine/replier.py`（MESSAGE_EDIT 降级 / 失败语义 / caps()）
- `src/im/engine/metrics.py`（degraded 维度 + 出站键）
- `src/im/channel/capability.py`（新增 REACTION 位）
- `src/im/handler_async.py`、`src/im/vod_handler_async.py`（api.* → egress.*，
  om_ 假设删除）
- `src/im/engine_wiring.py`（per-app Replier 登记 / dispatch 走 egress /
  平台传输闭包按渠道分支）
- `src/im/adapters/feishu/outbound.py`（metadata 路由参数透传，契约向后兼容）
- `src/im/adapters/dingtalk/outbound.py`（sessionWebhook 注册表 + 回复目标复用 +
  async 传输回调）、`dingtalk/channel.py`（入站登记 webhook）
- `src/api/main.py`（legacy BotManager 链路同样经 HandlerEgress 门面）
- 测试：`tests/im/test_egress_contract.py`（能力位降级/空路由计失败/双渠道回放/
  legacy parity/开关）、`tests/im/test_handler_no_direct_send.py`（grep=0 守卫）

## 范围（Out）

- `download_image` / `download_file` 语义变更（适配器入站能力，P4 MediaRef 另案）
- 钉钉 Stream SDK 建连 wiring（#936 P3，本单只保证出站契约与回放测试）
- 统一富卡片协议 / 中央能力协商（设计文档 §10 明确不做）
- 哈希环 / 租约 / 去重重写（行为不变）

## 验收标准（Issue 原文）

1. replier 模式下两 handler 的 `api.reply_*/send_text_to_user/add_reaction`
   发送直连 grep 计数为 0；
2. 钉钉真机「你好」收到回复、无 deliver skipped、failed=0；
3. 飞书真机卡片不回归（降级/分块不丢内容）；
4. pytest tests/im 全绿含新增出站契约用例；
5. legacy 开关行为与改造前一致。
