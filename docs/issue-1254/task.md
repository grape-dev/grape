# Issue #1254 Task（编码阶段清单）

状态：编码完成，单测全绿，PR #991 已建，待 QA 审查与主人真机验收。

## 代码

- [x] `src/im/adapters/wecom/__init__.py` 依赖铁律 + 平台语义声明
- [x] `channel.py` 订阅（10s ack，失败即重连/永久）/ 30s ping / 90s 读超时 / kicked 退出 / 去重 / 异常不穿透
- [x] `inbound.py` 字符串 body 容错、single/group、@ 裁决、媒体引用、`template_card_event` → CARD_ACTION
- [x] `outbound.py` respond/send 分流、send_msg msgtype 守卫、20480 字节分块、三步分片上传
- [x] `resolvers.py` 收帧即下载 + AES-256-CBC 解密 + 缓存
- [x] `cards.py` 内联 template_card + event_key ↔ action_payload + task_id 台账
- [x] `markdown.py` 分块 + 代码围栏闭合
- [x] `src/im/channel/renderers/wecom.py` Card IR → 模板卡片 + markdown 兜底
- [x] `engine_wiring.py` 注册/工厂/渲染器/校验器/自检
- [x] `chat.py` + `im_channel.js` 前端解禁与 provider 白名单
- [x] `001_channel_tables.py` 回填按 `im_provider`

## 测试

- [x] `test_wecom_inbound.py`（真报文形态：字符串 body、chattype/chatid、aeskey、无 eventtype）
- [x] `test_wecom_outbound.py`（窗口内/外、text 永不出站、分块、上传、secret 不落观察哨）
- [x] `test_wecom_card.py`（模板卡片 / 按钮 key 解码 / 降级 / 台账）
- [x] `test_wecom_channel.py`（订阅/心跳/超时/踢下线/坏帧/去重/媒体/租约/装配）
- [x] 既有 `tests/im` 全绿；只改一处既有用例（未知渠道断言改用 slack）

## 待主人真机验收

- [ ] FR-1 配置后 ≤30s `channels +1`、发「你好」有回复、`channel_inbound_message` 落行
- [ ] FR-2 群聊仅 @ 触发
- [ ] FR-3 3s 短答复与 2 分钟长答复均送达
- [ ] FR-4/5 发图可读、语音得转写、产出文件可回传
- [ ] FR-6 卡片点「2」真换仓、`repo_im_group_binding` 落行
- [ ] FR-7 回调含操作人 userid
- [ ] FR-8 配置/启停/解绑/状态四项真实
- [ ] 多副本：`channels=1` 且不重复回复