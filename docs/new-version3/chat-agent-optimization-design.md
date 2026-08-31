# Chat Agent 对话能力优化方案

> 2026-08-29 设计。学习 Grape 运营管理台 AI 分析对话机制（AiTaskManager + AiChatService），
> 优化 Chat Agent 的响应速度、跨机器恢复能力、页面切换不中断。

## 一、现状分析

### 1.1 当前 Grape Chat Agent 架构

```
前端 doSend()
  → POST /api/chat/send/stream (单端点：提交+流式一体)
    → StreamingResponse(event_stream())
      → agent_worker() 在 run_in_executor 中执行
        → stream_agent() → hermes_acp.stream_execute()
          → ACP JSON-RPC over stdin/stdout
      → 事件通过 asyncio.Queue 桥接到 event_stream
    → SSE yield 到前端
  → 连接断开时 agent_worker finally 写 DB
  → 切回来时 resumeChatTask 轮询 /task/{task_id}/status (2s 间隔)
```

### 1.2 当前问题清单

| # | 问题 | 根因 | 影响 |
|---|---|---|---|
| P1 | **响应速度慢** | agent_worker 通过 asyncio.Queue 桥接事件，额外开销；ACP done 检测有 15s+10s 双计时器 | 用户等待时间长 |
| P2 | **页面切换只能轮询** | 单端点 send/stream，连接断开后无法重新订阅 SSE 流 | 切回后只能 2s 轮询 DB status，无法实时获取后续输出 |
| P3 | **无缓冲重放** | agent_worker 的事件只通过 SSE yield，无内存缓冲 | 切回时已输出的文本丢失，只能看到最终结果 |
| P4 | **ACP done 检测脆弱** | `_CHAT_STABLE_SECONDS=15` + `_CHAT_IDLE_SECONDS=10` 双计时器，总延迟 25s | chat 任务完成后还要等 10-25s 才返回 |
| P5 | **远端引擎走队列模式** | 远端 worker 执行时 `wait_task_terminal` 阻塞 event_stream，无实时流式 | 远端引擎场景只能等全部完成才显示 |
| P6 | **跨机器恢复依赖 DB 轮询** | 无内存任务管理器，task status 只靠 DB 兜底 | 恢复延迟大，无法获取中间过程 |

### 1.3 Admin AI 机制（已验证跑通，作为参考）

```
前端 runQueryAsync()
  → POST /ai/chat/submit → 立即返回 {taskId, sessionId}
  → GET /ai/chat/task/{taskId}/stream (SSE 订阅)
    → AiTaskManager.subscribe(taskId, emitter)
      → 先重放 task.events 缓冲（已输出的 token）
      → 再实时追加新事件
    → 后台 Thread: spawn python3 ai_hermes_query.py
      → BufferedReader 逐行读 stdout → task.events 缓冲 → flush 到 subscribers
    → finally: saveHistory (写 DB assistant 消息)
  → 页面切换时 onUnmounted 中断 SSE 但不清 sessionStorage
  → 切回时 reconnectActiveTask:
    → status 预检 (GET /task/{taskId}/status)
    → running → 重新 streamTask (SSE 重放缓冲 + 实时追加)
    → done/error → 拉取最终消息
```

**Admin AI 核心优势：**
1. submit/stream 分离 → 提交立即返回，SSE 可断开重连
2. 内存 events 缓冲 → SSE 重连时重放已输出内容，不丢不重
3. SseEmitter 不阻塞 Tomcat 线程
4. 外部进程执行（ProcessBuilder）→ 天然不阻塞
5. sessionStorage 持久化 taskId → 跨页面/跨标签恢复
6. status 预检 → 避免对已完成任务盲目重连

## 二、优化方案

### 2.1 核心架构：submit/stream 分离 + 内存任务管理器

```
前端 doSend()
  → POST /api/chat/submit → 立即返回 {task_id, conversation_id}
  → GET /api/chat/task/{task_id}/stream (SSE 订阅，可断开重连)
    → ChatTaskManager.subscribe(task_id) → 重放缓冲 + 实时追加
  → 页面切换 → onUnmounted abort SSE，不清 sessionStorage
  → 切回 → reconnectActiveTask:
    → status 预检
    → running → 重新 streamTask (SSE 重放 + 实时)
    → done/error → 拉取最终消息

后端:
  POST /api/chat/submit
    → 生成 task_id，写 user 消息 (generate_status=1)
    → ChatTaskManager.submit(task) → 启动 agent_worker 线程
    → 返回 {task_id, conversation_id}

  agent_worker (后台线程)
    → stream_agent() → hermes_acp.stream_execute()
    → 事件 → ChatTaskManager.push_event(task_id, event)
      → 追加到 task.events 缓冲
      → flush 到所有 SSE subscribers
    → finally: 写 DB (generate_status=2/3)，释放信号量

  GET /api/chat/task/{task_id}/stream
    → ChatTaskManager.subscribe(task_id)
      → 先重放 task.events 缓冲
      → running → 加入 subscribers，实时推送
      → done/error → 重放后 complete
```

### 2.2 ChatTaskManager 设计（学习 Admin AI AiTaskManager）

```python
# src/api/chat_task_manager.py (新文件)

import threading, time, asyncio, json
from collections import deque
from src.config import log

class ChatTask:
    """单个 chat 任务的内存状态"""
    def __init__(self, task_id, conv_id, user_id, username):
        self.task_id = task_id
        self.conv_id = conv_id
        self.user_id = user_id
        self.username = username
        self.status = "running"  # running / done / error
        self.events = deque(maxlen=500)  # 事件缓冲（SSE 重放用）
        self.subscribers = []  # asyncio.Queue 列表（每个 SSE 订阅一个）
        self.created_at = time.time()
        self.finished_at = 0.0
        self.final_text = ""
        self.error = ""

class ChatTaskManager:
    """内存任务管理器 — submit/stream 分离 + 缓冲重放"""

    _tasks = {}  # task_id → ChatTask
    _lock = threading.Lock()
    _semaphore = threading.Semaphore(20)  # 并发限制

    @classmethod
    def submit(cls, task_id, conv_id, user_id, username, agent_fn):
        """启动后台 agent_worker 线程，立即返回"""
        with cls._lock:
            task = ChatTask(task_id, conv_id, user_id, username)
            cls._tasks[task_id] = task
        # 后台线程执行 agent
        t = threading.Thread(target=cls._run, args=(task, agent_fn), daemon=True)
        t.start()
        return task

    @classmethod
    def _run(cls, task, agent_fn):
        """后台线程：执行 agent，事件推入缓冲 + subscribers"""
        if not cls._semaphore.acquire(blocking=True):
            cls._set_error(task, "并发超限")
            return
        try:
            for evt_type, data in agent_fn():
                cls._push_event(task, evt_type, data)
                if evt_type == "done":
                    if isinstance(data, dict):
                        task.final_text = data.get("text", "")
                    break
                elif evt_type == "error":
                    break
            task.status = "done" if task.final_text else "error"
        except Exception as e:
            cls._set_error(task, str(e))
        finally:
            cls._semaphore.release()
            task.finished_at = time.time()
            cls._notify_done(task)
            # 30 分钟后自动清理
            # (cleanup 由定时器或在 subscribe 时惰性检查)

    @classmethod
    def _push_event(cls, task, evt_type, data):
        """事件 → 缓冲 + flush 到所有 subscribers"""
        sse_data = json.dumps({"type": evt_type, "text": data if isinstance(data, str) else (data.get("text", "") if isinstance(data, dict) else str(data))})
        task.events.append(sse_data)
        for sub in list(task.subscribers):
            try:
                sub.put_nowait(sse_data)
            except:
                task.subscribers.remove(sub)

    @classmethod
    def _notify_done(cls, task):
        """通知所有 subscribers 任务完成"""
        for sub in list(task.subscribers):
            try:
                sub.put_nowait(None)  # None = 结束信号
            except:
                pass
        task.subscribers.clear()

    @classmethod
    def subscribe(cls, task_id, loop):
        """订阅任务输出 — 返回 asyncio.Queue（先重放缓冲，再实时）"""
        with cls._lock:
            task = cls._tasks.get(task_id)
        if not task:
            return None, None
        q = asyncio.Queue()
        # 重放缓冲
        for evt in task.events:
            q.put_nowait(evt)
        if task.status != "running":
            q.put_nowait(None)  # 已完成 → 结束信号
            return q, task
        # 运行中 → 加入 subscribers
        task.subscribers.append(q)
        return q, task

    @classmethod
    def get_status(cls, task_id):
        """查询任务状态（内存优先，DB 兜底由调用方处理）"""
        with cls._lock:
            task = cls._tasks.get(task_id)
        if task:
            return {"status": task.status, "text": task.final_text, "error": task.error}
        return None

    @classmethod
    def cleanup_stale(cls):
        """清理 30 分钟前已完成的任务"""
        cutoff = time.time() - 30 * 60
        with cls._lock:
            stale = [tid for tid, t in cls._tasks.items()
                     if t.status != "running" and t.finished_at > 0 and t.finished_at < cutoff]
            for tid in stale:
                del cls._tasks[tid]
        if stale:
            log.info(f"ChatTaskManager cleanup {len(stale)} stale tasks")
```

### 2.3 后端 API 改动（chat.py）

#### 新增 POST /api/chat/submit

```python
@router.post("/submit")
async def chat_submit(body: ChatMessage, user: dict = Depends(get_current_user)):
    """提交 chat 查询 → 立即返回 task_id（学习 Admin AI submit/stream 分离）"""
    # 1. 会话处理（与 send/stream 一致）
    conv_id = body.conversation_id
    # ... token 解析、conv 获取/创建（同现有逻辑）...

    # 2. 生成 task_id，写 user 消息
    chat_task_id = "chat_" + _uuid.uuid4().hex[:12]
    add_message(conv_id, "user", body.message, task_id=chat_task_id, generate_status=1)
    mark_task_processing_by_conversation(conv_id)

    # 3. 构建 prompt（同现有逻辑）
    cfg = _get_chat_config(user["id"])
    backend_name = cfg.get("backend", "")
    # ... prompt 构建（同现有逻辑）...

    # 4. 无引擎 → 直接返回错误
    if not backend_name:
        add_message(conv_id, "agent", "⚠️ 无可用引擎", task_id=chat_task_id, generate_status=3)
        return {"task_id": chat_task_id, "conversation_id": conv_id, "status": "error"}

    # 5. 引擎路由：direct vs queue_remote
    rid = cfg.get("runtime_id") or 0
    # ... 引擎解析（同现有逻辑）...

    if kind == EXEC_MODE_DIRECT:
        # 本机直连：ChatTaskManager 启动 agent_worker
        from src.api.chat_task_manager import ChatTaskManager

        def agent_fn():
            for evt_type, data in stream_agent(
                prompt=prompt, cwd=_get_chat_cwd(user["id"], conv_id),
                session_id=runtime_session_id, backend_name=backend_name,
                user_id=user["id"], account=user["username"],
                user_token=gitcode_token, agent_name="Chat Agent",
                agent_label=f"[Agent:chat|user:{user['username']}]",
            ):
                yield evt_type, data

        ChatTaskManager.submit(chat_task_id, conv_id, user["id"], user["username"], agent_fn)

        # agent_worker 的 finally 逻辑（DB 写入 + 飞书推送）在 ChatTaskManager._run 的回调中
        # 见下方 _run_wrapper 设计
    else:
        # 远端引擎：入队 + ChatTaskManager 轮询任务状态
        _enqueue_chat_remote(chat_task_id, ...)  # 见 2.5

    return {"task_id": chat_task_id, "conversation_id": conv_id, "status": "running"}
```

#### 新增 GET /api/chat/task/{task_id}/stream

```python
@router.get("/task/{task_id}/stream")
async def chat_task_stream(task_id: str, user: dict = Depends(get_current_user)):
    """SSE 订阅任务输出 — 先重放缓冲，再实时追加（学习 Admin AI subscribe）"""
    from src.api.chat_task_manager import ChatTaskManager

    loop = asyncio.get_running_loop()
    q, task = ChatTaskManager.subscribe(task_id, loop)
    if not q:
        # 内存无 → 任务可能已完成或跨机器 → 返回 DB 中的最终结果
        # (DB 兜底逻辑同现有 chat_task_status)
        ...

    async def event_stream():
        yield f"data: {json.dumps({'type': '思考中', 'text': '正在连接后端...'})}\n\n"
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=120)
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'error', 'text': '超时'})}\n\n"
                return
            if item is None:  # 结束信号
                break
            yield f"data: {item}\n\n"
        # 发送 done 事件
        if task:
            yield f"data: {json.dumps({'type': 'done', 'text': task.final_text, 'task_id': task_id})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                           headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
```

#### 改造 chat_task_status — 加入内存优先

```python
@router.get("/task/{task_id}/status")
async def chat_task_status(task_id: str, user: dict = Depends(get_current_user)):
    # 1. 内存优先（实时状态 + 累积文本）
    from src.api.chat_task_manager import ChatTaskManager
    mem = ChatTaskManager.get_status(task_id)
    if mem:
        return {"status": mem["status"], "text": mem["text"], "error": mem["error"], "source": "memory"}

    # 2. DB 兜底（同现有逻辑）
    # ... 查 chat_messages by task_id ...
```

### 2.4 agent_worker 适配 — ChatTaskManager 集成

ChatTaskManager._run 的 agent_fn 包装器：

```python
def _run_agent_fn(prompt, cwd, session_id, backend_name, user, conv_id,
                  gitcode_token, chat_task_id, runtime_session_id):
    """构建 agent_fn — yield 事件 + finally 写 DB"""
    from src.runtime.executor import stream_agent
    from src.session_manager import add_message, touch_conversation
    from src.config import log

    _final_text = ""
    try:
        for evt_type, data in stream_agent(
            prompt=prompt, cwd=cwd, session_id=session_id,
            backend_name=backend_name, user_id=user["id"],
            account=user["username"], user_token=gitcode_token,
            agent_name="Chat Agent",
            agent_label=f"[Agent:chat|user:{user['username']}]",
        ):
            if evt_type == "text":
                text = data if isinstance(data, str) else (data.get("text", "") if isinstance(data, dict) else str(data))
                if text:
                    _final_text += text
                    yield ("text", text)  # 增量文本
            elif evt_type == "done":
                if isinstance(data, dict):
                    done_text = data.get("text", "")
                    if done_text and not _final_text:
                        _final_text = done_text
                yield ("done", {"text": _final_text})
            elif evt_type == "error":
                yield ("error", str(data))
                break
            else:
                yield (evt_type, data)
    finally:
        # DB 写入（与现有 agent_worker finally 一致）
        if _final_text:
            add_message(conv_id, "agent", _final_text, task_id=chat_task_id, generate_status=2)
            touch_conversation(conv_id)
            # 飞书推送
            asyncio.run_coroutine_threadsafe(
                _push_reply_to_feishu(get_conversation(conv_id), user, _final_text),
                asyncio.get_event_loop()
            )
        else:
            add_message(conv_id, "agent", "（无返回内容）", task_id=chat_task_id, generate_status=3)
            touch_conversation(conv_id)
```

### 2.5 远端引擎场景优化

远端引擎（queue_remote）当前 `wait_task_terminal` 阻塞 event_stream，无实时流式。

优化方案：ChatTaskManager 启动一个轮询线程，订阅远端 worker 的 task_messages：

```python
def _enqueue_chat_remote(chat_task_id, tid, conv_id, user, ...):
    """远端引擎：入队 + 轮询 task_messages → 推入 ChatTaskManager 缓冲"""
    from src.api.chat_task_manager import ChatTaskManager
    from src.runtime.service import enqueue_task, get_task_messages_batch

    def remote_poller():
        """轮询远端任务消息，推入 ChatTaskManager"""
        last_seq = 0
        while True:
            msgs = get_task_messages_batch(tid, since_seq=last_seq)
            for m in msgs:
                ChatTaskManager.push_event(chat_task_id, m["type"], m["data"])
                last_seq = m["seq"]
            # 检查任务终态
            task = get_task_status(tid)
            if task["status"] in ("completed", "failed"):
                ChatTaskManager.finish(chat_task_id, task.get("result_text", ""))
                break
            time.sleep(0.5)  # 500ms 轮询

    ChatTaskManager.submit(chat_task_id, conv_id, user["id"], user["username"],
                          remote_poller_fn)
```

### 2.6 前端改造（chat.js v31）

学习 Admin AI AiAssistant.vue 的 streamTask + reconnectActiveTask：

```javascript
// doSend: submit → streamTask
function doSend(input, body) {
  const msg = input.value.trim();
  input.value = '';
  body.innerHTML += '<div class="msg l">' + msg.replace(/</g,'&lt;') + '</div>';
  const tid = 't' + Date.now();
  body.innerHTML += '<div class="msg r" id="' + tid + '"><span class="chat-typing">查询中</span>...</div>';

  // 1. submit → 立即获取 task_id
  API.post('/chat/submit', {message: msg, conversation_id: _currentConvId}).then(r => {
    _chatActiveTask = {task_id: r.task_id, conv_id: r.conversation_id || _currentConvId, el_id: tid};
    if (_currentConvId) _setStoredTask(_currentConvId, _chatActiveTask);
    // 2. SSE 订阅
    streamTask(r.task_id, tid);
  });
}

// streamTask: fetch SSE + ReadableStream（可重连）
let _chatStreamAbort = null;
async function streamTask(taskId, elId) {
  if (_chatStreamAbort) { _chatStreamAbort.abort(); _chatStreamAbort = null; }
  const ac = new AbortController();
  _chatStreamAbort = ac;

  const body = document.getElementById('chatBody');
  const el = document.getElementById(elId);
  let finalText = '';

  try {
    const resp = await fetch('/api/chat/task/' + taskId + '/stream', {
      headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
      signal: ac.signal
    });
    if (!resp.ok || !resp.body) {
      if (el) el.innerHTML = '连接失败: ' + resp.statusText;
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
              if (el) el.innerHTML = renderMarkdown(finalText) || _NO_REPLY;
            } else if (d.text && el) {
              finalText = d.text;
              el.innerHTML = renderMarkdown(finalText);
            }
          } catch(e) {}
        }
      });
      body.scrollTop = body.scrollHeight;
    }
    // 完成 → 清理
    _chatActiveTask = null;
    if (_currentConvId) _clearStoredTask(_currentConvId);
    loadConversations();
  } catch(e) {
    if (e.name === 'AbortError') return;  // 主动中断（切换页面）
    // 连接断开 → 后端继续执行，切回来时 reconnectActiveTask 恢复
  }
}

// reconnectActiveTask: 学习 Admin AI — status 预检 + SSE 重连
async function reconnectActiveTask() {
  if (!_chatActiveTask) {
    if (_currentConvId) _chatActiveTask = _getStoredTask(_currentConvId);
    if (!_chatActiveTask) return;
  }
  const taskId = _chatActiveTask.task_id;
  if (!taskId) { _chatActiveTask = null; return; }

  // 1. status 预检
  try {
    const st = await API.get('/chat/task/' + taskId + '/status');
    if (st.status === 'done' || st.status === 'error') {
      // 已完成 → 刷新消息
      _chatActiveTask = null;
      _clearStoredTask(_chatActiveTask.conv_id);
      API.get('/chat/messages/' + _currentConvId).then(d => renderChatMessages(d.messages || []));
      loadConversations();
      return;
    }
    if (st.status === 'not_found') {
      // 任务丢失 → 提示
      _chatActiveTask = null;
      _clearStoredTask(_currentConvId);
      API.get('/chat/messages/' + _currentConvId).then(d => {
        renderChatMessages(d.messages || []);
        const body = document.getElementById('chatBody');
        if (body) body.innerHTML += '<div class="msg r" style="color:var(--t3)">（查询已中断，请重新发送）</div>';
      });
      return;
    }
    // running → 重新订阅 SSE（缓冲重放 + 实时追加）
    const elId = _chatActiveTask.el_id || ('t' + Date.now());
    await streamTask(taskId, elId);
  } catch(e) {
    // 网络异常 → 静默，下次重试
  }
}
```

### 2.7 Hermes ACP done 检测优化

当前问题：`_CHAT_STABLE_SECONDS=15` + `_CHAT_IDLE_SECONDS=10` 双计时器，总延迟最多 25s。

优化方案：

```python
# hermes_acp.py — chat 任务 done 检测优化

# 旧：双计时器
# _CHAT_STABLE_SECONDS = 15  # 首次 text 后至少等 15s
# _CHAT_IDLE_SECONDS = 10    # 最后一条 text 后连续 10s 无新 text

# 新：单一 idle 计时器 + 快速 done 检测
_CHAT_IDLE_SECONDS = 5       # 最后一条 text 后连续 5s 无新 text → done
_CHAT_MIN_WAIT = 3           # 最少等 3s（防 LLM 首 token 延迟误判）

# 判断逻辑简化：
if _is_chat_task and collected_text:
    if _chat_first_text_time == 0:
        _chat_first_text_time = time.time()
    _chat_last_text_time = time.time()
    # 条件：至少等了 _CHAT_MIN_WAIT + 最后 text 后 idle _CHAT_IDLE_SECONDS
    elapsed_since_first = time.time() - _chat_first_text_time
    elapsed_since_last = time.time() - _chat_last_text_time
    if elapsed_since_first >= _CHAT_MIN_WAIT and elapsed_since_last >= _CHAT_IDLE_SECONDS:
        # → done
```

另外，主循环中的 `elapsed_silent > 15` 也降低到 `8`：

```python
# 旧
if collected_text and elapsed_silent > 15:
    ...
# 新
if collected_text and elapsed_silent > 8:
    ...
```

### 2.8 引擎交互机制选择：ACP vs CLI

**结论：保持 ACP 为主选机制，CLI 为降级备选。**

| 维度 | ACP (JSON-RPC) | CLI (verbose) |
|------|----------------|---------------|
| 结构化 | ✅ JSON 事件 | ❌ 正则解析 |
| 实时流式 | ✅ chunk 级 | ✅ 行级 |
| 会话管理 | ✅ session/resume | ⚠️ --continue 最近会话 |
| 权限处理 | ✅ request_permission 自动 | ❌ --yolo 无交互 |
| 稳定性 | ⚠️ done 检测需启发式 | ✅ 进程退出=done |
| 并发隔离 | ✅ per-task HERMES_HOME | ❌ 共享 state.db |
| 工具调用 | ✅ 结构化 tool_call | ⚠️ 文本解析 |

**ACP 的 done 检测问题是 hermes 实现层面**（turn_end 通知不发），不是协议问题。优化 idle 超时即可。

**CLI 降级条件**（ACP 不可用时）：
- `hermes acp --check` 失败
- ACP 连续 3 次 30s 内 reader EOF（依赖未装）

降级逻辑已有（hermes.py 的 `_probe_verbose_mode`），保持不变。

### 2.9 保留功能清单

以下功能**不丢失**，在新架构中保持原有位置：

| 功能 | 当前位置 | 新架构位置 | 说明 |
|------|----------|-----------|------|
| 飞书推送 | `_push_reply_to_feishu` | agent_fn finally | DB 写入后推送 |
| GitCode token 注入 | `_resolve_gitcode_token` | submit 端点 | 不变 |
| 任务会话 prompt | `build_task_chat_prompt` | submit 端点 | 不变 |
| Issue 评论同步 | `_sync_issue_comments_to_conversation` | messages 端点 | 不变 |
| 飞书扫码配置 | `startFeishuOnboard` | 前端 | 不变 |
| 仓库绑定 | `conv.repo_key` | submit 端点 | 不变 |
| 技能选择 | `_get_chat_config` | submit 端点 | 不变 |
| 引擎路由 | direct/queue_remote | submit 端点 | 不变 |
| 跨机器恢复 | task_id + DB status | submit/stream/status | 增强：内存优先+DB兜底 |

### 2.10 兼容性方案

**旧端点 POST /api/chat/send/stream 保留**，作为向后兼容（旧前端版本仍可调用）：
- 内部改为调用 ChatTaskManager.submit + 同步等待结果（行为与当前一致）
- 新前端使用 POST /submit + GET /task/{id}/stream

**DB Schema 不变**：chat_messages.task_id + generate_status 已有，无需新迁移。

## 三、实施步骤

### Phase 1: ChatTaskManager + submit/stream 分离（核心）

1. 新建 `src/api/chat_task_manager.py`
2. 新增 `POST /api/chat/submit` 端点
3. 新增 `GET /api/chat/task/{task_id}/stream` 端点
4. 改造 `chat_task_status` 加入内存优先
5. 旧 `/send/stream` 内部适配 ChatTaskManager

### Phase 2: 前端改造

1. `chat.js` 升级到 v31：doSend → submit + streamTask
2. `reconnectActiveTask` 改为 status 预检 + SSE 重连（替代 2s 轮询）
3. `app.js` nav 恢复逻辑保持 `setTimeout(resumeChatTask, 500)` 不变

### Phase 3: ACP done 检测优化

1. `hermes_acp.py` 降低 idle 超时参数
2. 简化双计时器为单 idle 计时器

### Phase 4: 远端引擎实时流式

1. `_enqueue_chat_remote` 轮询 task_messages → ChatTaskManager 缓冲
2. 远端 worker 代码不需要改动（task_messages 机制已有）

## 四、验证用例

### 4.1 基础功能验证

**TC-01: submit/stream 基本流程**
```
前置：本机 hermes 引擎健康，LLM 网关 key 有效
步骤：
  1. curl -X POST /api/chat/submit -d '{"message":"你好","conversation_id":""}' -H "Auth: Bearer <token>"
  2. 验证返回 {task_id: "chat_xxx", conversation_id: "xxx", status: "running"}
  3. curl -N /api/chat/task/<task_id>/stream -H "Auth: Bearer <token>"
  4. 验证 SSE 流先收到 {type: "思考中"} → {type: "text", text: "..."} → {type: "done"}
预期：
  - submit 立即返回（< 200ms）
  - SSE 流有实时文本输出（非全部完成后才返回）
  - done 事件包含完整回复文本
  - DB chat_messages 有 user(generate_status=1) + agent(generate_status=2) 两条
```

**TC-02: 页面切换不中断**
```
前置：TC-01 通过
步骤：
  1. 前端发送消息，SSE 流正在接收 {type: "text"} 事件
  2. 点击导航切换到 "Task Center" 页面
  3. 等待 5s
  4. 切回 "Chat" 页面
预期：
  - 切回后自动调用 reconnectActiveTask
  - status 预检返回 "running"
  - SSE 重新订阅，重放已缓冲的文本（不丢失）
  - 继续接收后续实时输出直到 done
  - 最终显示完整回复
```

**TC-03: 跨机器恢复**
```
前置：机器 A 和机器 B 共享 MySQL DB
步骤：
  1. 机器 A 上发送 chat 消息，获取 task_id
  2. 在 agent 执行期间，从机器 B 查询 GET /api/chat/task/<task_id>/status
  3. 验证返回 status=running（内存或 DB）
  4. 等任务完成后，机器 B 再查 status
预期：
  - 机器 B 能查到 task 状态（running → done）
  - done 时返回完整文本
  - DB chat_messages 表有完整的 user+agent 消息对
```

**TC-04: 任务完成后 SSE 重连**
```
步骤：
  1. 发送消息，等待完成（status=done）
  2. 再次 GET /api/chat/task/<task_id>/stream
预期：
  - SSE 重放所有缓冲事件
  - 最后发 done 事件
  - 不卡在 running 状态
```

### 4.2 性能验证

**TC-05: 响应速度对比**
```
步骤：
  1. 用旧端点 /send/stream 发送 "1+1=?"，记录从发送到 done 的时间 T_old
  2. 用新端点 /submit + /stream 发送同样消息，记录 T_new
  3. 各测 5 次取中位数
预期：
  - T_new < T_old + 200ms（submit 多一跳但 ACP done 检测优化抵消）
  - 首个 text 事件时间 TTFT_new ≤ TTFT_old（实时流式不受影响）
```

**TC-06: ACP done 检测延迟**
```
步骤：
  1. 发送简单问题 "什么是 GitCode？"
  2. 记录最后一个 text 事件时间 T_last_text
  3. 记录 done 事件时间 T_done
  4. 计算 delta = T_done - T_last_text
预期：
  - delta ≤ 8s（优化前 15-25s）
```

**TC-15: 会话切换不中断（核心用例）**
```
前置：本机 hermes 引擎健康
步骤：
  1. 在会话 A 发送消息 "帮我分析一下 GitCode 的优势"
  2. SSE 正在接收 text 事件（agent 正在回复）
  3. 点击左侧会话列表切换到会话 B
  4. 会话 B 正常显示历史消息
  5. 等 5s 后切回会话 A
预期：
  - 切回 A 时消息列表末尾有 3 点动画占位（chat-typing + chat-dots）
  - 3 点动画持续到任务完成
  - SSE 重连后先重放已缓冲的文本（不丢失之前已输出的内容）
  - 继续接收后续实时输出直到 done
  - 最终显示完整回复（与未切换会话的结果一致）
  - 会话列表中会话 A 不显示"查询中"状态（不在当前视图）
```

**TC-16: 多会话并发查询**
```
步骤：
  1. 会话 A 发送消息 "1+1=?" → SSE A 开始
  2. 切换到会话 B，发送消息 "2+2=?" → SSE B 开始
  3. 切换回会话 A → 3 点动画 + SSE 重连
  4. 等 A 完成后切换到会话 B → 3 点动画或已完成
预期：
  - 后端 ChatTaskManager 同时维护 taskA 和 taskB
  - 两个任务互不干扰，各自独立完成
  - 切换到任一会话都能正确显示状态（running → done）
  - 两个会话的最终回复都正确写入 DB
```

**TC-17: 3点动画视觉效果**
```
步骤：
  1. 发送消息，观察 chat-body 中 agent 回复区域
  2. 验证 3 点动画 CSS 渲染
预期：
  - 显示"查询中"文字 + 3 个圆点交替跳动
  - 圆点颜色为 var(--a)（主题强调色）
  - 动画频率 1.4s 循环
  - 任务完成后动画消失，替换为实际回复文本
  - 切换会话再切回时动画重新出现（如果任务仍在运行）
```

**TC-18: 会话切换后任务已完成**
```
步骤：
  1. 会话 A 发送消息
  2. 切换到会话 B
  3. 等足够长时间确保会话 A 的任务已完成
  4. 切回会话 A
预期：
  - 切回时不显示 3 点动画（任务已完成）
  - 直接显示完整回复（从 DB 刷新）
  - 无"查询已中断"提示（任务是正常完成的）
```

**TC-19: 会话列表运行中标记**
```
步骤：
  1. 会话 A 发送消息，SSE 正在运行
  2. 切换到会话 B
  3. 观察会话列表中会话 A 的显示
预期（可选增强）：
  - 会话 A 标题旁有运行中标记（如 ● 或微光效果）
  - 类似 Admin AI 的 isSessionRunning 指示器
  - 任务完成后标记消失
```

### 4.6 边界场景验证（补充）

**TC-07: 服务重启后恢复**
```
步骤：
  1. 发送消息，agent 正在执行
  2. 重启 Grape 服务（kill + restart uvicorn）
  3. 切回 Chat 页面
预期：
  - reconnectActiveTask 查 status → 内存无 → DB 兜底
  - DB 有 user 无 agent → status=error（对齐 Admin AI getTaskFinalStatus）
  - 显示 "查询已中断，请重新发送消息"
```

**TC-08: 并发限制**
```
步骤：
  1. 同时发送 21 条消息（信号量=20）
  2. 第 21 条应等待
预期：
  - 20 条并发执行
  - 第 21 条在某个完成后才开始
  - 无 500 错误
```

**TC-09: 飞书推送不丢失**
```
前置：会话已绑定飞书（im_channel_id 非空）
步骤：
  1. 通过 Web 发送消息
  2. 等待 agent 回复
  3. 检查飞书是否收到推送
预期：
  - agent 回复完成后飞书收到 "[来自Web] xxx" 推送
  - 推送在 DB 写入后触发（finally 块）
```

**TC-10: 远端引擎实时流式**
```
前置：配置远端引擎（203.0.113.1 worker 在线）
步骤：
  1. 发送消息，引擎路由到远端 worker
  2. GET /api/chat/task/<task_id>/stream
预期：
  - SSE 收到实时 text 事件（非全部完成后才返回）
  - 事件来源：ChatTaskManager 轮询 task_messages → 缓冲 → SSE
  - 延迟 ≤ 1s（500ms 轮询 + 500ms SSE flush）
```

**TC-11: 旧端点兼容**
```
步骤：
  1. 用旧端点 POST /api/chat/send/stream 发送消息
预期：
  - 行为与当前一致（单端点流式）
  - 内部通过 ChatTaskManager 执行
  - 不影响新端点功能
```

**TC-12: hermes ACP 不可用时降级**
```
步骤：
  1. 模拟 hermes acp --check 失败（重命名 hermes 二进制）
  2. 发送 chat 消息
预期：
  - 引擎降级到 CLI verbose 模式
  - 或返回明确的错误信息
```

### 4.4 数据完整性验证

**TC-13: DB 消息一致性**
```
步骤：
  1. 发送消息并等待完成
  2. 查询 chat_messages WHERE task_id = '<task_id>'
预期：
  - 恰好 2 条：user(generate_status=1) + agent(generate_status=2)
  - agent content 非空且与 SSE done 事件 text 一致
```

**TC-14: 中断后 DB 状态**
```
步骤：
  1. 发送消息，agent 执行中切换页面
  2. 后端 agent_worker 继续执行并在 finally 写 DB
  3. 查询 DB
预期：
  - agent 消息已写入（finally 块保证）
  - generate_status=2（成功）或 3（失败/空内容）
```

## 四点五、会话切换不中断 + 3点动画（新增）

### 4.5.1 问题分析

当前 chat.js 会话切换的缺陷：

```
switchConversation(convId)
  → _currentConvId = convId      // 直接覆盖，旧会话的 _chatActiveTask 丢失
  → body.innerHTML = '...'       // DOM 重建，旧 SSE 的 el 引用失效
  → API.get('/chat/messages/'+convId)  // 加载新会话消息
  → 不检查旧会话是否有运行中任务
```

当前 `_chatActiveTask` 是单变量（非 Map），切换会话后：
- 旧会话的 SSE fetch 仍在运行，但 el 已失效（getElementById 返回 null）→ 事件被丢弃
- 切回旧会话时 `_chatActiveTask` 已被新会话覆盖 → 无法恢复
- 没有在消息列表中显示"正在查询"的 3 点动画

Admin AI 的做法（AiAssistant.vue）：
```
switchSession(s)
  → activeSessionId.value = s.id   // 只是切换指针，不破坏旧会话数据
  → s.msgs === null ? loadMsgs(s)  // 懒加载消息
  → reconnectActiveTask()           // 检查当前会话是否有运行中任务
    → status 预检
    → running → streamTask(s, aiMsg)  // 重新 SSE 订阅（先 abort 旧 SSE）
    → done/error → 拉取最终消息
```

关键差异：Admin AI 的 sessions 数组中每个会话独立持有 `taskId`、`taskCompleted`、`msgs`（含 `streaming: true` 的占位消息）。切换只是换指针，不破坏数据。

### 4.5.2 前端改造：per-conversation 任务追踪

将 `_chatActiveTask` 单变量改为 `_chatTasks` Map：

```javascript
// chat.js v31

// 旧：let _chatActiveTask = null;  // 单变量，切换会话后丢失
// 新：per-conversation 任务状态
let _chatTasks = {};  // { conv_id: { task_id, el_id, status, finalText, abortController } }
let _chatStreamAbort = null;  // 当前活跃的 SSE AbortController

function _getTask(convId) {
  return _chatTasks[convId] || null;
}

function _setTask(convId, task) {
  _chatTasks[convId] = task;
  if (convId) _setStoredTask(convId, task);  // sessionStorage 持久化
}

function _clearTask(convId) {
  delete _chatTasks[convId];
  if (convId) _clearStoredTask(convId);
}
```

### 4.5.3 switchConversation 改造

```javascript
function switchConversation(convId) {
  _currentConvId = convId;
  loadConversations();
  const body = document.getElementById('chatBody');
  const input = document.getElementById('chatInput');
  body.innerHTML = '<div class="loading">' + I18N.t('common.loading') + '</div>';

  API.get('/chat/messages/' + convId).then(d => {
    const msgs = d.messages || [];
    const conv = d.conversation || {};

    // 更新标题
    const hdr = document.querySelector('.chat-hdr h2');
    if (hdr) {
      const titleBase = conv.title || 'Personal Assistant';
      const safeTitle = String(titleBase).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      hdr.innerHTML = I18N.t('chat.header.title_prefix') + linkifyIssueNumbers(safeTitle, conv.repo_key || '');
    }

    // 渲染消息列表
    body.innerHTML = msgs.map(m => {
      const rendered = renderMarkdown(m.content);
      return `<div class="msg ${m.role === 'user' ? 'l' : 'r'}">${linkifyIssueNumbers(rendered, conv.repo_key || '')}</div>`;
    }).join('');

    // ★ 关键：检查最后一条消息是否有运行中任务
    const lastMsg = msgs[msgs.length - 1];
    if (lastMsg && lastMsg.role === 'user' && lastMsg.task_id) {
      // 有 task_id → 查后端 status（内存优先，DB 兜底）
      fetch('/api/chat/task/' + lastMsg.task_id + '/status', {
        headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
      }).then(r => r.json()).then(st => {
        if (st.status === 'running') {
          // ★ 任务仍在执行 → 追加 3 点动画占位 + SSE 重连
          const elId = 't' + Date.now();
          body.innerHTML += '<div class="msg r" id="' + elId + '">'
            + '<span class="chat-typing">' + I18N.t('chat.querying') + '</span>'
            + '<span class="chat-dots"><span></span><span></span><span></span></span>'
            + '</div>';
          body.scrollTop = body.scrollHeight;

          // 记录任务状态（per-conversation）
          _setTask(convId, {task_id: lastMsg.task_id, conv_id: convId, el_id: elId, status: 'running'});

          // SSE 重连（后端重放缓冲 + 实时追加）
          streamTask(lastMsg.task_id, elId, convId);
        } else if (st.status === 'done' || st.status === 'error') {
          // 任务已完成但 DB 中 agent 消息可能刚写入 → 刷新消息
          if (st.status === 'done' && !msgs.some(m => m.role === 'agent' && m.task_id === lastMsg.task_id)) {
            // agent 消息不在列表中 → 重新拉取
            API.get('/chat/messages/' + convId).then(d2 => {
              renderChatMessages(d2.messages || []);
              loadConversations();
            });
          }
          _clearTask(convId);
        } else {
          // not_found → 任务丢失
          _clearTask(convId);
          body.innerHTML += '<div class="msg r" style="color:var(--t3)">（查询已中断，请重新发送消息）</div>';
          body.scrollTop = body.scrollHeight;
        }
      }).catch(() => { _clearTask(convId); });
    }

    body.scrollTop = body.scrollHeight;
    if (input) input.disabled = false;
    const sendBtn = document.querySelector('.chat-in button');
    if (sendBtn) sendBtn.disabled = false;
  }).catch(() => {
    body.innerHTML = '<div class="error">' + I18N.t('chat.load_failed') + '</div>';
  });
}
```

### 4.5.4 streamTask 改造 — 不中断旧会话

```javascript
// 旧：_chatStreamAbort 全局单变量，切换会话时 abort 旧 SSE
// 新：只在以下情况 abort：
//   1. 同一会话发起新查询时（旧任务被新任务替代）
//   2. 离开 Chat 页面时（app.js nav 切换）
//   3. 切换到有运行中任务的会话时（旧 SSE 让位给新会话的 SSE）

async function streamTask(taskId, elId, convId) {
  // 如果当前有 SSE 在运行，且是不同会话的 → abort（让位）
  // 如果是同一会话的 → 也 abort（新查询替代旧查询）
  if (_chatStreamAbort) { _chatStreamAbort.abort(); _chatStreamAbort = null; }
  const ac = new AbortController();
  _chatStreamAbort = ac;

  const body = document.getElementById('chatBody');
  const el = document.getElementById(elId);
  let finalText = '';

  try {
    const resp = await fetch('/api/chat/task/' + taskId + '/stream', {
      headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
      signal: ac.signal
    });
    if (!resp.ok || !resp.body) {
      if (el) el.innerHTML = '连接失败: ' + resp.statusText;
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
              if (el) el.innerHTML = renderMarkdown(finalText) || _NO_REPLY;
            } else if (d.text && el) {
              finalText = d.text;
              el.innerHTML = renderMarkdown(finalText);
            }
          } catch(e) {}
        }
      });
      // 只有当前会话才滚动（后台会话不抢滚动）
      if (_currentConvId === convId) body.scrollTop = body.scrollHeight;
    }
    // 完成 → 清理任务状态
    _clearTask(convId);
    // 刷新会话列表（更新最后消息预览）
    if (_currentConvId === convId) loadConversations();
  } catch(e) {
    if (e.name === 'AbortError') return;  // 主动中断（切换会话/离开页面）
    // 连接断开 → 后端继续执行，切回来时 switchConversation 会重连
    // 不清除 _chatTasks[convId] — 保留状态供切回时恢复
  }
}
```

### 4.5.5 doSend 适配 per-conversation

```javascript
function doSend(input, body) {
  const msg = input.value.trim();
  input.value = '';
  body.innerHTML += '<div class="msg l">' + msg.replace(/</g,'&lt;') + '</div>';
  const tid = 't' + Date.now();
  body.innerHTML += '<div class="msg r" id="' + tid + '">'
    + '<span class="chat-typing">' + I18N.t('chat.querying') + '</span>'
    + '<span class="chat-dots"><span></span><span></span><span></span></span>'
    + '</div>';
  body.scrollTop = body.scrollHeight;

  API.post('/chat/submit', {
    message: msg,
    conversation_id: _currentConvId,
  }).then(r => {
    const convId = r.conversation_id || _currentConvId;
    // per-conversation 记录
    _setTask(convId, {task_id: r.task_id, conv_id: convId, el_id: tid, status: 'running'});
    streamTask(r.task_id, tid, convId);
  }).catch(e => {
    const el = document.getElementById(tid);
    if (el) el.innerHTML = I18N.t('common.request_failed') + e.message;
  });
}
```

### 4.5.6 页面离开时的处理（app.js）

```javascript
// app.js nav 切换离开 Chat 页面时：
if (oldView === 'chat') {
  // 中断 SSE 但不清任务状态（后台任务继续跑，切回来时恢复）
  if (typeof _chatStreamAbort !== 'undefined' && _chatStreamAbort) {
    _chatStreamAbort.abort();
    _chatStreamAbort = null;
  }
}
// nav 切换回 Chat 页面时：
if (view === 'chat') {
  renderChat(param || null);
  // switchConversation 内部会检查运行中任务并自动重连
  // 不再需要单独的 resumeChatTask（已内联到 switchConversation）
}
```

### 4.5.7 3点动画 CSS（已有，无需新增）

```css
/* 已存在于 CSS 中 */
.chat-typing{display:inline-block;font-size:12px;color:var(--t2)}
.chat-dots{display:inline-flex;gap:3px;margin-left:6px;vertical-align:middle}
.chat-dots span{width:5px;height:5px;border-radius:50%;background:var(--a);animation:chatDotBounce 1.4s infinite ease-in-out both}
.chat-dots span:nth-child(1){animation-delay:-0.32s}
.chat-dots span:nth-child(2){animation-delay:-0.16s}
@keyframes chatDotBounce{0%,80%,100%{transform:scale(0.6);opacity:0.4}40%{transform:scale(1);opacity:1}}
```

### 4.5.8 数据流对比

```
旧（当前）：
  doSend → SSE → 切换会话 → _chatActiveTask 丢失 → el 失效 → 事件丢弃
  切回 → resumeChatTask（单变量）→ 2s 轮询 DB → 无中间过程

新（优化后）：
  doSend → submit + streamTask → 切换会话 → _chatTasks[old_conv] 保留
    → 旧 SSE 被 abort（让位给新会话）
    → 后端 ChatTaskManager 继续执行，事件存入内存缓冲
  切回旧会话 → switchConversation
    → 查 DB messages → 最后一条 user 有 task_id
    → 查 status → running
    → 追加 3 点动画占位
    → streamTask 重连 SSE → 重放缓冲（已输出文本）+ 实时追加
    → done → 渲染完整回复
```

### 4.5.9 多会话并发场景

用户在会话 A 发送消息后切换到会话 B 发送消息：

```
1. 会话 A: doSend → submit(taskA) → streamTask(taskA) → SSE A 开始
2. 切换到会话 B: _chatTasks[A] 保留, SSE A 被 abort
   → switchConversation(B): 无运行中任务 → 正常渲染
3. 会话 B: doSend → submit(taskB) → streamTask(taskB) → SSE B 开始
4. 切换回会话 A: _chatTasks[B] 保留, SSE B 被 abort
   → switchConversation(A): 查 messages → lastMsg.task_id = taskA
   → 查 status(taskA):
     → running → 3点动画 + streamTask(taskA) 重连（重放缓冲）
     → done → 刷新消息显示完整回复
5. 切换回会话 B: 同理恢复
```

后端 ChatTaskManager 同时维护 taskA 和 taskB 两个任务的内存状态，互不干扰。

## 五、风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| ChatTaskManager 内存泄漏 | 中 | OOM | 30 分钟定时清理 + deque maxlen=500 |
| SSE 连接过多 | 低 | 连接池耗尽 | 前端 AbortController + 后端 120s 超时 |
| ACP done 检测过激进 | 中 | 提前结束 | 保留 8s idle + 3s min_wait 双条件 |
| 远端轮询延迟 | 低 | 用户体验 | 500ms 轮询 + SSE flush，可接受 |
| 旧前端调用旧端点 | 低 | 兼容性 | 旧端点保留，内部适配 |

## 六、版本号

- chat.js: v31（submit/streamTask/reconnectActiveTask）
- app.js: v37（不变，nav 恢复逻辑已有）
- chat.py: 新增 submit/stream 端点
- chat_task_manager.py: 新文件
- hermes_acp.py: idle 超时参数调整
