# Issue #1095 Spec：IM 卡片中立模型（Card IR）

> 设计依据：Issue #1095 正文 + 附件《中立卡片模型（Card IR）设计 · weekly-09》。
> 目标分支：`weekly-09-IM`（基线含 #1008 出站信封抽象 @ 2cb33eab9）。

## 目标

为 IM 出站建立渠道中立卡片模型 Card IR（`src/im/channel/card.py`）：

- 业务层只构造 `Card / Block / Action / Tone`，不出现 `lark_md` / `behaviors` / `template` 等平台字段；
- 各渠道由 `CardRenderer` 渲染为本平台形态（飞书 lark JSON / 钉钉 actionCard / 共享纯文本）；
- 无 `RICH_CARD` 渠道统一压平为文本并计 `card_text_fallback`，禁止透传平台 JSON；
- 修复钉钉声明 `RICH_CARD` 却静默发空消息（`ok=True`）的缺陷。

## 范围

### In Scope

- Card IR 数据模型（扁平 blocks + 底部 actions，语义 action id + 结构化参数，Tone 语义色）。
- 三个渲染器：飞书（v1 elements / v2 schema 2.0）、钉钉（actionCard）、共享 text。
- 能力位：`RICH_CARD` 语义收紧为「能渲染 IR」；新增 `CARD_ACTION = 1 << 8`；启动自检。
- `handler_async.py` 21 处 lark 结构调用点迁移为 Card IR。
- 空内容硬护栏（replier egress + 钉钉 sender + actionCard transport）。
- `degraded_by_reason` 指标（`card_text_fallback` / `card_action_unsupported` / `card_empty_guard`）。
- 附带缺陷：`_BIND_SUCCESS_CARD_TPL` 重复定义、`_flatten_card` 丢按钮/下拉。

### Out of Scope

- 附件类卡片迁移（走独立两跳通道）。
- 钉钉动作回调（批 5，需卡片实例 API + 回调订阅）。
- 卡片生命周期落库 `channel_outbound_card`（批 4.5，待拍板后单独实施）。
- 其他 IM 渠道适配。

## 验收标准

1. 同一 IR fixture × 3 renderer 契约测试：飞书 JSON 等价、钉钉无空按钮、文本保留全部可见文本与动作提示。
2. 空内容硬护栏：任一路径发空 → `failed` 并告警。
3. 能力位断言 + 启动自检：声明 `RICH_CARD` 必有 renderer；飞书 `CARD_ACTION`、钉钉不申报。
4. `/repo` `/chat` `/task` `/help` + autopilot 派发链路 mock 驱动验证。
5. `pytest tests/im` 全绿 + 新增 ≥25 条。
6. 静态检查：业务层禁止 `lark_md` / `behaviors` / `tag` 平台字段。
7. `degraded_by_reason` 指标可从 `/health/realtime` 观察。

## 待拍板开放问题（不阻塞本期已落地范围）

1. 附件类卡片是否迁移（建议不动）。
2. 钉钉交互目标：仅展示对齐 vs 批 5 回调。
3. `opaque` 逃生舱保留期限（建议批 5 结束删除）。
4. 批 4.5 卡片生命周期落库是否纳入本期。