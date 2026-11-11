# Issue #936 Plan：钉钉适配器实现方案

## 模块落位（对齐设计文档 §2.2）

```
src/im/adapters/dingtalk/
  __init__.py     依赖铁律 docstring + 公共导出
  markdown.py     split_markdown（15000 字节分块 + 围栏闭合）
  inbound.py      Stream 事件 payload → InboundMessage（golden 契约）
  outbound.py     DingtalkSender（markdown 分块发送 / 纯文本 / 能力位）
  channel.py      DingTalkChannel（WS 接收循环：ACK 先行 3s / 去重 / 限流 / 错误分类）
  resolvers.py    身份/媒体解析（附件两跳：只产 MediaRef，VLM 在 worker）
src/im/engine_wiring.py
  + _dingtalk_factory 注册（复用抽出的通用 Router 装配）
  + SqliteBindingStore.upsert 可带 channel_type（绑定表渠道列真实化）
tests/im/
  test_dingtalk_inbound.py     golden 翻译
  test_dingtalk_markdown.py    15000 字节围栏闭合
  test_dingtalk_outbound.py    发送器分块/能力/错误包装
  test_dingtalk_channel.py     ACK 先行 + 全链路 golden + 去重
  test_dingtalk_engine.py      工厂注册 + 双安装会话隔离
```

## 钉钉 Stream 事件契约（golden 输入）

```
{
  "headers": {"contentType":"application/json", "messageId":"...", "eventId":"...",
              "eventType":"chat.message"|"SYSTEM", "appKey":"...", "timestamp":"..."},
  "data": {"conversationId":"cid...", "msgId":"mid...", "conversationType":"1"|"2",
           "senderStaffId":"...", "senderNick":"...", "msgtype":"text"|"picture"|"file",
           "text":{"content":"@bot hello"}, "content":"...",
           "downloadCode":"...", "sessionId":"..."}
}
```

- conversationType："1"→P2P，"2"→GROUP；群内 @ 前缀剥离（剥离后空 → addressed_to_bot=False）。
- msgtype 映射：text→TEXT，picture→IMAGE，file→FILE，rich media→MEDIA。
- ACK 帧：`{"code":200,"headers":{"contentType":"application/json","messageId":<mid>},"messageId":<mid>,"data":{}}`。

## 引擎装配（engine_wiring）

1. 把 `_feishu_factory` 的注入点装配抽成通用 `_router_wiring(cfg, channel_type, sender, build_channel)`；
   feishu / dingtalk 各自薄工厂调用（`channel_builder` 注入钩子两平台共用）。
2. `ImEngine.start()` 增 `registry.register(Type.DINGTALK, self._dingtalk_factory)`。
3. `_TypedBindingStore` 包装（dingtalk 绑定行 channel_type='dingtalk'），Router 协议不变。

## 测试策略

- 适配器层不触网：endpoint_fetcher / ws_connect / lease_checker / seen 钩子全可替身（同 feishu 模式）。
- 引擎层：FakeChannel + FakeApi 驱动 Router 全链路；双安装用 `channel_installation` 两行
  （channel_type feishu/dingtalk 各一）验证绑定行隔离。
- 回归：pytest tests/im 全量（17 存量套件 + 5 新套件）。
