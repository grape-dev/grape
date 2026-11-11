# Issue #1095 任务拆解

| 批次 | 任务 | 状态 |
|---|---|---|
| 批1 | `channel/card.py` Card IR + `Capability.CARD_ACTION` + 共享 text renderer + 契约测试 | 已完成 |
| 批2 | 飞书 `card_renderer` + `HandlerEgress.send_card()` 路由 + `GRAPE_IM_CARD_IR` 双轨开关 | 已完成 |
| 批3 | 迁移 `handler_async.py` 21 处调用点为 Card IR；删重复 `_BIND_SUCCESS_CARD_TPL`；旧 JSON 快照断言 | 已完成 |
| 批4 | 钉钉 actionCard renderer + sender 分支 + 空内容硬护栏 + 能力位如实申报 | 已完成 |
| 批4.5 | `channel_outbound_card` 表 + `Card.update_key` 持久化 | 待拍板 |
| 批5 | 钉钉动作回收（卡片回调入站 + `CARD_ACTION` 置位） | 待拍板/单独评估 |
| 附带 | `_flatten_card` 补齐按钮/下拉 | 已完成 |
| 验收 | `degraded_by_reason` 指标 + 静态检查 + 链路 mock 测试 | 已完成 |