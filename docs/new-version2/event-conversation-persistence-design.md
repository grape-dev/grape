# 事件详情对话持久化优化设计

> 对齐 Chat Agent v31 的稳定机制，解决事件详情中「与 Issue Agent 对话」切换页面丢失、查询不返回、跨机器不可见等问题。

## 1. 现状分析

### 1.1 Chat Agent 已验证的稳定机制（v31）

| 机制 | 实现 | 效果 |
|------|------|------|
| submit/stream 分离 | `POST /chat/submit` 立即返回 task_id；`GET /chat/task/{id}/stream` 订阅 SSE | 发送即返回，不阻塞 |
| task_id 持久化 | `chat_messages` 表加 `task_id` + `generate_status` 列 | 跨机器可见 |
| DB 兜底状态查询 | `GET /chat/task/{task_id}/status`：内存优先 → DB 查 `generate_status` | 服务重启后仍可恢复 |
| agent_worker finally 写 DB | 连接断开后 `add_message(conv_id, "agent", final_text, task_id, generate_status=2)` | 结果不丢失 |
| call_soon_threadsafe | 线程池 → event loop 跨线程投递事件 | SSE 流不卡住 |
| sessionStorage 恢复 | `_setStoredTask(convId, task)` + `resumeChatTask()` | 切换页面回来不中断 |
| AbortController | 离开页面时 abort SSE，不清除 task 状态 | 后台任务继续执行 |
| hermes ACP 修复 | `_handle_response` 只匹配 prompt req_id；15s 静默 → done | LLM 返回不被误判 |

### 1.2 事件详情对话的现有问题

| 问题 | 根因 | 影响 |
|------|------|------|
| **切换页面丢失** | `sendConversation()` 用 `fetch` 直接读 SSE 流，连接断开即结束；无 task_id 持久化 | 切到其他菜单再回来，查询结果丢失 |
| **查询不返回** | `_direct_attempt` 硬编码 `backend_name="opencode"`；credential_id=0 时走 direct 但 `runtime_execution_kind` 返回 `queue_remote`（全局模式），入队后无 worker claim | 永久卡住 |
| **跨机器不可见** | `event_thread` 表无 `task_id` + `generate_status` 列；无状态查询端点 | 其他机器登录看不到查询状态 |
| **SSE 阻塞** | `_run_conversation` 是 async generator，`yield` 阻塞时 event loop 卡住 | 其他页面加载中 |
| **无恢复机制** | 无 `resumeConversation`；`loadConversation()` 只刷新历史消息，不恢复活跃查询 | 查询中切换页面 = 丢失 |

## 2. 优化方案

### 2.1 架构对比

```
Chat Agent v31（已验证）                    事件详情对话（目标）
┌────────────────────────────┐            ┌────────────────────────────┐
│ POST /chat/submit           │            │ POST /events/{seq}/chat/submit │
│ → task_id, conv_id          │            │ → task_id, thread_id           │
├────────────────────────────┤            ├────────────────────────────┤
│ GET /chat/task/{id}/stream  │            │ GET /events/chat/task/{id}/stream │
│ → SSE（重放缓冲 + 实时）    │            │ → SSE（重放缓冲 + 实时）       │
├────────────────────────────┤            ├────────────────────────────┤
│ GET /chat/task/{id}/status  │            │ GET /events/chat/task/{id}/status │
│ → 内存优先 → DB 兜底        │            │ → 内存优先 → DB 兜底          │
├────────────────────────────┤            ├────────────────────────────┤
│ chat_messages.task_id       │            │ event_thread.task_id           │
│ chat_messages.generate_status│           │ event_thread.generate_status   │
├────────────────────────────┤            ├────────────────────────────┤
│ agent_worker finally 写 DB  │            │ _run_conversation finally 写 DB│
│ call_soon_threadsafe        │            │ call_soon_threadsafe           │
├────────────────────────────┤            ├────────────────────────────┤
│ sessionStorage + resumeChat │            │ sessionStorage + resumeConv    │
│ AbortController             │            │ AbortController                │
└────────────────────────────┘            └────────────────────────────┘
```

### 2.2 DB 变更

#### event_thread 表加列

```sql
ALTER TABLE event_thread ADD COLUMN task_id VARCHAR(64) DEFAULT '';
ALTER TABLE event_thread ADD COLUMN generate_status INTEGER DEFAULT NULL;
CREATE INDEX idx_event_thread_task_id ON event_thread(task_id);
```

**generate_status 语义**（与 Chat Agent 一致）：
- `1` = 生成中（user 消息已写，agent 未回复）
- `2` = 成功（agent 回复已写）
- `3` = 失败
- `4` = 中断（服务重启等）

#### DDL 同步（db_setup.py）

```python
_EVENT_THREAD_DDL = """
CREATE TABLE IF NOT EXISTS event_thread (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_seq  INTEGER NOT NULL,
    repo_key    TEXT NOT NULL,
    issue_id    TEXT DEFAULT '',
    agent_type  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     MEDIUMTEXT NOT NULL,
    created_by  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    task_id     TEXT DEFAULT '',
    generate_status INTEGER DEFAULT NULL
)
"""
```

#### 迁移逻辑（_ensure_column）

```python
_ensure_column(c, "event_thread", "task_id", "TEXT DEFAULT ''")
_ensure_column(c, "event_thread", "generate_status", "INTEGER DEFAULT NULL")
```

### 2.3 后端 API 新增

#### POST /events/{seq}/chat/submit

立即返回 task_id，不阻塞。

```python
@router.post("/{seq}/chat/submit")
async def submit_conversation(seq: int, body: ConversationMessage, user: dict = Depends(get_current_user)):
    """提交对话查询 → 立即返回 task_id。后台异步执行 agent。"""
    evt = _load_event(seq)
    gate = _thread_gate(evt["repo_key"], evt["issue_id"])
    if not gate["open"]:
        raise HTTPException(status_code=409, ...)

    # 生成 task_id
    import uuid as _uuid
    conv_task_id = "conv_" + _uuid.uuid4().hex[:12]

    # 写 user 消息（带 task_id + generate_status=1）
    _add_thread_message(seq, evt["repo_key"], evt["issue_id"], evt["agent_type"],
                        "user", body.message.strip(), user.get("username", ""),
                        task_id=conv_task_id, generate_status=1)

    # 同步准备 prompt + session（快速操作，不阻塞）
    prompt, ws, oc_session_id, ... = _prepare_conversation(evt, body.message)

    # 写入内存任务缓冲
    with _conv_tasks_lock:
        _conv_tasks[conv_task_id] = {
            "status": "running", "events": [], "result": "", "error": "",
            "seq": seq, "repo_key": evt["repo_key"], "agent_type": evt["agent_type"],
            "created_at": time.time(), "finished_at": 0,
        }

    # 启动后台执行（run_in_executor）
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _execute_conv_task, conv_task_id)

    return {"task_id": conv_task_id, "seq": seq, "status": "running"}
```

#### GET /events/chat/task/{task_id}/status

```python
@router.get("/chat/task/{task_id}/status")
async def conv_task_status(task_id: str, user: dict = Depends(get_current_user)):
    """查询对话任务状态（内存优先 → DB 兜底）。"""
    # 1. 内存
    with _conv_tasks_lock:
        task = _conv_tasks.get(task_id)
        if task:
            return {"task_id": task_id, "status": task["status"],
                    "result": task.get("result", ""), "seq": task.get("seq", 0)}

    # 2. DB 兜底（event_thread by task_id）
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT role, generate_status, content FROM event_thread WHERE task_id=? ORDER BY id", (task_id,))
        rows = c.fetchall()
    has_user = any(r[0] == "user" for r in rows)
    has_agent = any(r[0] == "agent" for r in rows)
    if has_agent:
        for r in rows:
            if r[0] == "agent":
                gen = r[1]
                return {"task_id": task_id, "status": "done" if gen == 2 else "error",
                        "result": r[2] or ""}
    if has_user and not has_agent:
        return {"task_id": task_id, "status": "error",
                "result": "", "error": "查询已中断（服务重启），请重新发送消息"}
    return {"task_id": task_id, "status": "not_found"}
```

#### GET /events/chat/task/{task_id}/stream

SSE 流式输出（重放缓冲 + 实时追加）。

```python
@router.get("/chat/task/{task_id}/stream")
async def conv_task_stream(task_id: str, user: dict = Depends(get_current_user)):
    """订阅对话任务 SSE 输出（支持断线重连 + 缓冲重放）。"""
    # ... 与 chat_task_stream 类似，重放 _conv_tasks[task_id]["events"]
```

#### _execute_conv_task（后台执行）

```python
def _execute_conv_task(task_id: str):
    """后台执行对话任务（线程池中运行，call_soon_threadsafe 投递事件）。"""
    loop = asyncio.get_event_loop()  # 主 event loop（通过参数传入）
    # ... 从 _conv_tasks 获取 task 参数
    try:
        # direct 模式：stream_agent / consume_stream_async
        # queue 模式：enqueue + wait
        for evt_type, data in stream_agent(...):
            _worker_final_text += ...
            loop.call_soon_threadsafe(_push_event, task_id, evt_type, data)
    finally:
        # 写 DB（带 task_id + generate_status）
        add_thread_message(seq, ..., "agent", final_text, task_id=task_id, generate_status=2)
        loop.call_soon_threadsafe(_finish_task, task_id, final_text)
```

### 2.4 后端修复

#### _direct_attempt 引擎选择

```python
# 修复前（硬编码 opencode）：
gen = consume_stream_async(..., backend_name="opencode", ...)

# 修复后（按 issue 会话配置选引擎 + credential_id=0 强制 direct）：
from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT, runtime_credential_id
rid = _resolve_conversation_runtime_id(repo_key, agent_type)
kind = runtime_execution_kind(rid)
if (runtime_credential_id(rid) or 0) == 0:
    kind = EXEC_MODE_DIRECT  # 本机引擎强制 direct
```

#### _add_thread_message 支持 task_id

```python
def _add_thread_message(anchor_seq, repo_key, issue_id, agent_type, role,
                        content, created_by="", task_id="", generate_status=None):
    with db_lock:
        c = db.cursor()
        if task_id:
            c.execute(
                "INSERT INTO event_thread (anchor_seq, repo_key, issue_id, agent_type, "
                "role, content, created_by, task_id, generate_status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (anchor_seq, repo_key, issue_id, agent_type, role, content, created_by,
                 task_id, generate_status))
        else:
            c.execute(
                "INSERT INTO event_thread (anchor_seq, repo_key, issue_id, agent_type, "
                "role, content, created_by) VALUES (?,?,?,?,?,?,?)",
                (anchor_seq, repo_key, issue_id, agent_type, role, content, created_by))
        db.commit()
        return c.lastrowid
```

### 2.5 前端改造（events.js）

#### submit/stream 分离

```javascript
function sendConversation() {
  const input = document.getElementById('convInput');
  if (!input || !_convSeq) return;
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';

  const messagesEl = document.getElementById('convMessages');
  messagesEl.insertAdjacentHTML('beforeend', _convBubble('user', msg));

  const replyId = 'convReply' + Date.now();
  messagesEl.insertAdjacentHTML('beforeend',
    '<div class="conv-reply" id="' + replyId + '">'
    + '<span class="chat-dots"><span></span><span></span><span></span></span></div>');
  messagesEl.scrollTop = messagesEl.scrollHeight;

  // 1. submit → 立即获取 task_id
  API.post('/events/' + _convSeq + '/chat/submit', {message: msg}).then(r => {
    if (r.status === 'error') {
      document.getElementById(replyId).innerHTML = r.error || '查询失败';
      return;
    }
    // 记录到 sessionStorage
    _setConvTask(_convSeq, {task_id: r.task_id, el_id: replyId, status: 'running'});
    // 2. SSE 订阅
    streamConvTask(r.task_id, replyId, _convSeq);
  }).catch(e => {
    document.getElementById(replyId).innerHTML = '请求失败: ' + e.message;
  });
}
```

#### streamConvTask（SSE 订阅 + 断线不清除）

```javascript
let _convStreamAbort = null;
let _convTasks = {};  // {seq: {task_id, el_id, status}}

async function streamConvTask(taskId, elId, seq) {
  if (_convStreamAbort) { _convStreamAbort.abort(); _convStreamAbort = null; }
  const ac = new AbortController();
  _convStreamAbort = ac;

  const el = document.getElementById(elId);
  let finalText = '';

  try {
    const resp = await fetch('/api/events/chat/task/' + taskId + '/stream', {
      headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
      signal: ac.signal
    });
    if (!resp.ok || !resp.body) {
      if (el) el.innerHTML = '连接失败';
      _clearConvTask(seq);
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\n');
      buf = lines.pop() || '';
      lines.forEach(line => {
        if (line.startsWith('data: ')) {
          try {
            const d = JSON.parse(line.substring(6));
            if (d.type === 'done') {
              finalText = d.text || finalText;
              if (el) el.innerHTML = renderMarkdown(finalText) || '（无回复）';
              _clearConvTask(seq);
              loadConversation();
            } else if (d.type === 'text' && d.text) {
              finalText += d.text;
              if (el) el.innerHTML = renderMarkdown(finalText);
            } else if (d.type === 'thinking') {
              if (el) el.innerHTML = '<span class="chat-dots"><span></span><span></span><span></span></span>';
            }
          } catch(e) {}
        }
      });
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  } catch(e) {
    if (e.name === 'AbortError') return;
    // 连接断开 → 不清除 task，切回来时 resumeConvTask 恢复
    if (el) el.innerHTML = '<span class="chat-dots"><span></span><span></span><span></span></span>';
  }
}
```

#### resumeConvTask（切换回事件详情时恢复）

```javascript
function resumeConvTask() {
  if (!_convSeq) return;
  const task = _getConvTask(_convSeq);
  if (!task || !task.task_id) {
    // 无活跃 task → 查 DB 最后一条消息
    API.get('/events/' + _convSeq + '/thread').then(d => {
      const msgs = d.messages || [];
      const lastMsg = msgs[msgs.length - 1];
      if (lastMsg && lastMsg.role === 'user' && lastMsg.task_id) {
        _setConvTask(_convSeq, {task_id: lastMsg.task_id, status: 'running'});
        resumeConvTask();
      }
    });
    return;
  }
  // 查 status
  fetch('/api/events/chat/task/' + task.task_id + '/status', {
    headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
  }).then(r => r.json()).then(st => {
    if (st.status === 'running') {
      // 仍在执行 → SSE 重连
      streamConvTask(task.task_id, task.el_id, _convSeq);
    } else {
      // done/error → 刷新消息
      _clearConvTask(_convSeq);
      loadConversation();
    }
  });
}
```

#### sessionStorage 持久化

```javascript
function _setConvTask(seq, task) {
  _convTasks[seq] = task;
  try { sessionStorage.setItem('conv_task_' + seq, JSON.stringify(task)); } catch(e) {}
}
function _getConvTask(seq) {
  if (_convTasks[seq]) return _convTasks[seq];
  try { const s = sessionStorage.getItem('conv_task_' + seq); return s ? JSON.parse(s) : null; } catch(e) { return null; }
}
function _clearConvTask(seq) {
  delete _convTasks[seq];
  try { sessionStorage.removeItem('conv_task_' + seq); } catch(e) {}
}
```

#### app.js nav 调用

```javascript
// 离开事件详情时中断 SSE（不清除 task）
if (typeof _convStreamAbort !== 'undefined' && _convStreamAbort && view !== 'event') {
  _convStreamAbort.abort();
  _convStreamAbort = null;
}
// 切回事件详情时恢复
if (view === 'event' && typeof resumeConvTask === 'function') {
  setTimeout(resumeConvTask, 500);
}
```

### 2.6 hermes ACP 适配

`_direct_attempt` 中使用 `consume_stream_async` 调用 `stream_agent` → `_stream_hermes_acp`。已有的 hermes ACP 修复（`_handle_response` 只匹配 prompt req_id + 15s 静默 → done）同样适用，无需额外修改。

### 2.7 保留旧端点兼容

保留 `POST /{seq}/chat/stream` 作为兼容端点（内部调 submit + stream），前端逐步切换到 submit/stream 分离模式。

## 3. 实施步骤

| 步骤 | 文件 | 改动 |
|------|------|------|
| 1 | `src/api/db_setup.py` | event_thread DDL 加 task_id + generate_status；_ensure_column 迁移 |
| 2 | MySQL RDS | `ALTER TABLE event_thread ADD COLUMN task_id VARCHAR(64) DEFAULT ''` + `generate_status INTEGER` |
| 3 | `src/api/routes/events.py` | `_add_thread_message` 支持 task_id + generate_status |
| 4 | `src/api/routes/events.py` | 新增 `_conv_tasks` 内存缓冲 + `submit_conversation` + `conv_task_status` + `conv_task_stream` |
| 5 | `src/api/routes/events.py` | 新增 `_execute_conv_task` 后台执行（call_soon_threadsafe + finally 写 DB） |
| 6 | `src/api/routes/events.py` | 修复 `_direct_attempt` 引擎选择（credential_id=0 强制 direct） |
| 7 | `src/api/static/js/pages/events.js` | `sendConversation` 改为 submit/stream 分离 |
| 8 | `src/api/static/js/pages/events.js` | 新增 `streamConvTask` + `resumeConvTask` + sessionStorage |
| 9 | `src/api/static/js/app.js` | nav 离开/恢复事件详情时中断/恢复 SSE |
| 10 | `src/api/static/index.html` | events.js 版本号 bump |

## 4. 验证用例

### 4.1 基本功能验证

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V1-发送消息 | 事件详情 → 输入消息 → 发送 | 三点动画 → 流式文本 → 最终回复 |
| V2-消息持久化 | 发送消息后刷新页面 | 历史消息完整显示（含 user + agent） |
| V3-task_id 写入 | 发送消息后查 DB `event_thread` | user 行有 task_id + generate_status=1；agent 行有 task_id + generate_status=2 |

### 4.2 切换页面不中断

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V4-切到其他菜单 | 发送消息 → 立即切到流水线 → 切回事件详情 | 三点动画恢复 → 流式文本继续 → 最终回复 |
| V5-切到其他菜单（已完成） | 发送消息 → 等 agent 完成 → 切到流水线 → 切回 | 显示最终回复（从 DB 加载） |
| V6-多次切换 | 发送消息 → 切到流水线 → 切回 → 切到任务中心 → 切回 | 每次切回都恢复到当前查询状态 |

### 4.3 跨机器可见

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V7-其他机器登录 | 机器 A 发送消息 → 机器 B 登录同账号 → 打开同一事件详情 | 机器 B 能看到 user 消息 + agent 回复（从 DB 加载） |
| V8-其他机器恢复 | 机器 A 发送消息（agent 正在执行）→ 机器 B 登录 → 打开同一事件详情 | 机器 B 看到三点动画（task_id 查 status=running）→ SSE 重连 → 流式文本 |

### 4.4 异常恢复

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V9-服务重启 | 发送消息 → 服务重启 → 重新登录 → 打开事件详情 | 显示"查询已中断，请重新发送消息"（generate_status=1 但无 agent 行 → error） |
| V10-agent 超时 | 发送消息 → agent 60s 无响应 | 显示"对话执行超时"（generate_status=3） |
| V11-LLM 网关故障 | 发送消息 → LLM 网关 401 | 显示错误信息（generate_status=3） |

### 4.5 不阻塞其他页面

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V12-查询中访问其他页面 | 发送消息 → 立即访问流水线/任务中心/场景中心 | 其他页面正常加载（0.03s），不卡住 |
| V13-多事件并行查询 | 事件 A 发送消息 → 切到事件 B 发送消息 → 切回 A | 两个查询并行执行，互不干扰 |

### 4.6 API 验证

| 用例 | 操作 | 预期结果 |
|------|------|----------|
| V14-submit 立即返回 | `POST /events/{seq}/chat/submit` | <100ms 返回 `{task_id, seq, status: "running"}` |
| V15-status DB 兜底 | 服务重启后 `GET /events/chat/task/{id}/status` | 返回 `{status: "error", error: "查询已中断..."}` |
| V16-stream 重放 | 发送消息 → 5s 后订阅 stream | 先收到缓冲事件 → 再收到实时事件 |
| V17-旧端点兼容 | `POST /events/{seq}/chat/stream`（旧） | 仍能正常工作（内部调 submit + stream） |

### 4.7 自动化测试

```python
# tests/test_event_conversation.py

class TestEventConversationSubmit:
    """V1-V3: 基本功能"""

    def test_submit_returns_task_id(self, client, auth_header):
        """V1: submit 立即返回 task_id"""
        resp = client.post(f"/api/events/{seq}/chat/submit",
                          json={"message": "test"}, headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"].startswith("conv_")
        assert data["status"] == "running"

    def test_user_message_has_task_id(self, db, task_id):
        """V3: user 消息写入 event_thread 带 task_id + generate_status=1"""
        row = db.execute("SELECT task_id, generate_status FROM event_thread WHERE task_id=?", (task_id,)).fetchone()
        assert row[0] == task_id
        assert row[1] == 1

    def test_agent_message_has_generate_status_2(self, db, task_id):
        """V3: agent 回复写入后 generate_status=2"""
        # 等待 agent 完成
        row = db.execute("SELECT generate_status FROM event_thread WHERE task_id=? AND role='agent'", (task_id,)).fetchone()
        assert row[0] == 2


class TestEventConversationStatus:
    """V15: DB 兜底状态查询"""

    def test_status_not_found(self, client, auth_header):
        """不存在的 task_id → not_found"""
        resp = client.get("/api/events/chat/task/nonexistent/status", headers=auth_header)
        assert resp.json()["status"] == "not_found"

    def test_status_running_in_memory(self, client, auth_header, task_id):
        """内存有 → running"""
        resp = client.get(f"/api/events/chat/task/{task_id}/status", headers=auth_header)
        assert resp.json()["status"] == "running"

    def test_status_done_in_db(self, client, auth_header, task_id):
        """内存无 + DB 有 agent 行 → done"""
        # 清除内存（模拟服务重启）
        _conv_tasks.clear()
        resp = client.get(f"/api/events/chat/task/{task_id}/status", headers=auth_header)
        assert resp.json()["status"] == "done"

    def test_status_error_interrupted(self, client, auth_header, task_id):
        """内存无 + DB 有 user 无 agent → error（中断）"""
        _conv_tasks.clear()
        resp = client.get(f"/api/events/chat/task/{task_id}/status", headers=auth_header)
        assert resp.json()["status"] == "error"
        assert "中断" in resp.json().get("error", "")


class TestEventConversationNoBlock:
    """V12: 不阻塞其他页面"""

    def test_pipeline_during_conversation(self, client, auth_header, task_id):
        """查询中访问 pipeline API 不阻塞"""
        import time
        t0 = time.time()
        resp = client.get("/api/pipeline/songspace/hello-blog", headers=auth_header)
        elapsed = time.time() - t0
        assert resp.status_code == 200
        assert elapsed < 1.0  # 1 秒内返回
```

## 5. 风险与回退

### 5.1 风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| event_thread 加列失败 | 低 | 新功能不可用 | _ensure_column 自动迁移 + MySQL 手动 ALTER |
| _execute_conv_task 阻塞 event loop | 中 | 其他页面卡住 | 使用 run_in_executor + call_soon_threadsafe |
| hermes ACP 在事件对话中卡住 | 中 | 查询不返回 | 已有 15s 静默超时修复 |
| 旧端点兼容性问题 | 低 | 旧前端报错 | 保留 /chat/stream 端点 |

### 5.2 回退方案

1. 前端回退：events.js 版本号回退到旧版本
2. 后端回退：保留旧 `/chat/stream` 端点，新端点 `/chat/submit` + `/chat/task/*/status` + `/chat/task/*/stream` 可独立禁用
3. DB 回退：`task_id` + `generate_status` 列保留（不影响旧逻辑）

## 6. 对齐 Chat Agent 的关键设计决策

| 决策 | Chat Agent | 事件详情对话 | 理由 |
|------|-----------|-------------|------|
| submit/stream 分离 | ✅ | ✅ | 不阻塞 event loop |
| task_id 持久化 | chat_messages | event_thread | 跨机器可见 |
| generate_status | ✅ | ✅ | DB 兜底状态查询 |
| finally 写 DB | ✅ | ✅ | 连接断开结果不丢 |
| call_soon_threadsafe | ✅ | ✅ | 跨线程投递事件 |
| sessionStorage | ✅ | ✅ | 切换页面恢复 |
| AbortController | ✅ | ✅ | 离开页面中断 SSE |
| resume 函数 | resumeChatTask | resumeConvTask | 切回时恢复 |
| DB 兜底 status | ✅ | ✅ | 服务重启后可恢复 |
| credential_id=0 强制 direct | ✅ | ✅ | 本机引擎不入队黑洞 |
