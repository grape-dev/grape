# 个人任务中心 + 任务通知接口 + Pipeline 责任人改造 设计方案

> 目标：
> 1. **任务中心（Task Center）**：每个用户可在 sidebar Account 分组下查看自己待处理/已完成的任务。
> 2. **个人任务通知接口**：issue agent 分配负责人后调用，devops-bot 创建任务、通过该用户的 chat-agent + 飞书私聊通知。
> 3. **Pipeline 责任人**改为 Issue 的 assignee（issue agent 分配决定，无需查询）。
> 4. **任务生命周期**：任务结束 = Issue 关闭 或 Issue 更换责任人（由 issue agent 感知并刷新）。
> 状态：设计定稿（待实施）

---

## 一、背景与问题

### 1.1 现状问题

1. **Pipeline 责任人判定不准确**（`src/api/routes/pipeline.py:63-77`）：依赖 `events.actor` 推断，不是 GitCode 真实 assignee；A2A 事件 actor 为空 → 责任人不显示。
2. **缺少任务分派机制**：issue agent 分配负责人后，责任人本人无感知，也没有"我的待办任务"视图。
3. **无任务生命周期**：Issue 关闭 / 换人后，任务无自动结束机制。

### 1.2 目标

1. 新增 **`tasks` 表**，记录每个用户的待处理任务（含 user_id 关联）。
2. 新增 **个人任务通知接口**：issue agent 分配/换人 → 建任务 + 私聊通知 + chat-agent 协同。
3. 新增 **任务中心页面**（sidebar Account 分组），展示当前用户的任务，含已完成历史（回溯定位）。
4. **Pipeline 责任人 = Issue assignee**（飞书昵称显示），assignee 变更则跟随。
5. **任务结束**：Issue 关闭（`issue_closed`）或更换责任人（`reassigned`），由 issue agent 感知并刷新。

---

## 二、关键设计决策（已确认）

| # | 决策 | 说明 |
|---|------|------|
| D1 | **assignee 不查询** | 分配哪个人由 issue skill 决定；分配后直接调 notify 接口 |
| D2 | **先创建 Web conversation** | notify 首先创建 Web 端 `chat_conversations`（Chat Agent 页可见，`create_conversation` 不带 im_channel_id），作为任务的主交互会话 |
| D3 | **飞书仅作通知** | 飞书只推送「你有新任务」通知卡片；后续沟通在 Web Chat Agent 页该 conversation 中进行（不依赖飞书私聊双向） |
| D4 | **触发方式与 A2A 一致** | skill 内 curl 调用 `POST /api/tasks/notify` / `POST /api/tasks/close` |
| D5 | **notify 立即返回** | 接口起 `asyncio.create_task` 后台执行 chat-agent，请求立即返回 |
| D6 | **独立入口** | 新增 `src/api/routes/tasks.py` |
| D7 | **Pipeline 责任人显示飞书昵称** | 优先 `unified_user_mapping.feishu_name`，缺失回退 GitCode 用户名 |
| D8 | **issue 上下文取自 events 表** | 从最新事件 payload 取标题/正文 + `get_issue_summary`，零额外 GitCode API 调用 |
| T1 | **tasks 表含 user_id** | notify 创建时按 assignee(GitCode username) → 查 `unified_user_mapping.user_id` 关联 |
| T2 | **Issue 关闭/换人由 issue agent 刷新** | issue agent 感知关闭/换人后调 `tasks/close` / `tasks/notify`；webhook 管线不做自动同步 |
| T3 | **任务中心按当前用户过滤** | `GET /api/tasks/mine` 用 `tasks.user_id = get_current_user().id` |
| T4 | **保留已完成 tab** | 全部保留，标注完成原因（issue_closed / reassigned），供回溯定位 |
| T5 | **「去对话」打开关联会话** | 任务卡片点击后跳转 Chat Agent 并自动打开该任务 conversation |

---

## 三、执行方式：阻塞分析

两种候选执行方式（notify 接口内跑 chat-agent）：

| 维度 | 方式1：`asyncio.create_task` + `consume_stream_async` | 方式2：`threading.Thread` + 同步 `stream_agent` |
|------|----------------------------------------|-------------------------------|
| 阻塞事件循环 | ❌ 不阻塞（agent 在线程池线程） | ❌ 不阻塞（agent 在线程） |
| 完成后推飞书 | ✅ task 内自然 `await` | ⚠️ 需 `run_coroutine_threadsafe` 桥接 |
| 代码复杂度 | 低（与 `handler_async.py` 同构） | 中（跨线程桥接） |
| 并发上限 | 默认线程池（约 32） | 自建线程无上限 |

**结论（D5）**：两种都不阻塞事件循环。`consume_stream_async`（`src/agent/executor.py:296`）内部已用 `loop.run_in_executor` 把阻塞的 `stream_agent` 丢进默认线程池，事件循环只 `await q.get()`。选**方式 1**：更简洁，且 `send_card_to_user` 是 async 方法，在 task 内自然 await。

---

## 四、数据模型：`tasks` 表

### 4.1 建表 SQL（`db_setup.py`）

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,       -- ← 关联 user_accounts.id（T1，创建时解析）
    assignee         TEXT NOT NULL,          -- GitCode 用户名（展示用）
    repo_key         TEXT NOT NULL,          -- owner/repo
    issue_number     INTEGER NOT NULL,       -- Issue 编号
    title            TEXT DEFAULT '',        -- Issue 标题快照
    task_description TEXT DEFAULT '',        -- 任务描述
    conversation_id  TEXT DEFAULT '',        -- 关联私聊对话（chat_conversations.id）
    status           TEXT DEFAULT 'active',  -- active / completed
    completed_reason TEXT DEFAULT '',        -- issue_closed / reassigned
    created_at       TEXT DEFAULT (datetime('now')),
    completed_at     TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_repo_issue ON tasks(repo_key, issue_number);
```

- **唯一性**：不设 UNIQUE；notify 时若「同 repo+issue+assignee 已有 active 任务」则跳过。已完成任务可保留多条历史（T4 回溯）。

---

## 五、接口设计（`src/api/routes/tasks.py`）

| 接口 | 用途 |
|------|------|
| `POST /api/tasks/notify` | issue agent 分配/换人后调用：建任务 + 换人刷新 + 私聊通知 + 后台 chat-agent |
| `POST /api/tasks/close` | issue agent 关闭 Issue 后调用：该 issue 全部 active 任务 → completed（issue_closed） |
| `GET /api/tasks/mine?status=active\|completed` | 任务中心：当前登录用户的任务列表 |

### 5.1 `POST /api/tasks/notify`

```
POST /api/tasks/notify
Content-Type: application/json
```

**请求体**：

```json
{
  "assignee": "younger_eric",
  "repo_key": "org/repo",
  "issue_number": 5,
  "task_description": "请处理该 Issue 的 xxx 问题"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `assignee` | ✅ | 责任人 GitCode 用户名（由 issue skill 决定） |
| `repo_key` | ✅ | 仓库 `owner/repo` |
| `issue_number` | ✅ | Issue 编号 |
| `task_description` | ✅ | 任务描述（issue agent 写的） |

**成功响应（立即返回）**：

```json
{
  "ok": true,
  "task_id": 123,
  "conversation_id": "uuid-xxx",
  "message": "任务已通知责任人，chat-agent 正在处理"
}
```

**失败响应**：

```json
{ "ok": false, "code": 400/404, "message": "..." }
```

### 5.2 `POST /api/tasks/close`

```json
{ "repo_key": "org/repo", "issue_number": 5 }
```

- 将该 issue 下所有 `status='active'` 任务 → `completed`，`completed_reason='issue_closed'`

### 5.3 `GET /api/tasks/mine`

```
GET /api/tasks/mine?status=active|completed
Authorization: Bearer <token>
```

- `get_current_user` 取 user.id → `WHERE user_id=? [AND status=?] ORDER BY created_at DESC`

---

## 六、处理流程

### 6.1 notify 流程

```
POST /api/tasks/notify（FastAPI async 端点）
  │
  ├─ 1. 校验
  │      ├─ assignee / repo_key / issue_number / task_description 必填 → 400
  │      └─ repo 已托管（managed_repos 有记录）→ 否则 404
  │
  ├─ 2. 解析 user_id（T1）
  │      user_id = unified_user_mapping.user_id（WHERE gitcode_user=assignee）
  │      └─ 无 user_id → 返回 {ok:false, message:"责任人未注册平台，无法创建任务"}
  │
  ├─ 3. 任务刷新（换人处理，T2）
  │      UPDATE tasks SET status='completed', completed_reason='reassigned', completed_at=datetime('now')
  │        WHERE repo_key=? AND issue_number=? AND status='active' AND assignee<>?
  │      若同 assignee 已有 active 任务 → 复用（不重复创建）
  │      否则 → INSERT tasks（user_id, assignee, repo_key, issue_number, task_description, status='active'）
  │
  ├─ 4. 创建/复用 Web conversation（D2，主交互会话）
  │      create_conversation(user_id, repo_key, title=f"Issue #{n}")   # session_manager.py:362，不带 im_channel_id
  │      回写 tasks.conversation_id
  │
  ├─ 5. 查用户 chat-agent 配置
  │      cfg = get_chat_config(user_id)             # session_manager.py:475（backend/skill/im 配置）
  │
  ├─ 6. 后台执行 chat-agent（D5 方式1）
  │      asyncio.create_task(_run_notify(...))      # 立即返回
  │      │
  │      │  _run_notify:
  │      │    1) 构建 prompt：
  │      │         build_chat_prompt + events 最新事件 payload 的 issue 标题/正文
  │      │         + get_issue_summary(repo_key) + task_description
  │      │    2) async for evt in consume_stream_async(prompt, cwd, session_id,
  │      │            backend_name=cfg.backend, user_token, ...)   # executor.py:296
  │      │    3) 收集 final_text
  │      │    4) add_message(conv_id, "user", task_description)     # 记录用户侧任务
  │      │       add_message(conv_id, "agent", final_text)          # 记录 agent 回复
  │      │    5) 飞书通知（D3，仅通知）：查 open_id → send_card_to_user(ctx, open_id, 任务通知文案)
  │      │       └─ 无 open_id → 跳过通知（任务/对话已创建，不阻断）
  │      └─ 异常 → log.warning，不影响主流程
  │
  └─ 7. 返回 {ok:true, task_id, conversation_id}
```

### 6.2 close 流程

```
POST /api/tasks/close（FastAPI 端点）
  → 校验 repo_key / issue_number 必填
  → UPDATE tasks SET status='completed', completed_reason='issue_closed', completed_at=datetime('now')
      WHERE repo_key=? AND issue_number=? AND status='active'
  → 返回 {ok:true, closed: n}
```

### 6.3 BotContext / api 获取

```python
from src.api import main as api_main
bm = api_main.bot_manager
# 取任一 chat_bot 类型的 BotContext + api
ctx = next((b for b in bm.get_all_bots().values() if b.bot_type == "chat_bot"), None)
api = bm.api   # FeishuAsync 实例
```

`send_card_to_user(ctx, open_id, text)`（`feishu_async.py:274`）主动推送通知卡片给用户（D3：飞书仅作通知，非主交互）。

### 6.4 后续用户回复如何命中同一会话

notify 创建的是 **Web conversation**（`create_conversation` 不带 `im_channel_id`），用户在 **Chat Agent 页面**的对话列表中选择该会话即可继续沟通（T5「去对话」直接打开）。飞书通知卡片仅提示"有新任务"，不承载双向对话。

---

## 七、任务中心页面（前端）

### 7.1 侧边栏（`index.html`，Account 分组）

```html
<div class="grp">Account</div>
<a id="nav-chat" onclick="app.nav('chat')"><span class="ic">☎</span>Chat Agent</a>
<a id="nav-tasks" onclick="app.nav('tasks')"><span class="ic">📋</span>任务中心</a>
```

### 7.2 页面原型（`static/js/pages/tasks.js`）

```
┌─ 任务中心 ────────────────────────────────────────┐
│  [● 待处理 3]  [✓ 已完成 2]                        │
│                                                    │
│  ┌──────────────────────────────────────────────┐  │
│  │ 📋 Issue #5 · org/repo                       │  │
│  │    登录超时                                    │  │
│  │    📝 请处理该 Issue 的 xxx 问题               │  │
│  │    ⏱ 2026-08-05 · ● 待处理                   │  │
│  │    [去对话]  [查看 Issue ↗]                   │  │
│  └──────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────┐  │
│  │ 📋 Issue #8 · org/repo                       │  │
│  │    API 超时优化                                │  │
│  │    📝 请优化接口超时逻辑                       │  │
│  │    ⏱ 2026-08-04 · ● 待处理                   │  │
│  │    [去对话]  [查看 Issue ↗]                   │  │
│  └──────────────────────────────────────────────┘  │
│                                                    │
│  （已完成 tab：✓ 完成原因 已关闭 / 已转派）          │
└────────────────────────────────────────────────────┘
```

### 7.3 交互

- **Tab 切换**：待处理（默认）/ 已完成
- **去对话（T5）**：`app.nav('chat')` + 打开该任务 `conversation_id`（`chat.js` 增加"按 conversation_id 打开指定会话"能力）
- **查看 Issue**：跳转 `https://gitcode.com/{repo_key}/issues/{issue_number}`（新窗口）

### 7.4 `app.js` 集成

- `VIEWS` 数组增加 `'tasks'`
- `nav(view)` 增加 `view === 'tasks'` 分支 → `title='任务中心'` + `renderTasks()`

---

## 八、Pipeline 责任人改造

### 8.1 现状（`pipeline.py:63-77`）

```
从最后一个非 pending 阶段的最新 event actor 推断：
  actor ∈ bot 用户名 → Agent 显示名
  actor 是用户 → GitCode 用户名
```

### 8.2 改为 Issue assignee（D1/D7）

```
1. SQL 增加 payload 列（pipeline.py:100）
2. _get_latest_assignee(issue_events)：
     for e in reversed(issue_events):            # 从最新往前
         payload = e["payload"] or {}
         obj = payload.get("issue") or payload.get("object_attributes") or {}
         asg = obj.get("assignee", "")
         if isinstance(asg, dict): asg = asg.get("username", "")
         if asg: return asg
     return ""
3. 责任人判定：
     assignee = _get_latest_assignee(issue_events)
     if assignee:
         # 映射飞书昵称：unified_user_mapping WHERE gitcode_user=? 取 feishu_name
         current_owner = feishu_name or assignee     # D7
     else:
         # 回退：现有 actor 推断逻辑
```

### 8.3 assignee 跟随变更

issue agent PATCH `{"assignee": "..."}` → 触发 `issue_updated` 事件 → 新事件 payload 含新 assignee → pipeline 倒序取最新 → **自动跟随** ✅

### 8.4 边界

- 无任何 assignee → 回退现有 actor 推断（保持兼容）
- assignee 是 GitCode 用户名，无飞书映射 → 直接显示用户名

---

## 九、issue skill 指引改造

### 9.1 `skills/issue/references/assignee-guide.md`

在「7. 能确定负责人」的 `PATCH /issues/{n}` 之后追加：

```markdown
### 8. 分配后通知责任人（必做）

分配负责人后，调用个人任务通知接口，创建任务并让 devops-bot 通过该用户的 chat-agent + 飞书私聊通知：

curl -X POST http://localhost:8080/api/tasks/notify \
  -H "Content-Type: application/json" \
  -d '{
    "assignee": "younger_eric",
    "repo_key": "'"$REPO_FULL_NAME"'",
    "issue_number": "'"$ISSUE_NUMBER"'",
    "task_description": "Issue #N 已分配给你，请处理：<简要任务描述>"
  }'

说明：
- assignee 必须是本仓库的有效成员（GitCode 用户名）
- 若该用户未注册平台，接口会返回 {ok:false}，此时在 Issue 评论中说明「该用户未注册 devops-bot 平台，无法收到任务通知」
- 若该用户已注册但未绑定飞书，任务仍会创建，仅无法私聊推送
```

### 9.2 `skills/issue/SKILL.md`

1. 「分配负责人」能力项补充调用说明（指向 assignee-guide.md 步骤 8）。
2. 「关闭 Issue」相关步骤后追加：

```markdown
### 关闭 Issue 后刷新任务（必做）

关闭 Issue 后，调用任务关闭接口，将该 Issue 的待处理任务标记为已完成：

curl -X POST http://localhost:8080/api/tasks/close \
  -H "Content-Type: application/json" \
  -d '{
    "repo_key": "'"$REPO_FULL_NAME"'",
    "issue_number": "'"$ISSUE_NUMBER"'"
  }'
```

---

## 十、代码改动清单

### 新增

| 文件 | 职责 |
|------|------|
| `src/api/routes/tasks.py` | `notify` / `close` / `mine` 三接口 + tasks 表 CRUD 辅助 |
| `src/api/static/js/pages/tasks.js` | 任务中心页（待处理/已完成 tab + 卡片） |
| `tests/test_tasks.py` | 三接口 + user_id 关联 + 换人/关闭刷新 |

### 修改

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | `tasks` 表 + 索引建表 |
| `src/api/main.py` | 注册 `tasks.router` |
| `src/api/routes/pipeline.py` | SQL 加 payload 列；`_get_latest_assignee`；责任人显示飞书昵称；无 assignee 回退 |
| `src/api/static/index.html` | Account 分组加「任务中心」导航（`nav-tasks`） |
| `src/api/static/js/app.js` | `VIEWS` 加 `'tasks'`；`nav()` 加 `'tasks'` 分支 |
| `src/api/static/js/pages/chat.js` | 「去对话」按 conversation_id 打开指定会话（T5） |
| `skills/issue/references/assignee-guide.md` | 追加「8. 分配后通知责任人（必做）」 |
| `skills/issue/SKILL.md` | 分配说明 + 关闭 Issue 后调 `tasks/close` |
| `tests/test_pipeline.py` | assignee 判定（有/无、变更、飞书昵称回退） |

### 不改

- `chat_conversations` / `chat_messages` 表结构（复用现有）

---

## 十一、测试计划

### `tests/test_tasks.py`

| 用例 | 断言 |
|------|------|
| notify 缺 assignee / repo_key / issue_number / task_description | 400 |
| repo 未托管 | 404 |
| assignee 无 user_id（未注册平台） | `{ok:false}` + 提示 |
| 正常路径（mock user_id/open_id/chat_config/consume） | `{ok:true, task_id, conversation_id}`，请求立即返回 |
| 同 assignee 同 issue 重复 notify | 复用已有 active 任务，不重复创建 |
| 换人刷新 | 新 assignee notify 后，原 assignee active 任务 → completed（reassigned） |
| close | 该 issue 全部 active → completed（issue_closed） |
| mine 按 user 过滤 | 只返回当前用户任务 |
| mine 状态过滤 | active / completed 正确分离 |
| 创建 Web conversation | 正常路径下创建 `chat_conversations`（不带 im_channel_id），tasks.conversation_id 回写 |
| 飞书通知降级 | 无 open_id → 任务/对话仍创建成功，仅跳过通知（D3） |

### `tests/test_pipeline.py`

| 用例 | 断言 |
|------|------|
| 有 assignee | 责任人 = 飞书昵称（有映射） |
| 有 assignee 无飞书映射 | 责任人 = GitCode 用户名 |
| assignee 变更 | 取最新事件 assignee |
| 无 assignee | 回退现有 actor 推断 |

---

## 十二、实施步骤

| 步骤 | 内容 |
|------|------|
| 1 | `db_setup.py` 加 `tasks` 表 + 索引 |
| 2 | 新增 `src/api/routes/tasks.py`（notify / close / mine + 后台执行 chat-agent） |
| 3 | `main.py` 注册 `tasks.router` |
| 4 | `pipeline.py`：SQL 加 payload + `_get_latest_assignee` + 飞书昵称责任人 |
| 5 | 前端：`index.html` 导航 + `app.js` nav + `tasks.js` 页面 + `chat.js` 打开指定会话 |
| 6 | `assignee-guide.md` / `SKILL.md` 追加 notify / close 指引 |
| 7 | 新增 `tests/test_tasks.py`、更新 `tests/test_pipeline.py` |
| 8 | 跑通测试 + 手工验证端到端（分配 → 建任务 + Web 对话 → 飞书通知 → 任务中心可见 → 关闭/换人刷新 → 已完成历史） |

---

## 十三、风险与兜底

| 场景 | 处理 |
|------|------|
| assignee 未注册平台（无 user_id） | notify 返回 `{ok:false}`；issue agent 在 Issue 评论说明 |
| assignee 已注册未绑定飞书 | 任务 + Web 对话创建成功，仅跳过飞书通知（D3 降级） |
| 无 chat_bot 运行 | `send_card_to_user` 静默失败（返回空 msg_id），不影响任务/对话 |
| chat-agent 执行失败 | 后台 task 内 log.warning，不扩散；可后续经 Web conversation 重试 |
| Pipeline 无 assignee | 回退现有 actor 推断（保持兼容） |
| 并发通知 | 同 assignee 同 issue 复用 active 任务 |
| 任务量增长 | 已完成任务全部保留（T4），索引 user_id+status 保证查询性能 |
