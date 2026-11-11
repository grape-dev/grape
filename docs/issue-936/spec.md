# Issue #936 Spec：IM 渠道改造 P2 —— 钉钉适配器

> 完整设计依据：Issue #936 附件《grape IM 渠道重构设计文档 v2.0》§2.2 / §3 / §4.4 / §5.2 / §7.6 / §8-P2。

## 目标

落地 P2 阶段：新增 `src/im/adapters/dingtalk/`（channel/inbound/outbound/markdown/resolvers + `__init__.py`），
`src/im/engine_wiring.py` 注册 `Type.DINGTALK` 工厂，验证 `src/im/engine` / `src/im/channel` 核心零 diff。

## 范围

- 新增 `src/im/adapters/dingtalk/`（6 文件）
- 修改 `src/im/engine_wiring.py`：注册 DINGTALK 工厂（装配层，非引擎核心）
- 新增 `tests/im/test_dingtalk_*.py`：golden 全链路 + 15000 字节围栏闭合 + 双安装隔离

## 关键设计

1. **依赖铁律**（§2.1）：适配器只 import `im_channel`，永不 import `im_engine` 内部；核心不读 `InboundMessage.raw`
   （raw 哨兵测试自动覆盖新目录）。
2. **BindingKey = conversation_id**（§5.2 钉钉行）：`Source.chat_id` = `conversationId`，无话题平台不做线程合成。
3. **ACK 先行 3s**（§3.1 契约细节）：钉钉 Stream 模式有 3s 级 ACK 时限，收到 DATA 帧先回 ACK（`{"code":200,...}`）
   再 fire-and-forget 分发 handler（A4 纪律：semaphore 限流 + 异常隔离）。
4. **附件两跳**（P4 修正）：适配器只翻译 `downloadCode` → `MediaRef`（storage_key），不内联下载/VLM；
   理解在 worker 侧（resolvers 可注入下载钩子，handler 协程零阻塞）。
5. **markdown 15000 字节分块**（§4.4，参考 multica dingtalk_markdown.go L10-33）：
   `split_markdown` 按字节分块（不截断多字节字符），每块 ≤ 15000 bytes，**代码围栏闭合**——
   块内 "```" 出现次数为奇数时补闭合围栏，下一块以开栏续接（防 markdown 渲染塌方）。
6. **错误契约**：链路断开 → `ChannelError`（Supervisor 退避重连）；凭证永久失败 → `PermanentChannelError`。
   SDK 缺失（dingtalk-stream 未安装）→ `ChannelError`（可重试性质，安装后恢复），不硬依赖 SDK 导入。
7. **引擎核心 diff = 0**：`src/im/engine/*`、`src/im/channel/*` 一行不改；钉钉接入仅通过
   `engine_wiring`（装配层）注册工厂——第三平台零改核心即架构证明（§8-P2 验收）。

## 验收

- 钉钉 golden 全链路绿 + 15000 字节围栏闭合绿
- `git diff origin/weekly-09-IM -- src/im/engine src/im/channel` 为空（核心零 diff）
- 同一用户飞书（user 轨）+ 钉钉（user 轨）双安装，会话绑定隔离正确
- raw 哨兵 + `tests/im` 全量回归绿
