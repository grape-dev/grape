# 生产「进入任务会话」报错——飞书 awaiting 卡片点击分析

> 状态：分析完成，**观测日志已加**（待生产复测确认根因，2026-10-14）｜环境：单机生产 `<PROD_HOST>`（DEPLOYMENT-REMOTE.md，weekly-07-lyz @ a0fc6232f）
> 现象：飞书 awaiting-input 通知卡点击「进入任务会话」→ 报错「❌ 进入任务会话失败 / 该会话可能不属于当前账号。」
> 观测：`handler_async.py /task-open` 已加 `[task-open] entry` 与 `[task-open] join_result` 日志（uid/app/sender_open_id/conv_id/conv_user/open_id_to_user）；`join_feishu_chat` deny 分支已加 `join_feishu_chat deny` 日志。部署后重试点卡即可从日志确认 uid/conv_user/open_id 是否一致。

---

## 1. 报错代码点

唯一来源：`src/im/handler_async.py:484`（`/task-open` 分支）：
```python
if cid and join_feishu_chat(chat_id, cid, uid):
    ... "✅ 已进入任务会话" ...
else:
    ... "❌ 进入任务会话失败\n该会话可能不属于当前账号。" ...
```

`join_feishu_chat`（`src/session_manager.py:673`）唯一 False 条件：
```python
SELECT user_id FROM chat_conversations WHERE id=?
if not row or row[0] != user_id: return False
```
→ 即 **`chat_conversations.user_id != uid`**（或 conv 不存在）。

## 2. 数据完整性核查（生产实测，均自洽）

| 项 | 值 | 说明 |
|----|-----|------|
| issue <ID>（<REPO> #N，awaiting_input）| `user_id=<UID>`、`assignee=<OWNER>` | issuing owner |
| conversation `<CONV_ID>…` | `user_id=<UID>`, conv_type=task, issue_number=14 | awaiting 卡片携带 |
| `chat_agent_session sess-cfg-7` | `user_id=<UID>`, `im_app_id=<APP_ID>` | user X 本人 chat bot |
| `get_user_id_by_app_id(<APP_ID>)`（实机执行）| **7** | handler 点击 uid |
| 卡片 value | `conv_id=<CONV_ID>…`（含）| `_build_notify_actions` 实测 |
| awaiting 通知日志（08:41:50）| user=X, bot=<APP_ID>…, open_id=<OPEN_ID>…, conv=<CONV_ID>… | 发送正常 |

**按当前代码与数据，`join_feishu_chat(chat_id, <CONV_ID>, <UID>)` 应返回 True**，不应报错。

## 3. 矛盾与候选原因（按可能性排序）

### 候选 A（高）：点击者账号 ≠ conv.owner（多租户/账号切换）
- 通知发给了 `<OPEN_ID>…`（user X <NICKNAME>），但**点击者若已切换到其它 gitcode 账号**，其飞书点卡回调**仍走同一 bot app**（`<APP_ID>…`），handler 用 **`get_user_id_by_app_id(app_id)` 反查**（**不是 open_id→user**）。
- 单机一 bot 对应一 user 时反查恒定 7；但若 `chat_agent_session` 存在**该 app 的其它行**（如该 app 也绑了用户 B）或**点击者用另一 bot/app 发的历史卡**，则 `uid≠7` → 不匹配报错。
- ⚠️ 缺陷点：`/task-open` 未用 `sender_open_id`（卡片实际收件人）反查，而是用 `app_id`——**开卡 app_id ≠ 点卡人身份**时必然误判。

### 候选 B（中）：卡片是历史/其它状态卡
- 若用户点的是更早发出的**旧卡**（如 pending_review / task 卡，conv_id 指向别的 conv 或为空），且 conv.owner 与当前点卡身份不同 → 同上报错。

### 候选 C（低）：并发竞态
- `join_feishu_chat` 的 UPDATE（清旧设新）在 db_lock 内原子；但若卡片点击瞬间 issue 被重新分配/conv 被删除，`SELECT` 返回 None → False。（本次 conv 存在，排除。）

### 候选 D（极低）：多 worker 进程 DB 一致性
- `src/api/run.py` 3 进程（父 + main-api:3000 + webhook:8080）**共享同一 SQLite**。飞书 IM 在 main-api，DB 连接共享文件语义；`chat_agent_session`/`chat_conversations` 均在 main-api 内读，无跨进程不同步问题（SQLite WAL 除外）——基本排除。

## 4. 结论一句话（待观测确认）

> 生产数据全部自洽，按代码 `join_feishu_chat` 应通过；报错最可能是 **`/task-open` 用 `app_id` 而非 `sender_open_id` 反查用户**，当点卡账号不等于卡片 owner（或命中多账号共用 bot）时 `uid != conv.user_id` → 误报「不属于当前账号」。

## 5. 验证/修复建议

### 5.1 加观测日志（最优先，先确认真实 uid/conv/open_id）
在 `/task-open` 分支加日志：
```
log.info(f"[task-open] uid={uid} conv_id={cid} chat={chat_id[:20]} app={ctx.app_id} "
         f"sender_open_id={sender_open_id[:20]} -> join={join_feishu_chat(...)}")
```
并在 `join_feishu_chat` False 处打印 `conv_user_id、uid`。

### 5.2 修复（若确认候选 A）：改用 sender_open_id 反查用户
- 卡片开卡时可直接在 value 带 `user_id`（通知侧已知 `it["user_id"]`）→ 点击时以卡片身份为准（更准：通知发给谁，谁可进入）；
- 或：`join_feishu_chat` 宽容——若 `conv.user_id==0` 视为可进入；若 conv.user 与 uid 不匹配，回退按 `sender_open_id` 校验 unified_user_mapping 的 feishu_open_id == conv.user 的 open_id，匹配则放行。
- 避免误伤：仍保持「横切」安全——仅当 open_id 能映射到 conv.user 才放行。

### 5.3 观察
- 部署后让用户重试点卡，确认日志中 `uid=<UID> / conv_user=<UID> / sender_open_id=<OPEN_ID>…` 是否一致。

## 6. 落档
- 2026-10-14：awaiting 卡片发送正常、数据自洽；点卡进入报错根因指向 `/task-open` 用户归属判定（app_id 反查 vs open_id 收件人）——待按 §5.1 加观测日志后最终确认并修复。