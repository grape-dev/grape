# Chat Agent 全链路设计整合（Web + 飞书统一会话与工作目录）

> 版本：v1.0
> 日期：2026-08-31
> 定位：将 `docs/new-version3` 下关于 **Chat Agent（Web + 飞书 IM）** 的系列设计/修复文档整合为一份总纲，澄清问题脉络、设计原则、各子文档关系与统一验证口径。
> 目标读者：新接入的开发者、排障的运维、后续改动 Chat 链路的工程师。

---

## 1. 文档地图（系列关系）

本主题由多次演进形成，按时间/依赖排列：

| 文档 | 主题 | 状态 |
|---|---|---|
| [`im-bot-async-connection-analysis.md`](./im-bot-async-connection-analysis.md) | IM Bot WebSocket 异步连接机制分析与官方 SDK 对比（背景/机制） | ✅ 已实施（既有机制说明） |
| [`web-chat-no-response-cwd-fix.md`](./web-chat-no-response-cwd-fix.md) | **Web Chat 无回应根因**：cwd 与会话目录不一致 → 统一工作区基准（方案 A 已上线） | ✅ 已实施 |
| [`feishu-chat-reuse-web-session.md`](./feishu-chat-reuse-web-session.md) | **飞书会话复用**：飞书改用 conversation 的 runtime_session_id（方案文档） | ✅ 已实施 |
| [`web-to-feishu-push-cross-loop-fix.md`](./web-to-feishu-push-cross-loop-fix.md) | **Web→飞书推送跨循环 aiohttp 修复**（主 loop 投递） | ✅ 已实施 |
| [`poll-webhook-port-bug-fix-plan.md`](./poll-webhook-port-bug-fix-plan.md) | GitCode issue poll 注入端口 bug（webhook fallback） | ✅ 已实施（上游事件链路，与 Chat 相关但独立） |

> 阅读顺序建议：先 `im-bot-async-connection-analysis` 理解机制，再依次读 web-cwd → feishu-session → push-cross-loop。

---

## 2. 统一设计原则

> **同一对话（conversation）= 同一 opencode session = 同一工作目录。**

这是本次全链路修复的**核心不变式**，贯穿 Web 与飞书：

```
同一 conversation（chat_conversations.id）
  ├── session_id（runtime_session_id）  ← 同一 opencode 会话（历史上下文）
  ├── cwd 集合与目录（WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}）
  └── 双方共享：Web 发消息 / 飞书发消息 → 同一 session + 同一 cwd → 历史可恢复 → 有实质回复
```

- **web 侧**：`runtime_session_id = conv.runtime_session_id`，cwd = `_get_chat_cwd(uid, repo_key, conv_id)` → 天然一致。
- **飞书侧**：本已复用同一 conversation（`get_conversation_by_im`），本次补齐 **session 也用 conversation 的 runtime_session_id**（`_resolve_oc_sid`），不再兜底 repo 级旧会话。
- 这样任一端发消息都能在上一条历史之上续聊，且回复推送到另一端（Web→飞书卡片）正常。

---

## 3. 关键机制分层

### 3.1 连接与消息接收（`im-bot-async-connection-analysis`）
- 飞书走 WebSocket 双工长连接（手动 aiohttp/websockets，复用 lark-oapi protobuf 帧）。
- 与官方 SDK 对比：协议同构，但手写实现若在空闲重连/会话目录上偏离会引发问题；本主题聚焦**会话/目录一致**，连接层为背景。

### 3.2 Web Chat 工作目录统一（`web-chat-no-response-cwd-fix`）
- 路径：`WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`（owner/repo 两级，`parse_repo_key` 解析）。
- 根因：旧 `CHAT_CWD_BASE=~/grape_workspace` 与 opencode session.directory（`/home/grape/...`）不一致 → `--session` 恢复历史失败 → tokens=0 → 无输出。
- 修复：`_get_chat_cwd` 用 `WORKSPACE_DIR` 基准 + 存量 session.directory 一次性迁移（`scripts/migrate_chat_cwd.py`）。

### 3.3 飞书复用同一会话（`feishu-chat-reuse-web-session`）
- 根因：飞书 exec 用 repo 级 `chat_agent_session.runtime_session_id`（旧 `ses_fdcb68`），cwd 却为 conv 级 → 目录错位 → 空回复 `?`。
- 修复：`_resolve_oc_sid(conv, repo_oc_sid)`：
  - conv.runtime_session_id 存在 → 复用（与 web 同源）；
  - conv 无 → **返回空让 opencode 在 conv cwd 下新建**（不再兜底 repo 级），执行后写回 `update_conversation(runtime_session_id=...)`。
- 存量：清掉被污染的 conv session（目录不匹配的），下次自动新建。

### 3.4 Web→飞书推送主循环投递（`web-to-feishu-push-cross-loop-fix`）
- 根因：`agent_fn` 在后台线程 `asyncio.run()` 新建 loop，复用主 loop 的共享 aiohttp session → `Timeout context manager should be used inside a task` → 推送失败。
- 修复：`BotManager.start` 记录 `self.loop`；`_dispatch_push_to_main_loop` 用 `asyncio.run_coroutine_threadsafe` 投递回主 loop（fire-and-forget），兜底 `asyncio.run`。
- 涉及 `chat.py` 三处 `_push_reply_to_feishu` 调用点（direct agent / remote agent / poll）。

### 3.5 上游事件兜底（`poll-webhook-port-bug-fix`）
- 独立于 Chat 会话，但保障 GitCode issue 事件不丢：poll 注入改用权威 `get_webhook_url()`，修复 `38080` 死端口。

---

## 4. 目录/会话/推送三者协同（交互示意）

```
用户 Web Chat 发消息
  → chat_submit → ChatTaskManager（后台线程执行 agent）
      → cwd = _get_chat_cwD{uid,repo_key,conv_id}   ← 统一目录
      → session = conv.runtime_session_id               ← 复用 / 新建
      → opencode 在本 cwd 下跑 --session（历史恢复）
  → 完成后：
      - 落 DB chat_messages（generate_status=2）
      - SSE 回前端
      - _dispatch_push_to_main_loop(_push_reply_to_feishu)   ← 主 loop 投递
        → 若该 conv 被飞书接入（im_channel_id 非空）→ send_card_to_user 推送

用户飞书 IM 发消息
  → process_chat_async → 同一 conversation（get_conversation_by_im）
      → cwd = 同 conv 目录；session = _resolve_oc_sid(conv)   ← 与 web 同源
      → 在相同历史上下文回复
```

---

## 5. 统一验证与测试

| 关注点 | 测试文件 | 断言 |
|---|---|---|
| web cwd 结构 | `tests/test_chat_cwd.py` | 路径含 owner/repo 两级、目录创建 |
| web/飞书 cwd+session 契约 | `tests/test_chat_cwd_contract.py` | 同 conv 端到端一致、迁移幂等 |
| 飞书 session 复用 | `tests/test_feishu_session_unify.py` | conv 优先 / 无则新建 / 写回 |
| 推送主 loop 调度 | `tests/test_web_feishu_push.py` | 有主 loop 用 threadSafe / 兜底 asyncio.run / 守卫分支 |

运行级回归脚本（人工）：
- `scripts/verify_chat_cwd.sh`
- `scripts/verify_feishu_web_conv.sh`
- `scripts/verify_web_feishu_push.sh`

生产验证要点：web 发消息有回复、飞书同 conv 有回复、web 完成推送到飞书卡片成功（日志无 `send_card_to_user failed`）。

---

## 6. 涉及代码文件一览

| 文件 | 关键符号 | 角色 |
|---|---|---|
| `src/config.py` | `WORKSPACE_DIR` / `CHAT_CWD_BASE` | 工作区基准（web 统一到 WORKSPACE_DIR） |
| `src/api/routes/chat.py` | `_get_chat_cwd` / `_push_reply_to_feishu` / `_dispatch_push_to_main_loop` / `agent_fn` | web chat 目录、推送投递 |
| `src/api/chat_task_manager.py` | `submit` / `_ run` | 后台线程执行 agent |
| `src/im/handler_async.py` | `_ resolve_oc_sid` / `_ process_chat_inner_async` / `update_conversation` | 飞书会话复用 |
| `src/im/feishu_async.py` | `BotManager.start`（self.loop）/ `send_card_to_user` | 主 loop 记录与飞书 API |
| `scripts/migrate_chat_cwd.py` | 一次性迁移 | 存量 session.directory 对齐 |
| `scripts/verify_*.sh` | 回归脚本 | 人工运行级验证 |

---

## 7. 运维/排障速查

1. Web Chat 无回应 → 查日志 `opencode cwd` 与 `--session` 是否同 conv 目录；不通则看 `_get_chat_cwd` 是否用对 repo_key（parse_repo_key）。
2. 飞书无实质回复（reply 空 `?`）→ 确认 `conversation.runtime_session_id` 是否为空/是否能新建；必要时清掉被污染的 conv session。
3. web 完成但飞书收不到 → 查 `send_card_to_user failed / Timeout context`；若出现说明推送走了后台线程跨 loop，检查 `BotManageer.loop` 是否设置、`_dispatch_push_to_main_loop` 是否被调用。
4. GitCode 事件漏 → 查 `poll: inject failed`，确认 webhook 走 `get_webhook_url` 正确地址。

---

## 附：文档存放约定

- 本整合文档：`docs/new-version3/chat-agent-feishu-web-unified-design.md`
- 子文档保持独立（便于单点修改），总纲提供索引与决策记录。