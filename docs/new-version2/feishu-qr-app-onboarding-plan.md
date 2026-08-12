# 飞书扫码一键建应用改造方案

> 状态：**已实施**（后端 `src/api/routes/feishu_onboard.py` + 前端 `chat.js`/`agents.js` + 测试 `tests/test_feishu_onboard.py`）
> 目标：将「手动创建飞书应用 + 手动粘贴 app_id/app_secret」的接入方式，替换为「扫码一键创建应用」。
> 依据：飞书开放平台《[扫码一键创建应用](https://open.feishu.cn/document/mcp_open_tools/scan-to-create-an-app-in-one-click)》，基于 `lark-oapi` SDK 的 `register_app` / `aregister_app`（OAuth 2.0 Device Authorization Grant, RFC 8628）。

---

## 1. 现状

当前接入飞书应用完全靠手动：

```
用户在飞书开放平台创建应用
   → 配置事件订阅（im.message.receive_v1）
   → 配置权限（发消息等）
   → 复制 app_id / app_secret
   → 在 DevOps Bot admin 面板手动粘贴
       · Chat Agent:  src/api/static/js/pages/chat.js:60
       · VOD Agent:    src/api/static/js/pages/agents.js:117
   → 落库 chat_agent_session / devops_agent_session + 登记 global_im_bot_config
   → BotManager.load_from_db() 启动 bot（src/im/feishu_async.py:893）
   → 走飞书 WS 长连接接收 im.message.receive_v1 事件
```

痛点：步骤多、易错（事件订阅漏配、权限漏配、秘钥复制出错）。

## 2. 目标方案

复用飞书 SDK 的 Device Authorization Grant 流程，让用户在 admin 面板「扫码即得凭据」：

```
admin 面板点「扫码配置」
   → 后端调 lark.aregister_app()（异步版，不阻塞事件循环）
   → SDK 回调返回二维码 URL → 前端展示二维码
   → 用户用飞书 App 扫码 → 飞书内打开「创建/确认应用」页面
   → 用户确认 → SDK 轮询拿到 {client_id, client_secret, user_info}
   → 后端凭据落库 → 触发 add_bot() 建立 WS 连接
   → 前端轮询状态，显示「配置完成」
```

## 3. 可行性结论

| 维度 | 结论 |
|---|---|
| SDK 版本 | ✅ `requirements.txt` 已含 `lark-oapi>=1.0`，本机 1.7.0 ≥ 1.5.5 |
| 返回凭据 | `client_id` → `im_app_id`，`client_secret` → `im_app_secret`，直接可用 |
| 事件订阅 | `addons.events.items.tenant: ["im.message.receive_v1"]` 预填，项目正消费该事件 |
| 发送权限 | `addons.scopes.tenant: ["im:message:send_as_bot"]` 预填 |
| 长连接 | 项目走 WS 长连接，不依赖事件请求 URL；addons 无法传递的「事件 URL/security/加密 key」恰好无影响 |
| 多 bot | 每次扫码创建新 app_id，天然适配 BotManager 按 app_id 去重管理 |
| 更新已有应用 | 传 `app_id` 参数可给已有 VOD/chat bot 增量补权限 |
| 用户绑定 | 返回 `user_info.open_id` + `tenant_brand`，可顺带写入 `unified_user_mapping` |

## 4. 服务端流程设计

### 4.1 新增 API 端点（建议放在 `src/api/routes/`，可新建 `feishu_onboard.py`）

```
POST /api/feishu/onboard/start
  body: { bot_type: "chat_bot" | "vod_bot", repo_key?: string }
  → 创建 pending 记录，生成 task_id
  → 启动后台任务 lark.aregister_app(...)
  → 返回 { task_id }

GET  /api/feishu/onboard/status?task_id=xxx
  → 返回 { status: pending|waiting_confirm|connected|failed,
           qr_url?, expire_in?, error? }

POST /api/feishu/onboard/cancel   (可选)
  → 取消后台扫码任务
```

### 4.2 状态机

```
pending（等待 SDK 生成二维码）
  → waiting_confirm（二维码已就绪，等待用户扫码确认）
  → connected（凭据已落库，bot 已启动）
  → failed（超时 / 用户拒绝 / SDK 异常）
```

### 4.3 数据库变更（`src/api/db_setup.py`）

新增任务表（进程内内存 dict 亦可，但建议落库以便异常恢复/多实例）：

```sql
CREATE TABLE IF NOT EXISTS feishu_onboard_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT UNIQUE NOT NULL,        -- 前端轮询用
    bot_type TEXT NOT NULL,              -- chat_bot | vod_bot
    repo_key TEXT DEFAULT '',            -- vod_bot 的仓库维度
    user_id INTEGER NOT NULL,            -- 操作人
    status TEXT DEFAULT 'pending',       -- pending|waiting_confirm|connected|failed
    app_id TEXT DEFAULT '',
    app_secret TEXT DEFAULT '',
    qr_url TEXT DEFAULT '',
    expire_in INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT DEFAULT ''
);
```

注意：`user_accounts` 已有 `feishu_app_id` / `feishu_app_secret` 字段（旧设计），本方案统一落到 `chat_agent_session` / `devops_agent_session`，与现状一致。

### 4.4 落库与建连

扫码成功后：

1. `client_id` → `im_app_id`，`client_secret` → `im_app_secret`
2. **chat_bot**：写入 `chat_agent_session`（`repo_key=''`），并登记 `global_im_bot_config`（`bot_type='chat_bot'`），走 `update_chat_config` 的幂等逻辑
3. **vod_bot**：写入 `devops_agent_session` + `global_im_bot_config`（`bot_type='vod_bot'`），走 `agents.py` 的 VOD 配置逻辑
4. 调用 `BotManager.add_bot(app_id, app_secret, bot_type)`（`src/im/feishu_async.py:852`）动态建连
5. 复用现有 app_id 换绑清理逻辑（`src/api/routes/chat.py:632`）：旧 app 若无其他引用则从 `global_im_bot_config` 释放

## 5. 前端设计

### 5.1 Chat Agent 页（`src/api/static/js/pages/chat.js`）

在现有 App ID / App Secret 输入框旁增加「扫码配置」按钮：

- 点击 → 调 `POST /api/feishu/onboard/start` → 弹出二维码弹层
- 弹层内轮询 `GET /api/feishu/onboard/status`
- 二维码就绪 → 展示二维码 + 剩余有效期
- 用户扫码确认后 → 状态变 connected → 自动回填/隐藏输入框 → 关闭弹层并刷新配置

### 5.2 VOD Agent 页（`src/api/static/js/pages/agents.js`）

同样的入口，`bot_type='vod_bot'`，`repo_key` 取当前仓库。

## 6. addons 预填配置

```python
addons = {
    "preset": True,   # 保留平台基础模板；如权限冗余再改 False 走最小模板
    "scopes": {
        "tenant": [
            "im:message:send_as_bot",   # 发送消息
            "im:chat:readonly",          # 读取群成员（get_chat_members 需要）
        ],
    },
    "events": {
        "items": {
            "tenant": ["im.message.receive_v1"],  # 项目唯一消费事件
        },
    },
}
```

> 权限清单最终需与 `FeishuAsync` 实际调用的接口逐一核对（reply_text/reply_card/send_card_to_user/get_chat_members/get_user_name）。

## 7. 关键实现注意事项

1. **必须用异步版 `aregister_app`**：`register_app` 是同步阻塞轮询（最长可能数分钟），会卡死 FastAPI 事件循环。用 `asyncio.create_task` 在后台跑，用 `task_id` 关联结果。
2. **事件循环兼容性需实测**：lark-oapi 内部实现需验证在项目现有 asyncio 环境下不互相阻塞（可作为首个验证项）。
3. **二维码过期处理**：`expire_in` 过期后任务标记 failed 或重新生成，前端提示用户重扫。
4. **并发限制**：同一用户同时只允许一个进行中的扫码任务（start 时检查未完成记录）。
5. **异常分类**：`AppAccessDeniedError`（用户拒绝）、`AppExpiredError`（过期）、`RegisterAppError`（其他，含 code/description）→ 分别映射前端文案。
6. **secret 脱敏**：前端回显与现状一致用 `********` 占位，避免日志打印明文 secret。

## 8. 变更文件清单（预估）

| 文件 | 变更 |
|---|---|
| `src/api/routes/feishu_onboard.py`（新增） | 扫码任务 API：start / status / cancel |
| `src/api/db_setup.py` | 新增 `feishu_onboard_task` 表 DDL |
| `src/api/routes/chat.py` | 复用落库逻辑；接入扫码成功回调 |
| `src/api/routes/agents.py` | 同上（vod_bot 分支） |
| `src/im/feishu_async.py` | 可能无需改动；add_bot 已支持动态建连 |
| `src/api/static/js/pages/chat.js` | 扫码按钮 + 二维码弹层 + 轮询 |
| `src/api/static/js/pages/agents.js` | 同上 |
| `src/api/main.py` 或路由注册处 | 挂载新路由 |
| `docs/feishu-agent-auto-config.md` | 标记旧 ISV/webhook 方案作废，指向本文档 |

## 9. 风险与待确认项

| 项 | 说明 |
|---|---|
| `aregister_app` 事件循环兼容 | 需在项目环境实测（首项验证） |
| 基础模板默认权限 | 需确认平台基础模板默认 scope，避免缺失长连接所需权限 |
| addons 只增不减 | 如需精简需 `"preset": False` 最小模板 |
| 换绑清理 | 扫码创建新 app_id 后旧 app 需按现有逻辑释放 |
| 付费/商务 | 扫码创建能力若涉及服务商资质需提前确认 |
| 多实例部署 | 若多 worker 进程需保证 task_id 全局唯一与跨进程状态可见（落库方案已覆盖） |

## 10. 测试用例设计

> 测试文件建议：`tests/test_feishu_onboard.py`（后端）、`tests/test_api_routes.py` 追加扫码相关类。
> 关键点：`lark.aregister_app` 不真正联网 —— 用 `unittest.mock.patch` mock SDK 回调，直接喂 `on_qr_code` / 结果返回值；落库与建连沿用现有 DB 测试约定（`os.environ["DEVOPS_DB_PATH"]` 指向测试库）。

### 10.1 后端单元/集成测试

**A. start 端点**
- A1 `start` 成功返回 task_id，记录 status=`pending`（DB 中 `feishu_onboard_task` 有行）
- A2 `start` 时同用户已有未完成任务 → 返回 409（并发限制）
- A3 `start` 参数校验：非法 `bot_type`（非 chat_bot/vod_bot）→ 400
- A4 `start` 后后台任务被创建（`asyncio.create_task` 调用被记录/mock）

**B. 扫码回调 / 状态流转**
- B1 后台任务调用 `aregister_app`，`on_qr_code` 回调被触发 → 任务置为 `waiting_confirm`，DB 存 `qr_url` + `expire_in`
- B2 mock `aregister_app` 返回 `{"client_id":"cli_xxx","client_secret":"sec_yyy"}` → 任务置 `connected`，`im_app_id`/`im_app_secret` 写入 `chat_agent_session`（chat_bot）
- B3 同 B2 但 bot_type=vod_bot → 写入 `devops_agent_session` + `global_im_bot_config`（`bot_type='vod_bot'`）
- B4 `add_bot` 被调用且参数正确（app_id / secret / bot_type）
- B5 `aregister_app` 抛 `AppExpiredError` → 任务 `failed`，`error` 含过期语义，前端可映射文案
- B6 `aregister_app` 抛 `AppAccessDeniedError`（用户拒绝）→ 任务 `failed`
- B7 `aregister_app` 抛 `RegisterAppError`（带 code/description）→ 任务 `failed` 且 error 含 code
- B8 结果返回的 `user_info.open_id` 正确写入 `unified_user_mapping`（P3 阶段，含 gitcode_user 关联）

**C. status 端点**
- C1 status 返回各状态正确映射（pending / waiting_confirm / connected / failed）
- C2 查询不存在的 task_id → 404
- C3 他人 task_id（跨用户）→ 404 或 403（防越权）

**D. cancel 端点**
- D1 取消进行中任务 → 状态置 failed（或 canceled），后台任务被取消
- D2 已 connected 任务取消 → 幂等，不破坏已有配置

**E. 换绑清理（复用 chat.py 现有逻辑）**
- E1 扫码新 app_id 后，旧 app_id 若被其他引用释放 / 未被引用则从 `global_im_bot_config` 删除（对齐 `tests/test_api_routes.py` 中现有换绑用例风格）
- E2 VOD 类型占用校验：扫码结果 app_id 已被 vod_bot 占用 → 拒绝绑定

### 10.2 前端测试

- F1 点击「扫码配置」→ 调 start、弹出弹层
- F2 弹层轮询 status：`waiting_confirm` 展示二维码 + 剩余时间
- F3 状态 `connected` → 自动关闭弹层并刷新配置（App ID 已回填 / 显示已连接）
- F4 状态 `failed` → 展示对应错误文案（拒绝/过期/其他）
- F5 二维码过期 → 显示「请重扫」并提供重新 start 入口
- F6 并发提示：已存在进行中任务时展示后端 409 文案

### 10.3 mock 参考

```python
# tests/test_feishu_onboard.py 骨架
import os, sys
os.environ["DEVOPS_DB_PATH"] = os.path.join(os.path.dirname(__file__), "..", "data", "test_sessions.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from unittest.mock import AsyncMock, patch

QR_URL = "https://example.com/qr?token=xyz"
RESULT = {"client_id": "cli_onboard_test", "client_secret": "sec_test", "user_info": {"open_id": "ou_xxx", "tenant_brand": "feishu"}}

def _patch_register(coro_result=None, side_effect=None, on_qr=None):
    """mock lark.aregister_app：优先触发 on_qr_code 回调，再返回 RESULT 或抛异常。"""
    async def _fake_aregister(**kwargs):
        if on_qr:
            kwargs.get("on_qr_code")({"url": QR_URL, "expire_in": 300})
        if side_effect:
            raise side_effect
        return coro_result or RESULT
    return patch("src.api.routes.feishu_onboard.lark.aregister_app", side_effect=_fake_aregister)
```

> 运行：`python -m pytest tests/test_feishu_onboard.py tests/test_api_routes.py -q`（对齐现有测试命令与 conftest 约定）。

## 11. 分阶段实施建议

1. **P0 验证**：独立脚本验证 `aregister_app` 在项目 asyncio 环境下跑通并返回凭据。
2. **P1 后端**：新增 `feishu_onboard.py` + DB 表，落地 chat_bot 扫码流程 + 10.1 A/B/C 用例。
3. **P2 前端**：Chat Agent 页接入扫码 UI + 10.2 用例。
4. **P3 扩展**：VOD Agent 页扫码；接入 `user_info` 写 `unified_user_mapping`；补 B8/E 用例。
5. **P4 收尾**：异常/过期/并发兜底，旧文档标记作废，补 D 用例与全量回归。
