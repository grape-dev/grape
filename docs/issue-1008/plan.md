# 方案设计：Issue #1008 出站收敛为纯 Replier

## 分层与数据流（改造后）

```
handler_async / vod_handler_async
    egress.<reply_text|reply_card|send_raw_card|add_reaction|update_*>   ← 门面（legacy 位置签名）
        OutboundMessage(chat_id, text, metadata{msg_id,title,template,raw_card,...})
            Replier.reply / Replier.edit            ← 能力位降级决策（RICH_CARD / MESSAGE_EDIT）
                CountingSender → ChannelSender.send ← ok→sent / 失败→failed（#870 钩子）
                    飞书传输：reply_card / reply_text / send_card_to_chat（按 metadata 路由）
                    钉钉传输：sessionWebhook（入站登记，出站按 conversationId 复用）
```

## 关键决策

1. **门面签名兼容**：`HandlerEgress` 方法保持 legacy 位置参数（ctx, msg_id, ...），
   `chat_id` 作可选尾参；装配层按消息注入 `default_route`（消息级 BindingKey），
   handler 无需逐调用点改签名 → 测试替身（AsyncMock）零迁移。
2. **平台假设下沉**：`om_` 判定从 handler 删除，改 `egress.message_id_valid()`
   （replier：适配器声明校验器——飞书 om_ / 钉钉恒真；legacy：om_，与改造前一致）。
   钉钉事故根因（msgId 形态误判 → 换 id 分支 → chat_id 空）由此闭合。
3. **钉钉回复目标复用**：DingtalkChannel 入站登记 `sessionWebhook`
   （WebhookRegistry，TTL 90min）；出站按 conversationId 查表，无 webhook →
   LookupError → SendResult(ok=False) → failed 计数（不再静默成功）。
4. **双模式灰度**：`GRAPE_IM_EGRESS_MODE` + `data/im_egress_mode` 覆盖文件；
   dispatch 时实时判定：replier → HandlerEgress(Replier 链)；legacy → 原样
   透传 FeishuAsync。replier 未就绪（渠道未建连/测试替身）自动回落透传。
5. **失败/降级语义**：空路由 → `record_failed` + 结构化 warning；
   REACTION/MESSAGE_EDIT/RICH_CARD 缺失 → `record_degraded`（内容不丢）；
   snapshot 新增 `total_messages_degraded` + health() 新增 `egress_mode`
   （/health/realtime im 段可见）。
6. **legacy BotManager 链路**（engine=off）同样包 HandlerEgress 门面（无 replier
   → 全量透传），保证两态契约一致。

## 风险与对策

- 飞书卡片回归：raw_card 走 metadata 原样透传（legacy_send_card_reply 与原
  `_send_card_reply` 逐行一致）；replier 侧由 test_egress_contract 双渠道回放
  + 既有 navigation/card 测试回归守卫。
- 流式原地更新：MESSAGE_EDIT 飞书在位 → api.update_message 透传（parity）；
  钉钉降级追加。
- 并发竞态：HandlerEgress 每次分发按消息新建（default_route 不跨消息共享）。
