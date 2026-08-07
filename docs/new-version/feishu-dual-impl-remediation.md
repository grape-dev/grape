# 双飞书实现整改方案（IM Async 收敛）

> 目标：消除「新版异步框架 + 旧版同步命令处理」的混合状态，实现事件循环零阻塞、token 单一来源、无死代码、测试对齐生产。
>
> **实施状态：✅ 已实施**（Phase 1-6 完成）。涉及文件：`feishu_async.py` 新增异步 `sync_repo_feishu_mapping` / 规范化 `get_chat_members`；`handler_async.py` 自包含纯逻辑 + 新增 `process_command_async`；删除 `feishu.py` / `handler.py` / `vod_handler.py` / `feishu_bot.py`；测试迁移至 async 版。

---

## 一、现状与问题

### 1.1 当前文件与状态

| 文件 | 行数 | 角色 | 生产状态 |
|------|------|------|---------|
| `src/im/feishu.py` | 933 | 同步 HTTP + lark-oapi WS + token 缓存 | ⚠️ 工具函数仍被新版引用 |
| `src/im/handler.py` | 246 | 同步 Chat handler | 🔴 死代码（仅测试引用） |
| `src/im/vod_handler.py` | 446 | 同步 VOD handler | 🔴 死代码（仅 `feishu.py:738` 引用） |
| `src/im/feishu_bot.py` | 198 | 旧单 Bot 类 | 🔴 死代码（仅 `feishu.py:808` 引用 `_get_ws_endpoint_async`） |
| `src/im/feishu_async.py` | 874 | 新版多 Bot（BotManager/BotContext/FeishuAsync） | ✅ `main.py` 使用 |
| `src/im/handler_async.py` | 323 | 新版异步 Chat handler | ✅ `main.py` 使用 |
| `src/im/vod_handler_async.py` | 406 | 新版异步 VOD handler | ✅ `main.py` 使用 |

### 1.2 实际运行路径

生产唯一入口 `src/api/main.py` lifespan：

```python
from src.im.feishu_async import BotManager
from src.im.handler_async import process_chat_async
from src.im.vod_handler_async import process_vod_async
bot_manager = BotManager(max_concurrent_per_bot=10)
```

全仓库无任何生产代码调用旧 `start_ws` / `handler.process_chat` / `vod_handler.process_vod_message`，旧同步实现均为死代码。

### 1.3 核心问题

| # | 问题 | 严重度 |
|---|------|--------|
| P1 | `handler_async.py` 混用旧同步 `process_command` → `sync_repo_feishu_mapping` → `get_chat_members`（同步 `urlopen`） | **高**：阻塞事件循环 |
| P2 | token 双套：全局 `get_tenant_token()`（旧）vs `BotContext.token`（新） | 中：多 Bot 取错 token |
| P3 | 新版宣称不依赖 lark-oapi，但 WS 帧解析仍 import `lark_oapi.ws.pb` | 低：文档/依赖不符 |
| P4 | `handler.py` / `vod_handler.py` / `feishu_bot.py` / 旧 WS 函数死代码未删 | 低：维护噪音 |
| P5 | 测试测旧实现，生产跑新实现（`test_chat_handler.py` / `test_feishu_handler.py`） | 中：测试与生产脱节 |

---

## 二、阻塞影响分析（P1 根因）

### 2.1 阻塞链路

```
handler_async.py:123  result = process_command(...)              ← 同步 def
  → feishu.py:490  sync_repo_feishu_mapping(repo_key, chat_id)   ← 同步
      → feishu.py:362  get_chat_members(chat_id)                 ← urlopen 分页，每页 15s
      → feishu.py:392  urlopen(GitCode 协作者分页)               ← 10s
```

async 代码运行在 uvicorn 单线程事件循环上，同步 HTTP 直接冻结整个 loop。

### 2.2 后果

1. **一个用户卡死全平台**：100+ Bot 共享同一 loop，任一 `/bind` 阻塞期间所有 Bot 消息处理 + 所有 HTTP API 冻结
2. **WS 心跳超时掉线**：`ping_interval=20s, ping_timeout=30s`，阻塞超 50s 即断开
3. **重连丢消息**：指数退避 5s→300s，重连窗口内消息无缓冲直接丢
4. **并发槽占满丢消息**：`Semaphore(10)` 槽被阻塞任务占满，后续消息 `concurrency limit reached, dropping message`
5. **重连风暴**：越卡越掉、越掉越卡，恶性循环
6. **单次卡顿不可控**：分页循环无总量上限，数百人群 + 大协作者数可达分钟级

---

## 三、整改目标

```
handler_async.py（纯 async，零同步 HTTP）
  │
  ├─ 纯逻辑（无 IO）：detect_bind_intent / detect_clean_intent / resolve_repo（SQLite 快查询）
  ├─ IO 型命令：process_command_async 内 await api.xxx_async()
  │     ├─ /bind    → await api.sync_repo_feishu_mapping(ctx, repo_key, chat_id)
  │     ├─ /refresh → await api.sync_repo_feishu_mapping(ctx, ...)
  │     └─ 其余命令 → 纯 DB（同步但快，可保留）
  └─ token：一律走 ctx.token（BotContext 每 Bot 缓存）
```

---

## 四、整改方案

### Phase 1 — 消除 async 事件循环阻塞（最高优先）

**现状引用**：`handler_async.py:16-19`

```python
from src.im.feishu import (
    resolve_repo, process_command, detect_bind_intent, detect_clean_intent,
    get_chat_members, sync_repo_feishu_mapping,
)
```

**改动**：

1. **`FeishuAsync` 新增异步方法**（`feishu_async.py`，参照已有 `get_chat_members` 的 `BotContext` 模式）：

   ```python
   async def get_chat_members(self, ctx: BotContext, chat_id: str) -> list[dict]:
       """异步拉取群成员，使用 ctx.token，分页累加。"""
       token = await self._ensure_token(ctx)
       # GET /im/v1/chats/{chat_id}/members?member_id_type=open_id&page_size=100&page_token=...
       # 循环 page_token，返回 [{name, open_id}]

   async def sync_repo_feishu_mapping(self, ctx: BotContext, repo_key: str, chat_id: str) -> int:
       """异步同步群成员 ↔ GitCode 协作者映射。
       - members = await self.get_chat_members(ctx, chat_id)
       - api_token = get_bot_token_by_repo(owner, repo) or 全局兜底（同步 DB/文件读取，快）
       - GitCode 协作者分页用 aiohttp（或同步 urllib 亦可接受，因走 run_in_executor）
       - matched = 写 unified_user_mapping
       """
   ```

2. **`handler_async.py` 新增 `process_command_async`**：复制 `feishu.py:process_command` 逻辑，但 `/bind` 与 `/refresh` 分支的 `sync_repo_feishu_mapping(...)` 改为 `await api.sync_repo_feishu_mapping(ctx, ...)`；其余命令（`/unbind`、`/status`、`/autodev`、`/notify`、`/clean`、`/help`）保持同步 DB 逻辑（SQLite 查询毫秒级，不阻塞）。

3. **替换调用点**（`handler_async.py:117/123/175`）：
   ```python
   if text.startswith("/"):
       result = await process_command_async(ctx, api, text, chat_id, parsed.get("sender_id", ""), db, account)
   ```
   `detect_clean_intent` 分支同样改调 `await process_command_async(ctx, api, "/clean", ...)`。

**不阻塞验证点**：`handler_async.py` 中不再出现任何同步 `urlopen` / `requests` / `get_tenant_token()`。

### Phase 2 — 统一 token 单一来源

- `handler_async.py` 所有飞书 API 调用已走 `await api.*`（内部 `ctx.token`）
- 删除 `feishu.py` 中：`_TOKEN`、`_TOKEN_EXPIRE`、`_TOKEN_LOCK`、`_TOKEN_CACHE`、`get_tenant_token()`、`get_tenant_token_from()`
- 确认 `vod_handler_async.py` 无全局 token 引用（已走 `api`）
- `feishu.py` 若仍被 `tasks.py` / `settings.py` 等引用 token，则一并迁移

### Phase 3 — 收敛 lark-oapi 帧解析依赖

**现状**：`feishu_async.py:585-586` 在 `run_bot` 内局部 import：

```python
from lark_oapi.ws.pb.pbbp2_pb2 import Frame
from lark_oapi.ws.enum import FrameType, MessageType
```

**方案 A（推荐，改动最小）**：移为文件顶部显式 import，并如实更新模块 docstring（「传输层用 aiohttp/websockets，协议帧解析复用 lark-oapi 的 protobuf 定义」），`requirements.txt` 注明 lark-oapi 为协议帧依赖。

**方案 B（彻底）**：手写 `Frame` protobuf 帧解析（需编译或手写字节解析），完全移除 lark-oapi。工作量大、易错，不推荐。

### Phase 4 — 删除死代码

删除（已核实无生产引用）：

- `src/im/feishu_bot.py`（198 行）
- `src/im/handler.py`（246 行）
- `src/im/vod_handler.py`（446 行）
- `feishu.py` 中：`start_ws`、`start_vod_ws_async`、`_handle_vod`、`stop_ws`、`parse_event_data`、`_ws_clients`、`_build_card_payload`、`reply_text`、`reply_card`、`send_card_to_user`、`update_message`、`add_reaction`、`get_user_name`、`get_chat_members`（若 Phase 1 已异步化）、`get_tenant_token*`

**保留** `feishu.py` 中纯逻辑函数（无 IO 或 IO 仅 SQLite）：`detect_bind_intent`、`detect_clean_intent`、`resolve_repo`、`process_command` 的业务分支（若未完全迁移）——或整体迁入 `handler_async.py` 后删除 `feishu.py`（更彻底，见决策点）。

### Phase 5 — 测试对齐生产

| 测试文件 | 现状 | 整改后 |
|---------|------|--------|
| `test_chat_handler.py` | 测旧 `handler.py`（8 用例） | 迁移到 `handler_async.py` 的 `process_command_async` |
| `test_feishu_handler.py` | 测旧 `feishu.py` token/reply | 重写为测 `FeishuAsync` 方法（mock aiohttp） |
| `test_p2p_bind.py` | 已测 `process_chat_async` ✅ | 保留 |
| `test_bot_manager_load.py` | 已测 `BotManager.load_from_db` ✅ | 保留 |
| `test_im_async_remediation.py` | **新增**（本方案配套） | 见「五、测试计划」 |

### Phase 6 — 依赖与文档

- `requirements.txt`：如选方案 A 注明 lark-oapi 用途；如 B 则移除
- 更新 `docs/new-version/im-ws-async-redesign.md` 与 `docs/new-version/im-ws-multi-boot-fix.md` 引用
- 全仓 `grep` 确认无残留 `from src.im.feishu import start_ws`、`handler.process_chat`、`vod_handler`、`feishu_bot` 引用

---

## 五、测试计划

> 配套测试文件：`tests/test_im_async_remediation.py`（本方案交付，当前以 skip 标记，整改实施后移除 skip 即可运行验证）。

### 5.1 目标态接口（整改后应存在）

| 接口 | 位置 | 说明 |
|------|------|------|
| `FeishuAsync.get_chat_members(ctx, chat_id)` | feishu_async.py | 异步群成员分页，用 ctx.token |
| `FeishuAsync.sync_repo_feishu_mapping(ctx, repo_key, chat_id)` | feishu_async.py | 异步同步映射 |
| `handler_async.process_command_async(ctx, api, text, chat_id, sender_id, db, account)` | handler_async.py | 异步命令处理 |

### 5.2 用例清单

#### A. `FeishuAsync.get_chat_members`

| 用例 | 场景 | 断言 |
|------|------|------|
| A1 | 单页返回成员 | 返回 `[{name, open_id}]`，调用一次 API |
| A2 | 分页：`has_more=true` 再翻页 | 两页成员拼接，page_token 传入第二次请求 |
| A3 | 无 token（`_ensure_token` 返回空） | 返回 `[]`，不发起请求 |
| A4 | API 抛异常 | 返回 `[]`，不抛异常（异常隔离） |
| A5 | 请求使用 `ctx.token` | mock session 断言 Authorization 头含 ctx.token |

#### B. `FeishuAsync.sync_repo_feishu_mapping`

| 用例 | 场景 | 断言 |
|------|------|------|
| B1 | 群成员昵称匹配 GitCode 协作者 | 写入 `unified_user_mapping`，返回匹配数 |
| B2 | 无群成员 | 返回 0，不写库 |
| B3 | 无 bot token（get_bot_token_by_repo + 全局兜底均为空） | 返回 0，日志 warning |
| B4 | 协作者分页返回空列表 | 匹配 0，不抛异常 |
| B5 | 昵称匹配时写入 gitcode_nickname | mapping 行含正确 nickname |

#### C. `handler_async.process_command_async`

| 用例 | 场景 | 断言 |
|------|------|------|
| C1 | `/bind owner/repo` | 写 `repo_im_group_binding`，调用 `api.sync_repo_feishu_mapping`，回复含「已绑定」 |
| C2 | `/bind` 且映射匹配数>0 | 回复含「同步了 N 个用户映射」 |
| C3 | `/unbind` | 删除 binding 行，回复「已解绑」 |
| C4 | `/status` 已绑定 | 回复含 repo_key |
| C5 | `/status` 未绑定 | 回复「未绑定任何仓库」 |
| C6 | `/autodev on` | `repo_im_group_binding.auto_impl_enabled=1`，回复「已开启」 |
| C7 | `/notify off` | `notify_enabled=0`，回复「已关闭」 |
| C8 | `/clean` | 清空 `chat_agent_session.backend_session_id`，回复「已清除」 |
| C9 | `/help` | 回复含命令列表 |
| C10 | 未知命令 | 返回空串（不回复） |
| C11 | **阻塞回归**：命令路径不调用任何 `urlopen`/`requests`/全局 `get_tenant_token()` | patch `src.im.feishu.urlopen` 断言未调用；patch `get_tenant_token` 断言未调用 |

#### D. 纯逻辑迁移回归

| 用例 | 场景 | 断言 |
|------|------|------|
| D1 | `detect_bind_intent("绑定到 owner/repo")` | 返回 `owner/repo` |
| D2 | `detect_bind_intent("查一下状态")` | 返回空 |
| D3 | `detect_clean_intent("清除会话缓存")` | True |
| D4 | `detect_clean_intent("你好")` | False |
| D5 | `resolve_repo("现在如何", bound_chat, account)` | 返回 DB 中绑定 repo_key |
| D6 | `resolve_repo("owner/repo 怎么样", ...)` | 返回 `owner/repo` |

---

## 六、验收标准

1. `handler_async.py` / `feishu_async.py` 中 grep 无同步 `urlopen` / `requests.get` / `get_tenant_token()`
2. 所有飞书 API 调用走 `api`（内部 `ctx.token`），无全局 token
3. `test_im_async_remediation.py` 全部用例通过（A1-A5 / B1-B5 / C1-C11 / D1-D6）
4. 现有 `test_p2p_bind.py` / `test_bot_manager_load.py` 不回归
5. 死代码删除后 `pytest tests/` 全绿、无 ImportError
6. `docs/new-version/im-ws-async-redesign.md` 引用更新

---

## 七、风险与回滚

| 风险 | 缓解 |
|------|------|
| `sync_repo_feishu_mapping_async` 行为与旧版不一致 | 以旧 `feishu.py:360-418` 为基准逐行对照，B1-B5 用例锁定 |
| 删除死代码时误删仍被引用函数 | Phase 4 前全仓 `grep`；删除后立即 `pytest tests/` |
| `/bind` 大群映射耗时仍长 | 异步化后不阻塞 loop；如需进一步优化可加超时/限制，非本方案范围 |
| 方案 B（去 lark-oapi）协议解析风险 | 默认方案 A，B 仅作备选 |

---

## 八、决策点（实施前确认）

1. **Phase 3 方案 A（推荐）还是 B？**
2. **纯逻辑函数去留**：保留 `feishu.py`（收敛为纯逻辑工具）还是彻底删除 `feishu.py`、将 `detect_*`/`resolve_repo` 迁入 `handler_async.py`？
3. **实施范围**：一次性 Phase 1-6，还是先 Phase 1+5 止血、后续迭代？
