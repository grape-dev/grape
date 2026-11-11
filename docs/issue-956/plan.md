# Issue #956 Plan：钉钉渠道产品化实现方案

## 1. 后端 `src/api/routes/chat.py`

### 1.1 GET /chat/config 回显 provider 化（1465-1473 区）

```
provider = (cfg.im_provider or '').lower() → 不在 (feishu, dingtalk) 时归一 feishu
im 响应：
  provider: provider（前端回显当前渠道用）
  feishu:   {connected, app_id, app_secret_exists}   ← provider==feishu 时填真值，否则空/false
  dingtalk: 同上，provider==dingtalk 时填真值
  wecom:    {connected: false, app_id: ''}            ← 维持占位不变
```

- 无 secret 回传路径：仅 `app_secret_exists` 布尔（既有，保持）。

### 1.2 PUT /chat/config provider 分支（1520-1705 区）

载荷解析（新旧兼容）：

```
im = body.im
provider = im.provider（显式，feishu|dingtalk；非法值 → 400 不支持的 IM 渠道）
channel 字典：
  - 显式 provider 且 im.<provider> 子 dict 存在 → 用子 dict（旧前端形态）
  - 显式 provider 无子 dict → 用平铺 im.app_id / im.app_secret（新前端形态）
  - 无 provider 键 → 兼容：im.feishu 存在 → feishu；im.dingtalk 存在 → dingtalk；
    都无 → 普通保存（#431 语义不动）
explicit_unbind = body 有 im 键且命中渠道载荷且 app_id 为空（新形态同义）
```

保存分支（channel.app_id 非空）：
- 复用既有 VOD 占用 / 他用户占用 / 换绑清理校验（provider 无关，app_id 全局唯一不变）
- `global_im_bot_config` 登记行：既有 `INSERT OR REPLACE (bot_app_id, bot_type, bot_name)`
  对 dingtalk 同样生效（`is_active DEFAULT 1` → 登记即 is_active=1，租约可发放）
- `kwargs["im_provider"] = provider`（有/无 secret 两路径都落 provider）
- `sync_legacy_installation(..., channel_type=provider)`（写入口透传渠道类型）
- BotManager 动态增删 bot：仅 feishu（legacy BotManager 为飞书 WS 协议，钉钉不走）

解绑分支：`_purge_chat_im_binding(..., provider=provider)`。

### 1.3 `_purge_chat_im_binding` 补 provider 维度（1477）

签名：`_purge_chat_im_binding(user_id, gitcode_user, old_app_id, provider="feishu")`

| 清理对象 | feishu | dingtalk |
|---|---|---|
| global_im_bot_config 注册行（无他引用） | ✅ | ✅ |
| unified_user_mapping 飞书三列 | ✅ | ❌（飞书专属） |
| chat_conversations（按 im_channel_id） | ✅ | ✅ |
| repo_im_group_binding | ✅ | ✅ |
| bot_seen_messages | ✅ | ✅ |
| feishu_onboard_task | ✅ | ❌（飞书专属，钉钉无此行） |

### 1.4 错凭证可读报错

占用冲突已有 400 detail.message；secret 格式不做网络校验（连接失败由引擎侧归因，
验收第 3 项的"错凭证"指重复占用/非法 provider → 400 可读文案，服务不崩、不落半行：
先校验后写入，异常路径无脏 INSERT）。

## 2. 引擎写桥 `src/im/engine_wiring.py`

- `sync_legacy_installation(..., channel_type)` 已有形参（#936 预留），chat.py 调用点补传。
- `_backwrite_legacy_installation` user 轨当前硬编码 `im_provider='feishu'` → 增加
  `channel_type` 形参并透传（否则钉钉保存后反向回写会把 legacy 行 provider 打回 feishu）。
  repo 轨（VOD）保持 'feishu' 不变。
- `GRAPE_IM_WRITE_MODE=legacy` 分支语义不动。

## 3. 前端 `src/api/static/js/pages/im_channel.js`

- 删 dingtalk `disabled: true`（wecom 保持灰态）。
- 保存/解绑载荷改新形态：`im = {provider, app_id, app_secret}`（后端兼容旧形态，
  同步更新 tests/js/test_issue_294_im_unbind.js 断言）。
- 扫码配置按钮仅 feishu 渲染（钉钉无扫码链路）。
- 表单与飞书同款（App ID/App Secret 双输入，占位符随渠道名）；保存失败只 toast
  不重载 → 已存凭证不清空；错误文案透传 `e.message`（后端 400 detail.message 可读）。
- `loadImChannelConfig` 优先用响应 `im.provider` 回显当前渠道。

## 4. 测试（M3）

- `tests/im/test_chat_im_provider_routes.py`（新增，路由契约）：
  GET provider 化回显 / 不回传 secret；PUT 新旧载荷兼容；钉钉保存落
  im_provider + 登记行 + channel_installation(channel_type=dingtalk)；解绑按渠道清理
  （dingtalk 不碰 feishu_onboard_task/unified_user_mapping）；非法 provider 400；
  保存失败无脏行。
- `tests/js/test_issue_956_im_dingtalk.js`（新增，前端源码契约，node 断言）：
  无 disabled、载荷含 provider、扫码按钮 feishu-only、保存失败不重载。
- 回归：tests/im 全量 + tests/test_chat_config_im_preserve.py + tests/js/test_issue_294_im_unbind.js。

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| 登记行缺失 → 配了也不连 | 保存路径既有 INSERT OR REPLACE 保底；契约测试断言行存在且 is_active=1 |
| backwrite 硬编码 feishu 覆写 provider | channel_type 透传 + 单测断言 backwrite 后 provider=dingtalk |
| 破坏 #431/#825 旧语义 | 载荷解析保持"无 im 键=普通保存"；回归测试全跑 |
| secret 泄漏 | GET 仅布尔；测试断言响应无 secret 值 |
