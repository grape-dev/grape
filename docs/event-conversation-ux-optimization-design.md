# 事件详情对话体验优化设计方案

> 对齐 Chat Agent 最新机制，让事件详情中与 Issue Agent 的对话达到同等的实时交互体验。

## 一、背景与现状

### 1.1 Chat Agent 最新机制（标杆）

Chat Agent 经过多次迭代，已具备完善的 SSE 流式交互能力：

**后端（chat.py）：**
- submit/stream 两步分离：POST /chat/submit 立即返回 task_id，前端再 SSE 订阅 /chat/task/{id}/stream
- agent_worker 线程跑 stream_agent，跨线程投递事件到 asyncio.Queue
- SSE 事件类型丰富：`思考中`(空text) → `step_start`/`step_finish`/`reasoning`/`tool_use`/`tool_result` → `text`(增量) → `生成中`(累计) → `done`(最终)
- 看门狗：60s 无输出主动报错；SSE 心跳：15s idle 发 ping 防代理掐断
- finally 块保证 DB 落库（即使 SSE 断连）
- 队列模式降级：入队 + 轮询 task status

**前端（chat.js streamTask）：**
- AbortController 管理 SSE 生命周期（切换会话/离开页面中断旧流）
- 思考中 → 三点动画占位（`chat-dots` bounce 动画）
- 中间步骤（reasoning/step_start/step_finish/tool_use 等）→ 单行滚动显示（`chat-ticker`）+ 三点动画，每个新事件替换上一行
- 流式 text chunk → 追加到正文（renderMarkdown 实时渲染）
- done → 只显示最终结果
- 会话切换恢复：resumeChatTask 查 task status，running 则 SSE 重连

### 1.2 事件详情 Issue Agent 对话现状（待优化）

**后端（events.py）：**
- `stream_conversation`：直接 StreamingResponse，无 submit/stream 分离
- `_run_conversation` 生成器 yield 事件：`thinking` → `text`(增量) → `done`
- `_direct_attempt` 里 `consume_stream_async` 把 `reasoning` 映射为 `thinking`，`text` 原样传递
- **问题1**：`step_start`/`step_finish`/`tool_use`/`tool_result` 等中间事件被丢弃——`_direct_attempt` 只处理 text/reasoning/done，其他事件类型不上抛
- **问题2**：队列模式下只 yield thinking + done（一次性返回，无中间过程）
- **问题3**：无 SSE 心跳（长连接可能被代理掐断）
- **问题4**：无 task_id 机制（无法断线重连）

**前端（events.js sendConversation）：**
- 思考占位：纯文字 "⏳ 思考中…"（无动画）
- SSE 解析只处理 `text` 和 `done` 两种事件类型——`thinking` 事件被忽略不渲染
- 中间步骤完全不可见（后端没上抛，前端也没处理）
- 无 AbortController（切换事件不中断旧 SSE）
- 无断线重连机制
- done 后调 `loadConversation()` 刷新整个对话区（闪烁）

### 1.3 差异对比

| 维度 | Chat Agent | 事件详情 Issue Agent |
|------|-----------|---------------------|
| 思考占位 | 三点动画（chat-dots bounce） | 纯文字"⏳ 思考中…" |
| 中间步骤 | 单行滚动（chat-ticker）实时显示 reasoning/step/tool | 完全不可见（后端丢弃） |
| 流式正文 | 增量追加 + 实时 renderMarkdown | 增量追加（有） |
| SSE 管理 | AbortController + 断线重连 | 无（切事件不中断旧流） |
| 看门狗 | 60s 超时报错 | _iter_with_timeout 有（但仅 direct 模式） |
| SSE 心跳 | 15s ping | 无 |
| 完成后刷新 | 原地更新 el.innerHTML | loadConversation() 全量刷新（闪烁） |
| 队列模式 | 入队 + 轮询 + SSE 重放缓冲 | 只 yield thinking + done |

## 二、设计目标

1. 思考占位改为三点动画（复用 chat-dots CSS）
2. 中间步骤（step_start/step_finish/reasoning/tool_use）实时单行滚动显示（复用 chat-ticker CSS）
3. SSE 生命周期管理（AbortController）
4. 完成后原地更新，不再全量刷新对话区
5. SSE 心跳防代理掐断

## 三、改造范围

### 3.1 后端（src/api/routes/events.py）

#### 3.1.1 `_direct_attempt` 上抛全部中间事件

当前只处理 `text`/`reasoning`→`thinking`/`done`，改为上抛全部事件类型：

```python
# 当前（行 731-746）
async for evt_type, data in _iter_with_timeout(gen, _CONV_NO_OUTPUT_TIMEOUT):
    if evt_type == "text":
        ...
        yield ("text", t)
    elif evt_type == "reasoning":
        yield ("thinking", ...)
    elif evt_type == "done":
        ...
        break

# 改为
async for evt_type, data in _iter_with_timeout(gen, _CONV_NO_OUTPUT_TIMEOUT):
    if evt_type == "text":
        ...
        yield ("text", t)
    elif evt_type == "reasoning":
        yield ("reasoning", ...)       # 保留原始事件类型（不再映射为 thinking）
    elif evt_type == "step_start":
        yield ("step_start", str(data))
    elif evt_type == "step_finish":
        yield ("step_finish", str(data))
    elif evt_type == "tool_use":
        yield ("tool_use", str(data)[:200])
    elif evt_type == "done":
        ...
        break
```

#### 3.1.2 `stream_conversation` SSE 事件对齐 chat.py

```python
# 当前（行 948-956）
async def event_stream():
    async for evt_type, data in _run_conversation(evt, body.message.strip(), username):
        if evt_type == "thinking":
            yield f"data: {json.dumps({'type': 'thinking', 'text': data})}\n\n"
        elif evt_type == "text":
            yield f"data: {json.dumps({'type': 'text', 'text': data})}\n\n"
        elif evt_type == "done":
            yield f"data: {json.dumps({'type': 'done', 'reply': data['reply'], 'messages': data['messages']})}\n\n"

# 改为
async def event_stream():
    # 先发思考中占位
    yield f"data: {json.dumps({'type': '思考中', 'text': ''})}\n\n"
    _last_sse = time.time()
    async for evt_type, data in _run_conversation(evt, body.message.strip(), username):
        _last_sse = time.time()
        if evt_type == "thinking":
            yield f"data: {json.dumps({'type': '思考中', 'text': data})}\n\n"
        elif evt_type == "text":
            yield f"data: {json.dumps({'type': 'text', 'text': data})}\n\n"
        elif evt_type == "reasoning":
            yield f"data: {json.dumps({'type': 'reasoning', 'text': str(data)})}\n\n"
        elif evt_type == "step_start":
            yield f"data: {json.dumps({'type': 'step_start', 'text': str(data)})}\n\n"
        elif evt_type == "step_finish":
            yield f"data: {json.dumps({'type': 'step_finish', 'text': str(data)})}\n\n"
        elif evt_type == "tool_use":
            yield f"data: {json.dumps({'type': 'tool_use', 'text': str(data)[:200]})}\n\n"
        elif evt_type == "done":
            yield f"data: {json.dumps({'type': 'done', 'reply': data['reply'], 'messages': data['messages']})}\n\n"
            return
        # SSE 心跳：idle 15s 发 ping
        if (time.time() - _last_sse) > 15:
            _last_sse = time.time()
            yield f": ping\n\n"
```

> 注：`_run_conversation` 开头的 `yield ("thinking", "正在连接 agent...")` 保留，前端收到 `思考中` 事件时维持三点动画。

#### 3.1.3 `_run_conversation` 中间事件传递

`_run_conversation` 的 direct 模式分支已经 `yield (kind, data)` 透传 `_direct_attempt` 的事件，改造后新增的 step_start/step_finish/reasoning/tool_use 会自动透传。需确认 `kind != "_stuck"` 和 `kind != "_done"` 的 else 分支能正确透传新事件类型（当前已满足）。

### 3.2 前端（src/api/static/js/pages/events.js）

#### 3.2.1 `sendConversation` 函数重写

**当前代码（行 286-336）核心问题：**
- 思考占位用纯文字
- SSE 只处理 text/done
- 无 AbortController
- done 后 loadConversation() 全量刷新

**改造后：**

```javascript
// 新增模块级 AbortController
let _convStreamAbort = null;

function sendConversation() {
  const input = document.getElementById('convInput');
  if (!input || !_convSeq) return;
  const msg = (input.value || '').trim();
  if (!msg) return;
  input.value = '';

  // 中断旧 SSE（切换事件/重复发送）
  if (_convStreamAbort) { _convStreamAbort.abort(); _convStreamAbort = null; }
  const ac = new AbortController();
  _convStreamAbort = ac;

  const messagesEl = document.getElementById('convMessages');
  if (messagesEl) messagesEl.insertAdjacentHTML('beforeend', _convBubble('user', msg));

  const token = localStorage.getItem('token') || '';
  const replyId = 'convReply' + Date.now();
  const holder = document.createElement('div');
  holder.style.display = 'flex';
  holder.style.justifyContent = 'flex-start';
  // 占位改为三点动画（复用 chat-dots CSS）
  holder.innerHTML = `<div id="${replyId}" style="max-width:85%;padding:8px 10px;border-radius:8px;font-size:12px;line-height:1.5;background:var(--bg);color:var(--t);border:1px solid var(--b)"><span class="chat-dots"><span></span><span></span><span></span></span></div>`;
  if (messagesEl) messagesEl.appendChild(holder);
  const replyEl = document.getElementById(replyId);
  if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;

  let acc = '';
  let lastStepText = '';

  fetch('/api/events/' + _convSeq + '/chat/stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    body: JSON.stringify({message: msg}),
    signal: ac.signal,
  }).then(resp => {
    if (!resp.ok) {
      resp.json().then(d => app.showToast((d.detail && d.detail.message) || I18N.t('events.send_failed')))
        .catch(() => app.showToast(I18N.t('events.send_failed')));
      if (replyEl) replyEl.textContent = I18N.t('events.send_failed_paren');
      return;
    }
    if (!resp.body) { loadConversation(); return; }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    function read() {
      return reader.read().then(({done, value}) => {
        if (done) { loadConversation(); return; }
        buf += dec.decode(value, {stream: true});
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const part of parts) {
          if (!part.startsWith('data: ')) continue;
          let evt;
          try { evt = JSON.parse(part.slice(6)); } catch (e) { continue; }

          if (evt.type === '思考中') {
            // 维持三点动画
            if (replyEl) replyEl.innerHTML = '<span class="chat-dots"><span></span><span></span><span></span></span>';
          } else if (evt.type === 'text') {
            acc += evt.text;
            if (replyEl) replyEl.innerHTML = renderMarkdown(acc);
          } else if (evt.type === 'done') {
            // 原地更新最终结果，不再全量 loadConversation
            if (replyEl) replyEl.innerHTML = renderMarkdown(evt.reply || acc);
            _convStreamAbort = null;
            // 后台静默刷新线程消息（不闪烁：只追加 system 消息等）
            setTimeout(() => loadConversation(), 200);
            return;
          } else if (evt.text) {
            // 中间步骤（reasoning/step_start/step_finish/tool_use）— 单行滚动
            lastStepText = evt.text;
            var _prefix = '';
            if (evt.type === 'step_start') _prefix = '▸ ';
            else if (evt.type === 'step_finish') _prefix = '✓ ';
            var _safe = String(_prefix + evt.text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            if (replyEl) replyEl.innerHTML = '<div class="chat-ticker">' + _safe + '</div><span class="chat-dots"><span></span><span></span><span></span></span>';
          }
        }
        if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
        return read();
      });
    }
    return read().catch(() => loadConversation());
  }).catch(e => {
    if (e.name === 'AbortError') return;  // 主动中断
    loadConversation();
  });
}
```

#### 3.2.2 切换事件时中断旧 SSE

在 `renderEventDetail` 或事件列表点击时，调用 abort：

```javascript
// 在 renderEventDetail 开头加入
function renderEventDetail(data) {
  if (_convStreamAbort) { _convStreamAbort.abort(); _convStreamAbort = null; }
  ...
}
```

### 3.3 CSS 依赖

`chat-dots` 和 `chat-ticker` 样式已在 `app.css` 中全局定义（v37），事件详情页加载 app.css，无需额外引入。

## 四、改造文件清单

| 文件 | 改动点 |
|------|--------|
| `src/api/routes/events.py` | `_direct_attempt` 上抛 step_start/step_finish/tool_use/reasoning；`stream_conversation` SSE 事件对齐 + 心跳 |
| `src/api/static/js/pages/events.js` | `sendConversation` 重写：三点动画 + 单行滚动 + AbortController + 原地更新；`renderEventDetail` 加 abort |
| `src/api/static/index.html` | events.js 版本号递增 |

## 五、验证用例

### 5.1 思考占位——三点动画

| 编号 | 用例 | 操作步骤 | 预期结果 |
|------|------|---------|---------|
| T1 | 发送消息后显示三点动画 | 事件详情 → 对话区 → 输入消息 → 发送 | 助手回复区立即显示三点 bounce 动画（chat-dots），不再显示"⏳ 思考中…"文字 |
| T2 | 思考中事件维持动画 | 后端 yield 思考中事件 | 前端维持三点动画，不闪烁 |

### 5.2 中间步骤——单行滚动

| 编号 | 用例 | 操作步骤 | 预期结果 |
|------|------|---------|---------|
| T3 | reasoning 事件单行显示 | Agent 执行过程中产生 reasoning 事件 | 回复区显示一行 reasoning 文本（chat-ticker 样式）+ 下方三点动画 |
| T4 | step_start 带前缀 | Agent 开始执行步骤 | 单行显示 `▸ Step N started...` + 三点动画 |
| T5 | step_finish 带前缀 | Agent 步骤完成 | 单行显示 `✓ Step N done...` + 三点动画 |
| T6 | tool_use 截断显示 | Agent 调用工具（tool_use data 较长） | 单行显示工具名/参数（超 200 字截断）+ 三点动画 |
| T7 | 多步骤滚动替换 | 连续多个中间事件 | 每个新事件替换上一行内容，视觉上像滚动更新 |
| T8 | 超长文本省略号 | 步骤文本超过对话区宽度 | 超出部分用 `…` 截断（text-overflow:ellipsis） |

### 5.3 流式正文

| 编号 | 用例 | 操作步骤 | 预期结果 |
|------|------|---------|---------|
| T9 | text 事件增量追加 | Agent 产出 text chunk | 回复区实时追加文本并 renderMarkdown 渲染 |
| T10 | done 后显示最终结果 | Agent 执行完成 | 回复区显示完整 markdown 渲染结果，中间步骤消失 |

### 5.4 SSE 生命周期管理

| 编号 | 用例 | 操作步骤 | 预期结果 |
|------|------|---------|---------|
| T11 | 切换事件中断旧 SSE | 发送消息后（Agent 仍在执行），点击另一个事件 | 旧 SSE 被 abort 中断，不串话；新事件详情正常加载 |
| T12 | 重复发送中断旧 SSE | 发送消息后（Agent 仍在执行），再次发送新消息 | 旧 SSE 被中断，新消息的 SSE 正常开始 |
| T13 | 主动中断不报错 | AbortController.abort() 触发 | catch 块识别 AbortError 静默返回，不显示错误 |

### 5.5 完成后刷新

| 编号 | 用例 | 操作步骤 | 预期结果 |
|------|------|---------|---------|
| T14 | done 后原地更新 | Agent 回复完成 | 回复区原地 renderMarkdown 更新，不闪屏 |
| T15 | done 后后台静默刷新 | done 后 200ms | loadConversation() 后台拉取最新线程消息（含 system 消息），不覆盖已渲染的回复区 |

### 5.6 SSE 心跳

| 编号 | 用例 | 操作步骤 | 预期结果 |
|------|------|---------|---------|
| T16 | 长时间执行保活 | Agent 执行超过 15s 无事件 | 后端发 `: ping` 心跳，SSE 连接不被代理掐断 |

### 5.7 队列模式兼容

| 编号 | 用例 | 操作步骤 | 预期结果 |
|------|------|---------|---------|
| T17 | 队列模式思考占位 | 队列模式下发送消息 | 先显示三点动画，等待结果返回后显示最终回复 |
| T18 | 队列模式 done | 队列模式任务完成 | done 事件携带 reply，前端正常渲染 |

### 5.8 边界场景

| 编号 | 用例 | 操作步骤 | 预期结果 |
|------|------|---------|---------|
| T19 | 门禁关闭时发送 | Issue 流程已结束（gate closed）→ 发送消息 | 返回 409，前端 Toast 提示"该 Issue 流程已结束" |
| T20 | 空消息发送 | 输入框为空 → 点发送 | 不触发请求（前端 return） |
| T21 | Agent 无输出 | Agent 执行完成但无文本输出 | 显示"（agent 未返回回复，请稍后重试）" |
| T22 | 会话挂起重试 | direct 模式复用的 issue 会话挂起 | 后端自动重置会话并重试一次，前端看到"对话执行异常，正在重置会话重试…" |

## 六、风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| `_direct_attempt` 透传新事件类型可能影响 `_run_conversation` 的 else 分支 | 低 | else 分支 `yield (kind, data)` 已覆盖任意非 `_stuck`/`_done` 事件，无需改动 |
| 前端 `chat-ticker` / `chat-dots` CSS 在事件详情页样式冲突 | 低 | 两个 class 是 chat 专用前缀，事件详情页 DOM 结构独立，冲突概率极低 |
| 队列模式下中间步骤仍不可见 | 中 | 队列模式本质上是一次性返回，要实现中间步骤需改造为 submit/stream 分离（本期不做，标记为后续迭代） |
| loadConversation 延迟刷新可能遗漏 system 消息 | 低 | 200ms 延迟足够后端写库；即使遗漏，下次打开事件详情会全量加载 |

## 七、后续迭代方向

1. **submit/stream 分离**：事件详情对话也改为 submit 返回 task_id + SSE stream 模式，支持断线重连
2. **队列模式中间步骤**：队列模式下通过 task status 轮询 + SSE 重放缓冲，实现中间步骤可见
3. **对话历史增量加载**：当前 loadConversation 全量拉取线程消息，长对话场景可改为增量加载
