# Issue #1095 方案设计：Card IR + 渠道渲染器

## 架构

```
业务层 handler_async / vod_handler_async
        │  构造 Card(blocks/actions/tone/form)
        ▼
HandlerEgress.send_card()  ← 按 Capability.RICH_CARD + card_renderer 选择
        │
        ├─ 有 RICH_CARD + renderer + 开关 on → CardRenderer.render() → 平台载荷
        ├─ 无 RICH_CARD / 无 renderer / 开关 off → card.render_text() + degraded
        └─ legacy 模式 → FeishuCardRenderer 渲染后走 legacy api（1:1 回滚）
        ▼
ChannelSender / Replier → Channel.send() → 平台
```

## 关键设计

1. **扁平不嵌套**：`Block(kind=text|fields|divider|select|actions|button)`，现有卡片最深两层足够。
2. **交互语义化**：`Action.id + params`；`action_payload()` 在渲染边界生成 `{"cmd": "/bind 3", ...}`，
   与迁移前回调 value 字典零漂移，入站 `parse_card_action` 零改动。
3. **Tone 语义色**：`DEFAULT/INFO/NEUTRAL/SUCCESS/WARNING/DANGER` → 飞书 `template` 映射在各 renderer 内。
4. **form 两种形态**：`v1`（elements 交互行）/ `v2`（schema 2.0 内联按钮 + behaviors），严格对齐迁移前外观。
5. **能力位诚实申报**：飞书 `RICH_CARD|CARD_ACTION`；钉钉 `RICH_CARD` 因其 renderer 已就绪（展示对齐），
   不申报 `CARD_ACTION`（按钮无回调）。`self_check_card_capabilities()` 在引擎启动时告警。
6. **空内容硬护栏**：`HandlerEgress._send`（kind=text 空文本）、`DingtalkSender.send`（空内容 /
   非 actionCard 平台载荷）、`send_action_card_via_webhook`（空 text + 空 btns）三处显式失败。
7. **双轨开关**：`GRAPE_IM_CARD_IR` 默认 on；off 时 replier 路径降级文本（可秒级回滚）。
8. **降级归因**：`EngineMetrics.degraded_by_reason` 记录三类原因，随 `/health/realtime` 暴露。

## 迁移策略

- 批 1/2：IR + renderer + egress 路由（不动调用点）。
- 批 3：按功能簇迁移 `handler_async.py` 调用点，旧 JSON 快照 fixture 断言外观零漂移。
- 批 4：钉钉 actionCard 渲染 + sender 分支 + 空文本硬护栏。
- 批 4.5 / 批 5：生命周期落库 / 钉钉动作回收，待开放问题拍板后单独实施。

## 风险与对策

| 风险 | 对策 |
|---|---|
| 飞书外观漂移 | 快照契约测试 + legacy 开关 |
| 迁移面积大 | 按功能簇分批，`pytest tests/im` 每批回归 |
| 钉钉按钮无回调 | 带 params 按钮降级为命令提示并计 `card_action_unsupported`，不出空按钮 |
| `select_static` 钉钉无等价物 | 渲染为编号文本列表 |
| 双轨维护成本 | 默认 on，验证后清理旧路径 |