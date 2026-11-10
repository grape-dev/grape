# Issue #1728 · 任务拆解（task）

## M1 接入 + 文本

- [x] `Type.WECHAT`、`protocol.py`（端点/头/base_info/容错访问器/client）
- [x] `login.py` 二维码状态机 + `chat.py` 扫码绑定接口
- [x] `channel.py` 长轮询 + 游标持久化 + `ret=-14` 暂停 + notifystart/stop
- [x] `inbound.py` 文本/媒体/语音/ref_msg
- [x] `outbound.py` 文本 4000 分块 + client_id + context_token
- [x] `engine_wiring.py` `_wechat_factory` + 注册 + renderer/validator 映射

## M2 媒体

- [x] `media.py` AES-128-ECB + PKCS#7、key 双形态、CDN 上传 + `x-encrypted-param` 重试
- [x] 入站媒体描述（storage_key JSON）+ AdapterApi 两跳下载/解密
- [x] 上传失败降级文本 + `media_upload_failed`

## M3 卡片降级

- [x] `renderers/wechat.py`「标题 + 正文 + 1..N 编号」
- [x] 复用 `channel_outbound_card` 语义：数字选择走既有 `_handle_selection`
- [x] `ref_msg` 引用还原到 `raw["ref_text"]`

## M4 前端 + 指标

- [x] `_IM_PROVIDERS` 加 wechat + config 回显
- [x] `im_channel.js` 平台卡 + 扫码弹窗（PIN）+ 状态回显
- [x] typing（getconfig → sendtyping）
- [x] `degraded_by_reason` 新增四项；`flap_by_cause` 动态 `ilink_longpoll`

## 测试

- [x] `tests/im/test_wechat_protocol.py`（请求头/ret/重试/分块）
- [x] `tests/im/test_wechat_media.py`（AES 往返/key 双形态/上传重试）
- [x] `tests/im/test_wechat_inbound.py`（真实帧/媒体/语音/ref_msg/去重键）
- [x] `tests/im/test_wechat_outbound.py`（分块/client_id/context_token/降级）
- [x] `tests/im/test_wechat_card.py`（标题+编号/选择不重复/空卡降级）
- [x] `tests/im/test_wechat_login.py`（状态机/PIN/expired 刷新 ≤3）
- [x] `tests/im/test_wechat_channel.py`（游标/-14 暂停/去重/生命周期）