# 任务生命周期状态机设计（Task Lifecycle）

> 目标：补齐任务状态的完整生命周期——新增 `processing`（处理中）状态，
> 打通「待处理 → 处理中 → 已完成」流转，并支持 bot 接管（assignee=grape_bot）时
> 自动将用户任务标记完成（bot_takeover）。**仅设计，未改代码。**

## 1. 背景与问题

### 1.1 现状：只有两种状态

`tasks` 表（`src/api/db_setup.py`）：
```sql
status           TEXT DEFAULT 'active',      -- active / completed
completed_reason TEXT DEFAULT '',            -- issue_closed / reassigned
completed_at     TEXT DEFAULT NULL
```

| 值 | 含义 | 设置时机 |
|---|---|---|
| `active` | 待处理（"处理中"未单独区分） | `notify` 建任务 |
| `completed` | 已完成 | `/close`（issue_closed）/ `_close_other_assignee_tasks`（reassigned） |

前端把 `active` 一律显示为"待处理"（`tasks.js:78`）。

### 1.2 现状问题

1. **状态不能及时刷新**：`tasks.js` 仅在进入页面/搜索时 `loadTasks()`；去对话后返回任务中心不自动刷新。
2. **无"处理中"状态**：用户点「去对话」开始处理，任务仍显示"待处理"，状态不更新。
3. **bot 接管被拒绝**：`notify` 端点拒绝 bot assignee（`tasks.py:227-229`），
   issue agent 无法通过"assignee=grape_bot"表示自己接管 → 用户任务不会标记完成。

## 2. 状态机设计

### 2.1 三态 + 终态

```
created(active) ──用户点「去对话」/开始处理──▶ processing ──▶ completed
      │                                       │            （issue_closed /
      │                                       │              reassigned /
      │                                       │              bot_takeover）
      │                                       └── bot 接管 ──▶ completed(bot_takeover)
      └──────────── 换人 / Issue 关闭 ───────────────────────▶ completed
```

| 状态 | 含义 | 进入方式 |
|---|---|---|
| `active`（待处理） | 任务已分配，等待处理 | `notify` 建任务 |
| `processing`（处理中） | 用户已进入对话/开始处理 | 点「去对话」/ 对话有用户消息 |
| `completed`（已完成） | 任务结束 | 见 2.3 |

### 2.2 状态迁移

| 迁移 | 触发 | completed_reason |
|---|---|---|
| active → processing | 用户点「去对话」或任务会话产生用户消息 | — |
| processing → completed | Issue 关闭 | `issue_closed` |
| processing → completed | 换人（assignee 变更） | `reassigned` |
| processing → completed | **bot 接管（assignee=grape_bot）** | `bot_takeover` |
| active → completed | 换人 / Issue 关闭 | `reassigned` / `issue_closed` |
| active → completed | **bot 接管** | `bot_takeover` |

### 2.3 bot 接管语义

issue agent 决定自己接管时，`notify` 传 `assignee=grape_bot`。语义：
- **不是新建 bot 任务**，而是**关闭该用户在该 issue 上的 active 任务**（表示用户无需再处理）。
- 关闭 reason = `bot_takeover`。
- 若该 issue 有多个用户任务，全部关闭（同 issue 只需一个接管方）。

## 3. 后端改动

### 3.1 `notify` 端点支持 bot 接管（`tasks.py:notify_task`）

当前拒绝逻辑（`tasks.py:227-229`）改为 bot 接管分支：

```python
# 责任人若是 bot（grape_bot）→ 视为"接管"：关闭该 issue 该用户的 active 任务（bot_takeover），不建新任务
from src.user.db import get_all_bot_usernames
if body.assignee in get_all_bot_usernames():
    # 需要 user_id：先解析 assignee 对应的用户（若 assignee 是 bot，则关闭该 repo+issue 的所有 active 任务）
    closed = _close_issue_tasks_bot_takeover(body.repo_key, body.issue_number)
    if closed:
        return {"ok": True, "task_id": 0, "conversation_id": "",
                "message": f"任务已由 {body.assignee} 接管（关闭 {closed} 条待处理任务）"}
    return {"ok": True, "task_id": 0, "conversation_id": "",
            "message": "无待处理任务可接管"}
```

辅助函数：
```python
def _close_issue_tasks_bot_takeover(repo_key: str, issue_number: int) -> int:
    """bot 接管：将同 issue 所有 active/processing 任务置 completed（bot_takeover）"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE tasks SET status='completed', completed_reason='bot_takeover', completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
            (repo_key, issue_number))
        db.commit()
        return c.rowcount
```

> 注：若 assignee 非 bot 且映射到具体 user_id，仍走正常 notify（建任务 + 换人刷新）。
> bot 接管不需要 user_id（用户任务在 DB 由 repo_key+issue_number 定位）。

### 3.2 新增「开始处理」端点：`POST /api/tasks/{id}/start`

用户点「去对话」时调用，将任务 active → processing：

```python
@router.post("/{task_id}/start")
async def start_task(task_id: int, user: dict = Depends(get_current_user)):
    """用户点「去对话」开始处理：任务 active → processing"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, status FROM tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
        if row[0] != user["id"]:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权操作该任务"})
        if row[1] == "active":
            c.execute("UPDATE tasks SET status='processing' WHERE id=?", (task_id,))
            db.commit()
            return {"status": "processing"}
        return {"status": row[1]}
```

### 3.3 对话事件同步任务状态（可选增强）

任务会话有**用户消息**时（说明用户在推进），若任务仍 active，同步置 processing。
可在 `chat.py` 发消息写入后调用，或由前端「去对话」显式触发（推荐，简单明确）。

> 取舍：推荐前端「去对话」时显式调 `/tasks/{id}/start`（用户点击即表示开始），
> 不依赖后台消息监听——逻辑清晰、可控。

## 4. 前端改动

### 4.1 「去对话」触发 processing（`tasks.js:openTaskConversation`）

```js
function openTaskConversation(t) {
  // t 为当前任务对象（含 id/conversation_id）
  if (t && t.id && t.status === 'active') {
    API.post('/tasks/' + t.id + '/start').then(() => {
      // 本地面乐观更新为 processing
      if (window._tasksById[t.id]) { window._tasksById[t.id].status = 'processing'; }
    }).catch(() => {});
  }
  if (t && t.conversation_id) {
    app.nav('chat', t.conversation_id);
  } else {
    app.nav('chat');
  }
}
```

> 调用处 `tasks.js:143` 由 `openTaskConversation('${t.conversation_id}')` 改为传入任务对象
> `openTaskConversation(window._tasksById[${t.id}])`。

### 4.2 状态展示区分 processing（`tasks.js`）

| 位置 | 现状 | 改为 |
|---|---|---|
| 列表行（`tasks.js:72-86`） | active→"待处理" | active→"待处理"；processing→"处理中"（新样式） |
| 详情（`tasks.js:126-128`） | active→"● 待处理" | active→"● 待处理"；processing→"⏳ 处理中" |
| 页签计数（`tasks.js:60-61`） | active 计数 | active + processing 都算"待处理"分组，或单列"处理中"分组 |

> 简化：列表仍按"未完成（active+processing）在前、completed 在后"分组；active/processing
> 在行/详情上用不同标签样式区分。

### 4.3 返回任务中心自动刷新

`app.nav('tasks')` 时 `renderTasks()` 已重置 `_taskSel` 并 `loadTasks()`（`tasks.js:8-14`）。
从 chat 返回任务中心会重新加载——无需额外改动（若发现不刷新，补 `window.addEventListener('pageshow')` 或 chat 返回时主动 reload）。

## 5. 涉及改动汇总

| 文件 | 改动 |
|---|---|
| `src/api/routes/tasks.py` | notify 支持 bot 接管（关闭任务 bot_takeover）；新增 `POST /{task_id}/start`；`_close_issue_tasks_bot_takeover` |
| `src/api/static/js/pages/tasks.js` | `openTaskConversation` 触发 processing + 状态展示（active/processing/completed） |
| 测试 | 见 §6 |
| 本文档 | `docs/new-version2/task-lifecycle-design.md` |

## 6. 测试用例设计

> 在 `tests/test_tasks.py` 补充；沿用现有 `TestClient`、`_bot_headers()`、`setup_module`
> 预建的 `younger_eric` 映射 + `gitcode:org/test` 托管仓库。

### 6.1 bot 接管：assignee=grape_bot 关闭任务

```python
def test_notify_bot_takeover_closes_tasks():
    """assignee=grape_bot 时，同 issue 的 active/processing 任务置 completed（bot_takeover）"""
    _clean_tasks()
    # 先建一条用户 active 任务
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                  (_uid["id"], "younger_eric", "gitcode:org/test", 20))
        db.commit()
    r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
        "assignee": "grape_bot", "repo_key": "org/test", "issue_number": 20,
        "task_description": "接管"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT status, completed_reason FROM tasks WHERE repo_key='gitcode:org/test' AND issue_number=20").fetchone()
    assert row and row[0] == "completed"
    assert row and row[1] == "bot_takeover"
```

### 6.2 无待处理任务时 bot 接管返回成功但不报错

```python
def test_notify_bot_takeover_no_tasks():
    """同 issue 无 active/processing 任务时 bot 接管返回 ok 且不抛错"""
    _clean_tasks()
    r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
        "assignee": "grape_bot", "repo_key": "org/test", "issue_number": 21,
        "task_description": "接管"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
```

### 6.3 开始处理：active → processing

```python
def test_start_task_active_to_processing():
    """用户点「去对话」：active → processing"""
    _clean_tasks()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                  (_uid["id"], "younger_eric", "gitcode:org/test", 22))
        db.commit()
        tid = c.lastrowid
    r = client.post(f"/api/tasks/{tid}/start", headers=_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "processing"
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row and row[0] == "processing"
```

### 6.4 开始处理：非本人任务拒绝

```python
def test_start_task_not_owner_rejected():
    """非本人任务调 start 返回 403"""
    _clean_tasks()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'active')",
                  (9999, "other", "gitcode:org/test", 23))   # 非本人
        db.commit()
        tid = c.lastrowid
    r = client.post(f"/api/tasks/{tid}/start", headers=_headers)
    assert r.status_code == 403
```

### 6.5 开始处理：已 processing/completed 幂等

```python
def test_start_task_idempotent():
    """processing 任务重复 start 返回当前状态，不改回 active"""
    _clean_tasks()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                  (_uid["id"], "younger_eric", "gitcode:org/test", 24))
        db.commit()
        tid = c.lastrowid
    r = client.post(f"/api/tasks/{tid}/start", headers=_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "processing"
```

### 6.6 close 也关闭 processing 任务

```python
def test_close_marks_processing_completed():
    """Issue 关闭应关闭 processing 任务（不只 active）"""
    _clean_tasks()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                  (_uid["id"], "younger_eric", "gitcode:org/test", 25))
        db.commit()
    r = client.post("/api/tasks/close", headers=_bot_headers(), json={"repo_key": "org/test", "issue_number": 25})
    assert r.status_code == 200
    assert r.json()["closed"] >= 1
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT status, completed_reason FROM tasks WHERE repo_key='gitcode:org/test' AND issue_number=25").fetchone()
    assert row and row[0] == "completed"
    assert row and row[1] == "issue_closed"
```

### 6.7 换人关闭 processing 任务（reassigned）

```python
def test_reassign_closes_processing_old_assignee():
    """换人时原 assignee 的 processing 任务置 completed（reassigned）"""
    _clean_tasks()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                  (_uid["id"], "younger_eric", "gitcode:org/test", 26))
        db.commit()
        old_tid = c.lastrowid
    def _swallow(coro, *a, **k):
        try: coro.close()
        except Exception: pass
        return None
    with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
            "assignee": "bob", "repo_key": "org/test", "issue_number": 26,
            "task_description": "换人处理"})
    assert r.status_code == 200
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT status, completed_reason FROM tasks WHERE id=?", (old_tid,)).fetchone()
    assert row and row[0] == "completed"
    assert row and row[1] == "reassigned"
```

### 6.8 `mine` 接口按 processing 过滤/展示

```python
def test_mine_filters_processing():
    """/tasks/mine 返回 processing 状态任务，前端可分状态展示"""
    _clean_tasks()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO tasks (user_id, assignee, repo_key, issue_number, status) VALUES (?,?,?,?, 'processing')",
                  (_uid["id"], "younger_eric", "gitcode:org/test", 27))
        db.commit()
    r = client.get("/api/tasks/mine", headers=_headers)
    assert r.status_code == 200
    statuses = [t["status"] for t in r.json()["tasks"]]
    assert "processing" in statuses
```

> 注：用例 6.7 需 `bob` 的 unified_user_mapping（`setup_module` 已建 bob 映射的类似先例可复用）。

## 7. 影响与风险

| 项 | 说明 |
|---|---|
| 状态扩展 | `processing` 仅新增取值，无 schema 变更（`status` 是 TEXT）；存量 active 不变 |
| bot 接管语义 | notify 的 bot 拒绝分支改为关闭任务；返回语义变化（不再 ok:false） |
| 前端 | tasks.js 状态展示 + 去对话触发 processing；无 DB/后端新依赖 |
| 兼容性 | `mine` 接口返回含 processing；前端 tab 计数需适配（active+processing 归"待处理"或单列"处理中"） |
| 风险 | 低——纯状态流转 + 一个端点 + 前端展示；现有测试对 notify bot 分支无断言，可安全改 |

## 8. 验收标准

1. `notify(assignee=grape_bot)` → 该 issue 用户 active/processing 任务置 completed(bot_takeover)。
2. 用户点「去对话」→ 任务 active→processing，前端显示"处理中"。
3. `/close` 与换人同时关闭 active 和 processing 任务。
4. `mine` 返回 processing 状态；前端正确分组展示。
5. 新增测试（§6）全部通过。

---

*本文为设计文档，纳入 git（docs/new-version2/task-lifecycle-design.md）。*
