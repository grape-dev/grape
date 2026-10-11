# 方案设计 — Issue #294：IM 渠道配置支持解绑

## 方案概述

前端为已绑定渠道加「解绑」入口；后端把「置空即解绑」补成完整清理事务，对齐换绑（rebind）时已有的清理链路。
不新增 API 端点，复用现有 PUT 语义。

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 解绑触发方式 | A. 独立 DELETE 端点 / B. 复用 PUT 置空语义 | **B** | Chat 侧已存在置空分支，前端改动最小；VOD 侧加解绑标记字段即可，避免新增 API 面 |
| VOD 解绑信号 | A. `im_app_id` 传空串 / B. 显式 `im_unbind` 布尔 | **B** | `AgentConfig.im_app_id` 默认空串，普通保存（不带 IM 字段）会误触发解绑；显式标记防误清 |
| 注册表清理条件 | A. 无条件删 / B. 无其他引用才删 | **B** | 对齐换绑逻辑（chat.py / feishu_onboard.py），app_id 全局唯一但可能跨用户/跨 repo 复用 |
| 会话数据处理 | A. 保留历史会话 / B. 删除 | **B** | 对齐换绑（`DELETE FROM chat_conversations/vod_conversations`），避免悬空引用 |
| 解绑后 Agent 配置 | A. 全量覆盖 / B. 解绑-only 请求只清 IM | **B** | `PUT /api/agents/...` 未传 backends/skill 时会回落默认引擎与空技能；解绑不应顺带重置引擎/技能/事件订阅 |

## 实现路径与落点

### 1. i18n（前置）

- `src/api/static/locales/zh/chat.json` / `en/chat.json` → `im_channel` 增 `unbind` / `unbind_confirm` /
  `unbind_success` / `unbind_failed`。
- `src/api/static/locales/zh/agents.json` / `en/agents.json` → `config` 增同名 4 键（VOD 侧复用）。

### 2. 前端 — Chat（`im_channel.js`）

- `renderImChannelBody`：`isConfigured`（`app_id` 非空）时在「扫码配置 / 保存」同排渲染红色「解绑」按钮。
- 新增 `unbindImChannelConfig()`：`confirm(_imt('unbind_confirm'))` → `PUT /chat/config`（`im[provider] =
  {app_id:'', app_secret:''}`，带当前 repo 查询参数）→ 成功 toast + `loadImChannelConfig()` 刷新状态。

### 3. 后端 — Chat（`chat.py`）

新增模块级 `_purge_chat_im_binding(user_id, gitcode_user, old_app_id)`，在请求**显式携带 `im`** 且
`app_id` 置空（解绑）时、`_set_chat_config()` 之后触发：

- `SELECT COUNT(*) FROM chat_agent_session WHERE im_app_id=? AND user_id!=? AND im_app_id!=''` 判断其他引用；
- 无引用 → `DELETE FROM global_im_bot_config WHERE bot_app_id=? AND bot_type='chat_bot'`，并清空
  `unified_user_mapping` 中该 app 的 `feishu_open_id/feishu_name/app_id`（open_id 为 app 级，app 释放后失效）；
- 有其他引用 → 只清空当前用户自己的映射行；
- 一律 `DELETE FROM chat_conversations WHERE im_channel_id=?`；
- 既有 `bot_manager.remove_bot(old_app_id)` 调用保留（`elif not feishu.get("app_id") and old_app_id` 分支）；
- 置空分支同时补 `im_provider=""`，避免留下「有 provider 无凭证」的脏状态。

### 4. 后端 — VOD（`agents.py`）

- `AgentConfig` 增 `im_unbind: bool = False`（显式解绑标记）。
- 新增 `_unbind_vod_im(repo_key)`：会话行 `im_app_id/im_app_secret/im_provider` **显式写空串**
  （`upsert_agent_session` 传 `None` = 保留既有值，置空必须 `""`）；无其他 repo 引用时删
  `global_im_bot_config` 行并清 `unified_user_mapping` 该 app 映射；删该会话 `vod_conversations`。
- `update_agent_config` 新增解绑分支 + 解绑-only 请求（未带 backends/skill/skill_id/subscribe_events）
  **直接返回**，只清 IM 三字段，保留引擎/技能/事件订阅；随后 `bot_manager.remove_bot(old_app_id)`。

### 5. 前端 — VOD（`agents.js`）

- VOD Agent 页顶部新增「已绑定渠道」卡片（`_agentVodImSection`）：**仅在 `data.im.im_app_id` 非空时渲染**
  （未绑定用户页面无变化），展示 App ID / 已绑定群数 + 「解绑」按钮。
- 新增 `unbindVodIm()`：confirm → `PUT /agents/vod/{owner}/{repo}` 且 body 仅 `{im_unbind: true}`
  → toast + 重新拉取详情刷新。
- 说明：VOD IM 配置面板在「场景中心重构」中已整体移除（`agentCfgPanel` 不再渲染），故解绑入口落在
  VOD Agent 页而非已不存在的配置面板。

## 影响范围

| 模块 | 改动 |
|------|------|
| `src/api/static/js/pages/im_channel.js` | 解绑按钮 + `unbindImChannelConfig()` |
| `src/api/static/js/pages/agents.js` | `_agentVodImSection()` + `unbindVodIm()` + VOD 页接入 |
| `src/api/routes/chat.py` | `_purge_chat_im_binding()` + 置空分支触发 + `im_provider` 清空 |
| `src/api/routes/agents.py` | `im_unbind` 字段 + `_unbind_vod_im()` + 解绑-only 早返回 |
| `src/api/static/locales/{zh,en}/chat.json`、`agents.json` | 解绑文案键 |
| `tests/test_chat_routes.py`、`tests/test_agents_routes.py`、`tests/js/test_issue_294_im_unbind.js` | 新增用例 |

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| `upsert_agent_session` 的 `im_*` 传 `None` = 保留，置空不生效 | 解绑显式写空串 `""`；单测断言 DB 字段为空 |
| 解绑误清其他用户 / 仓库共用的注册表行 | 严格「无其他引用才删」，COUNT 前置校验；跨引用时只清本用户/本仓库 |
| 解绑-only 请求把引擎/技能重置为默认 | 解绑-only 早返回，不进入引擎/技能写入路径；单测断言 `runtime_id`/`skill_id` 不变 |
| `bot_manager` 为空（api 未就绪）时 `remove_bot` 静默跳过 | 沿用既有 `try/except + log.warning`；重启后 BotManager 按表重建 |
| 前端旧缓存无新 i18n 键显示裸 key | 走 i18n 缓存版本机制（`I18N.init(cacheVer)`），发布时刷新 |

## 测试策略

- `tests/test_chat_routes.py`：Chat 解绑清理（凭证 / 注册表 / 会话 / 映射 / `remove_bot`）+ 绑-解-重绑循环。
- `tests/test_agents_routes.py`：VOD 解绑清理（三字段置空、注册表、群会话、引擎/技能保留、重绑）+ 普通保存不误清。
- `tests/js/test_issue_294_im_unbind.js`：前端入口条件渲染、解绑请求载荷、确认取消路径、双语 i18n 键、后端关键语义。
