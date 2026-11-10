# Issue #956 Task：任务拆解与进度

## M1 后端
- [x] 基线：pytest tests/im 184 全绿（26.69s）
- [x] chat.py GET /chat/config provider 化回显（含 im.provider、dingtalk 真值、不回传 secret）
- [x] chat.py PUT /chat/config provider 载荷分支（新旧兼容、落 im_provider、channel_type 透传、
      BotManager 仅 feishu、非法 provider 400）
- [x] _purge_chat_im_binding 补 provider 维度（钉钉跳过飞书专属清理）
- [x] engine_wiring._backwrite_legacy_installation 补 channel_type 形参（user 轨透传）

## M2 前端
- [x] im_channel.js 去 dingtalk 灰态
- [x] 保存/解绑载荷改 {provider, app_id, app_secret}
- [x] 扫码按钮仅 feishu；保存失败不重载不清凭证；错误提示透传 e.message

## M3 测试收口
- [x] tests/im/test_chat_im_provider_routes.py 新增（路由契约）
- [x] tests/js/test_issue_956_im_dingtalk.js 新增（前端契约）
- [x] 回归：tests/im 全量 + tests/test_chat_config_im_preserve.py + tests/js/test_issue_294_im_unbind.js
- [x] 自审查（code-review 5 轴 + pre-pr 增量检查）
