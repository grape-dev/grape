# Issue #1254 Plan（分批与提交映射）

## 分批

| 批 | 内容 | 落点 |
|---|---|---|
| 1 接入 + 文本 | 长连接 / 订阅 / 心跳 / 接收循环 / 文本入站出站 | `channel.py`、`inbound.py`、`outbound.py`、`markdown.py` |
| 2 媒体 | 收帧即下载 + 解密 + 缓存；出站三步分片上传 | `resolvers.py`、`outbound.py:upload_media` |
| 3 卡片 | Card IR → 内联 template_card；回调 → CARD_ACTION；无模板降级 | `cards.py`、`renderers/wecom.py` |
| 4 前端/路由 | 解禁企微卡、字段标签、provider 白名单与真实回显 | `im_channel.js`、`chat.py` |
| 5 流式 | 不做（能力位不申报） | — |

## 装配（符号定位，不按行号）

- `registry.register(Type.WECOM, self._wecom_factory)`
- `_wecom_factory` → 复用 `_channel_factory(cfg, "wecom", ..., sender_factory=...)`
- `_card_renderer_for("wecom") → WecomCardRenderer`
- `_msg_id_validator_for("wecom") → 恒 True`（企微按 chatid/userid 路由，msg_id 形态无关）
- `BUILTIN_CARD_RENDERERS += ("wecom",)`（声明 RICH_CARD 必有 renderer）

## 提交

| commit | 内容 |
|---|---|
| 1 | `feat(#1254)` 适配器 + 渲染器 + 引擎装配 + 4 个测试文件 |
| 2 | `fix(#1254)` 回填按 `im_provider` 落 `channel_type` |
| 3 | `feat(#1254)` 前端解禁 + provider 白名单 |
| 4 | `docs(#1254)` 需求与设计文档（含实现差异 §12） |
| 5 | `docs(#1254)` issue-1254 spec/plan/task |

## 风险与对策

| 风险 | 对策 |
|---|---|
| 设计稿 5s 回复窗口与官方 24h 冲突 | 取官方 24h，窗口/时钟可注入，两条路径都有用例 |
| 模板卡片需预建 template_id（误解） | 内联 `card_type` + 结构体，免配置；开关降级 markdown |
| 空载荷护栏挡住卡片 | renderer 产出 markdown 兜底 + template_card 双字段 |
| 回填把企微配置当飞书 | 按 `im_provider` 落 channel_type（无 schema 变更） |
| 单帧异常打死接收循环 | fire-and-forget + semaphore + 全异常兜底（钉钉 #1008） |