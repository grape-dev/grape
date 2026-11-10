# 任务拆解：Issue #1008

| # | 任务 | 状态 |
|---|------|------|
|
 1 | 契约层：Capability 增 REACTION 位；metrics 增 degraded 维度 + 出站键 | ✅ |
| 2 | Replier：caps()/edit()（MESSAGE_EDIT 降级追加）+ 失败语义文档化 | ✅ |
| 3 | 新增 src/im/engine/egress.py：HandlerEgress 门面（双模式 + 能力探测 + 空路由计失败）+ egress_mode 开关 | ✅ |
| 4 | handler_async / vod_handler_async：api.* → egress.* 全量改名（67 处发送调用）；_send_card_reply 收敛为 egress.send_raw_card；删除 om_ 前缀假设（视频 ack + 换 id 分支改能力探测） | ✅ |
| 5 | 飞书出站：FeishuSender metadata 路由参数透传（契约向后兼容，update_multi 断言保留） | ✅ |
| 6 | 钉钉出站：WebhookRegistry（sessionWebhook 入站登记/出站复用/TTL）+ 空路由 LookupError + async 传输回调；channel.py 入站登记 | ✅ |
| 7 | engine_wiring：per-app Replier 登记；_dispatch_agent/apply_command/add_reaction 走 HandlerEgress；平台传输闭包按渠道分支；health() 增 egress_mode | ✅ |
| 8 | main.py legacy BotManager 链路包 HandlerEgress（两态契约一致） | ✅ |
| 9 | 测试：test_egress_contract（能力位降级/空路由计失败/双渠道回放/legacy parity/开关/钉钉 msgId 形态）+ test_handler_no_direct_send（grep=0 守卫）；既有断言适配（card 文本提取改 send_raw_card；health 键集增量） | ✅ |
| 10 | 验证：pytest tests/im 全绿（218 passed）；根目录 IM 相关 9 文件仅余 3 个基线既有失败（stash 基线复核一致，与本改造无关）；grep 直连计数 = 0 | ✅ |
| 11 | 真机验证（钉钉「你好」/飞书卡片回归）：待 QA/人工按验收 ②③ 执行 | ⏳ |
