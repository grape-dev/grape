# Issue #936 Task：钉钉适配器任务拆解

- [ ] T1 markdown.py：split_markdown 15000 字节分块 + 围栏闭合（+测试）
- [ ] T2 inbound.py：Stream 事件 → InboundMessage golden 翻译（+测试）
- [ ] T3 outbound.py：DingtalkSender 分块发送/能力位/错误包装（+测试）
- [ ] T4 channel.py：WS 循环 ACK 先行 3s / 去重 / semaphore 限流 / 错误分类（+测试）
- [ ] T5 resolvers.py：身份/媒体两跳解析（+测试随 T2/T4）
- [ ] T6 engine_wiring：注册 DINGTALK 工厂 + 通用装配抽取 + 绑定表 channel_type（+双安装隔离测试）
- [ ] T7 全量回归：pytest tests/im 全绿；核心 diff 核对（src/im/engine、src/im/channel 零改动）
- [ ] T8 提交 + PR（base=weekly-09-IM）+ A2A QA
