# Issue #100: 事件详情页面对话产出能力优化——支持用户清理

## 0. 需求

事件详情页面的"对话产出"功能增加用户清理能力，支持用户手动清除：
1. 事件关联的对话消息（event_thread 记录）
2. 事件关联的 agent 沉淀知识（knowledge.md 文件 + DB 索引）

两个操作独立，用户可以分别清理。

## 1. 现状分析

### 数据存储

| 数据 | 存储位置 | 来源 | 清理范围 |
|------|---------|------|---------|
| 对话消息 | event_thread 表（anchor_seq 关联事件） | 用户发消息 + agent 回复 | 删 event_thread 记录 |
| 📌 留痕 | event_thread 表（role='system'） | agent 沉淀知识/流程操作时自动写 | 删 event_thread 记录（含在对话消息里） |
| 🔄 留痕 | event_thread 表（role='system'） | agent 流程干预时自动写 | 同上 |
| project 级知识 | {repo_base}/project-info/knowledge.md + DB | write_knowledge(scope='project') | 删文件 + DB 记录 + ref |
| issue 级知识 | {repo_base}/issues/{n}/knowledge.md + DB | write_knowledge(scope='issue') | 删文件 + DB 记录 + ref |
| issue_agent 级知识 | {repo_base}/issues/{n}/{agent}/knowledge.md + DB | write_knowledge(scope='issue_agent') | 删文件 + DB 记录 + ref |

### 知识文件格式

- 扩展名 .md，内容是 markdown
- 逐行追加：`- [seq {n} / {agent}] {内容}`
- 一条可能很长（agent 把多段 markdown 分析报告当 content 写入）
- read_knowledge() 返回 `{scope: "文件全文"}`，前端 renderOutcome() 直接 `${knowledge[scope]}` 塞进面板
- 当前展示问题：全文原样塞入 div，没截断没格式化

### 知识 scope 说明

| scope | 展示名 | 文件路径 | 影响范围 | 删除策略 |
|-------|--------|---------|---------|---------|
| project | 仓库级 | project-info/knowledge.md | 所有 Agent 共享 | 二次提示"所有 Agent 共享，确认删除？" |
| issue | Issue 级 | issues/{n}/knowledge.md | 该 Issue 所有 Agent | 直接确认删除 |
| issue_agent | Agent 独享 | issues/{n}/{agent}/knowledge.md | 该 Issue + 该 Agent | 直接确认删除 |

### 当前无清理功能

- 无前端按钮
- 无后端 DELETE 接口
- 知识展示为全文塞入，体验差

### 参考实现

Chat Agent 页面已有完整删除功能：
- 后端：`DELETE /api/chat/conversations/{conv_id}` (chat.py:110)
- 前端：`deleteConversation()` + `_showChatDelPopover()` 确认气泡 (chat.js:261)
- CSS：`.chat-del-overlay` / `.chat-del-pop` 确认气泡样式 (app.css:476-483)

## 2. 改动清单（5 项）

### A. 后端：清理对话接口

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | src/api/routes/events.py | 新增 `DELETE /{seq}/chat` | 删 event_thread WHERE anchor_seq=seq。鉴权 get_current_user + repo 成员校验。不删知识文件 |

```python
@router.delete("/{seq}/chat")
def delete_event_thread(seq: int, user: dict = Depends(get_current_user)):
    evt = _load_event(seq)
    if not evt:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件不存在"})
    _check_repo_member(user, evt["repo_key"])
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM event_thread WHERE anchor_seq=?", (seq,))
        db.commit()
    return {"ok": True, "message": "对话已清理"}
```

### B. 后端：删除知识接口

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 2 | src/api/routes/events.py | 新增 `DELETE /{seq}/knowledge/{scope}` | 删指定 scope 的知识文件 + DB 记录 + ref。scope=project/issue/issue_agent。鉴权 get_current_user + repo 成员校验 |

```python
@router.delete("/{seq}/knowledge/{scope}")
def delete_event_knowledge(seq: int, scope: str, user: dict = Depends(get_current_user)):
    evt = _load_event(seq)
    if not evt:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件不存在"})
    if scope not in ("project", "issue", "issue_agent"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 project/issue/issue_agent"})
    _check_repo_member(user, evt["repo_key"])

    from src.api.knowledge import knowledge_path, _issue_num_from
    from src.session_manager import db, db_lock
    from src.config import WORKSPACE_DIR
    import os

    issue_num = _issue_num_from(evt["issue_id"])
    agent_type = evt["agent_type"]

    # 1. 删工作区文件
    file_path = knowledge_path(evt["repo_key"], scope,
                               agent_type=agent_type if scope == "issue_agent" else "",
                               issue_num=issue_num if scope != "project" else 0)
    if os.path.isfile(file_path):
        os.remove(file_path)

    # 2. 删 OBS 对象（如有）
    try:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            from src.api.knowledge import knowledge_storage_key
            obs_key = knowledge_storage_key(evt["repo_key"], scope,
                                            agent_type=agent_type if scope == "issue_agent" else "",
                                            issue_num=issue_num if scope != "project" else 0)
            storage.delete_object(obs_key)
    except Exception:
        pass

    # 3. 删 DB 记录（agent_shared_knowledge + scene_knowledge_ref）
    with db_lock:
        c = db.cursor()
        # 先查 knowledge_id
        rows = c.execute(
            "SELECT id FROM agent_shared_knowledge WHERE repo_key=? AND scope=? "
            "AND agent_type=? AND issue_num=?",
            (evt["repo_key"], scope,
             agent_type if scope == "issue_agent" else "",
             issue_num if scope != "project" else 0)
        ).fetchall()
        for (kid,) in rows:
            c.execute("DELETE FROM scene_knowledge_ref WHERE knowledge_id=?", (kid,))
        c.execute(
            "DELETE FROM agent_shared_knowledge WHERE repo_key=? AND scope=? "
            "AND agent_type=? AND issue_num=?",
            (evt["repo_key"], scope,
             agent_type if scope == "issue_agent" else "",
             issue_num if scope != "project" else 0)
        )
        db.commit()

    return {"ok": True, "scope": scope, "message": "知识已删除"}
```

**鉴权**：get_current_user + _check_repo_member（仓库成员才能删）
**不删 events 表**：事件本身不删
**scope 区分**：project/issue/issue_agent 分别删除，前端按 scope 调用

### C. 前端：对话产出面板改为按钮式

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 3 | src/api/static/js/pages/events.js | renderOutcome() 改为按钮式展示 + 知识弹窗 | 从全文塞入改为三个按钮（按 scope 有内容才显示），点击弹窗展示 markdown 渲染的知识全文，弹窗底部有删除按钮 |

**改前**：
```javascript
function renderOutcome(knowledge, messages) {
  // knowledge[scope] 全文直接塞进 div
  const kRows = Object.keys(knowledge).map(scope =>
    `<div><b>${scope}</b> · ${knowledge[scope]}</div>`  // 全文
  ).join('');
  // ...
}
```

**改后**：卡片式展示，hover 出 ✕ 直接删除，点击卡片内容区弹窗看全文

```javascript
function renderOutcome(knowledge, messages) {
  const el = document.getElementById('convOutcome');
  if (!el) return;

  var scopeLabels = {project: '仓库级', issue: 'Issue 级', issue_agent: 'Agent 独享'};
  var scopeIcons = {project: '📦', issue: '📋', issue_agent: '🤖'};

  // 知识卡片（按 scope 有内容才显示，hover 出 ✕ 直接删除）
  var kCards = Object.keys(knowledge || {}).map(function(scope) {
    if (!knowledge[scope]) return '';
    var content = knowledge[scope];
    var summary = content.substring(0, 40).replace(/\n/g, ' ');
    var len = content.length;
    return '<div class="k-card" onclick="showKnowledge(event,\'' + scope + '\')">'
      + '<span class="k-card-label">' + scopeIcons[scope] + ' ' + scopeLabels[scope] + '</span>'
      + '<span class="k-card-summary">' + summary + '... (' + len + '字)</span>'
      + '<button class="k-card-x" onclick="event.stopPropagation();quickDelete(\'' + scope + '\')">✕</button>'
      + '</div>';
  }).join('');

  // 流程操作（不变，从 messages 筛选 🔄）
  var flowRows = (messages || []).filter(function(m) {
    return m.role === 'system' && (m.content || '').indexOf('🔄') === 0;
  }).map(function(m) {
    return '<div class="flow-row">' + m.content + '</div>';
  }).join('');

  if (!kCards && !flowRows) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML = '<h3>💬 对话产出</h3>'
    + (kCards ? '<div><b>知识沉淀</b><div class="outcome-btns">' + kCards + '</div></div>' : '')
    + (flowRows ? '<div><b>流程操作</b>' + flowRows + '</div>' : '');
}
```

**两种交互**：
- **hover 出 ✕** → 直接删除（不想看内容就删）
  - project 级 → 二次提示"所有 Agent 共享，确认删除？"
  - issue / issue_agent 级 → 直接删除
- **点击卡片内容区** → 弹窗看全文（markdown 渲染）
  - 弹窗固定高度 75vh，内容区 overflow-y:auto，超出出滚动条
  - 弹窗底部有"删除"按钮（同样 project 二次提示）

### D. 前端：清理对话按钮

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 4 | src/api/static/js/pages/events.js | 对话面板标题栏增加"清理对话"按钮 + 确认气泡 | 复用 Chat Agent 的确认气泡模式。清理后调 loadConversation() 刷新 |

按钮位置：对话面板标题栏（convPanel 的 h3），在全屏按钮旁边。

```javascript
// 标题栏 h3（改后）
<h3>
  ${I18N.t('events.section_conversation', {name: agentName})}
  <span style="display:flex;gap:6px;align-items:center">
    <button onclick="clearConversation(event, ${seq})" ...>${I18N.t('events.clear_conv_btn')}</button>
    <button onclick="toggleConvFullscreen()" ...>${I18N.t('events.fullscreen_btn')}</button>
  </span>
</h3>
```

确认气泡 + 删除逻辑：复用 Chat Agent 的 `.chat-del-overlay` / `.chat-del-pop` CSS。

```javascript
function clearConversation(ev, seq) {
  _showClearConvPopover(ev, seq);
}

function _showClearConvPopover(ev, seq) {
  _closeClearConvPopover();
  // 复用 chat-del-overlay / chat-del-pop CSS
  var overlay = document.createElement('div');
  overlay.className = 'chat-del-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _closeClearConvPopover(); };
  var pop = document.createElement('div');
  pop.className = 'chat-del-pop';
  pop.style.left = Math.min(ev.clientX, window.innerWidth - 220) + 'px';
  pop.style.top = Math.min(ev.clientY, window.innerHeight - 120) + 'px';
  // 标题 + 取消 + 确认按钮（同 Chat Agent 模式）
  // ...
  overlay.appendChild(pop);
  document.body.appendChild(overlay);
  requestAnimationFrame(function() { pop.classList.add('show'); });
}

function _doClearConv(seq) {
  _closeClearConvPopover();
  fetch('/api/events/' + seq + '/chat', { method: 'DELETE', headers: authHeaders() })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        app.showToast(I18N.t('events.clear_conv_done'));
        loadConversation();
      }
    });
}
```

### E. i18n 文案

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 5 | src/api/static/locales/zh/events.json + en/events.json | 新增文案 | 清理对话 + 知识删除 |

新增文案：

中文：
```json
{
  "clear_conv_btn": "清理对话",
  "clear_conv_title": "清除当前事件的对话消息",
  "clear_conv_confirm": "确认清理该事件的所有对话消息？此操作不可撤销。",
  "clear_conv_done": "对话已清理",
  "clear_conv_failed": "清理失败: {{msg}}",
  "delete_knowledge_btn": "删除知识",
  "knowledge_deleted": "知识已删除",
  "knowledge_delete_failed": "删除失败: {{msg}}",
  "knowledge_delete_confirm_project": "此知识为所有 Agent 共享，删除后其他 Agent 也会受影响。确认删除？",
  "knowledge_modal_close": "关闭"
}
```

英文：
```json
{
  "clear_conv_btn": "Clear Chat",
  "clear_conv_title": "Clear conversation messages for this event",
  "clear_conv_confirm": "Clear all conversation messages for this event? This cannot be undone.",
  "clear_conv_done": "Conversation cleared",
  "clear_conv_failed": "Clear failed: {{msg}}",
  "delete_knowledge_btn": "Delete",
  "knowledge_deleted": "Knowledge deleted",
  "knowledge_delete_failed": "Delete failed: {{msg}}",
  "knowledge_delete_confirm_project": "This knowledge is shared by all Agents. Confirm deletion?",
  "knowledge_modal_close": "Close"
}
```

## 3. CSS（新增知识卡片 + 弹窗样式）

在 app.css 追加（确认气泡复用已有 `.chat-del-overlay` / `.chat-del-pop`）：

```css
/* 卡片固定宽度，横向排列自动换行 */
.k-card { position:relative; width:140px; padding:8px 28px 8px 10px; border-radius:8px; border:1px solid var(--b,#d0d0d0); background:rgba(255,255,255,.7); font-size:11px; cursor:pointer; transition:all .15s; line-height:1.4; overflow:hidden; }
.k-card:hover { background:rgba(0,122,255,.06); border-color:#007aff; }
.k-card-label { font-weight:600; display:block; }
.k-card-summary { color:var(--t3,#86868b); font-size:10px; display:block; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.k-card-x { position:absolute; top:5px; right:5px; width:18px; height:18px; border-radius:50%; border:none; background:rgba(0,0,0,.05); color:var(--t3,#86868b); font-size:12px; line-height:18px; text-align:center; cursor:pointer; opacity:0; transition:all .15s; padding:0; }
.k-card:hover .k-card-x { opacity:1; }
.k-card-x:hover { background:#ff3b30; color:#fff; }

/* 知识详情弹窗——固定高度，内容滚动 */
.km-overlay { position:fixed; inset:0; z-index:9998; background:rgba(0,0,0,.25); display:none; align-items:center; justify-content:center; }
.km-overlay.show { display:flex; }
.km-modal { max-width:680px; width:90%; max-height:75vh; height:75vh; background:rgba(255,255,255,.95); backdrop-filter:blur(20px) saturate(180%); -webkit-backdrop-filter:blur(20px) saturate(180%); border-radius:14px; box-shadow:0 12px 40px rgba(0,0,0,.2); display:flex; flex-direction:column; overflow:hidden; }
.km-hdr { padding:16px 20px; border-bottom:1px solid rgba(0,0,0,.06); font-size:14px; font-weight:600; flex-shrink:0; }
.km-body { flex:1; min-height:0; overflow-y:auto; padding:16px 20px; font-size:13px; line-height:1.7; }
.km-footer { padding:12px 20px; border-top:1px solid rgba(0,0,0,.06); display:flex; gap:8px; justify-content:flex-end; flex-shrink:0; }
.km-del { border:none; background:#ff3b30; color:#fff; border-radius:8px; padding:8px 16px; font-size:13px; cursor:pointer; }
.km-del:hover { background:#ff5252; }
.km-close { border:1px solid #d0d0d0; background:transparent; border-radius:8px; padding:8px 16px; font-size:13px; cursor:pointer; }
.km-close:hover { background:#f0f0f0; }
```

## 4. 两个独立操作

| 操作 | 入口 | 后端接口 | 清理范围 |
|------|------|---------|---------|
| 清理对话 | 对话面板标题栏"清理对话"按钮 | DELETE /{seq}/chat | event_thread WHERE anchor_seq=seq |
| 删除知识 | 对话产出面板知识按钮 → 弹窗 → "删除"按钮 | DELETE /{seq}/knowledge/{scope} | knowledge.md 文件 + agent_shared_knowledge + scene_knowledge_ref |

两个操作完全独立：
- 清理对话不删知识文件
- 删除知识不删对话消息
- 两个操作后都调 loadConversation() 刷新页面

## 5. 不做

| 项 | 理由 |
|----|------|
| 删除 events 表记录 | 事件本身不删，只删对话和知识 |
| 支持部分清理对话（只删消息不删留痕） | Issue 要求清理所有对话消息，不区分类型 |
| bot token 鉴权 | 用户手动操作，用 get_current_user + repo 成员校验 |
| 知识内容编辑 | 只支持删除，不支持编辑（编辑在知识库管理页面） |

## 6. 验收标准

- [ ] 对话面板标题栏有"清理对话"按钮
- [ ] 点击清理对话时弹出二次确认气泡
- [ ] 确认后清除该事件的所有对话消息（event_thread 记录）
- [ ] 清理后对话面板刷新（显示"开始对话"提示）
- [ ] 对话产出面板知识沉淀区域改为卡片式（按 scope 有内容才显示）
- [ ] 卡片显示 scope 标签 + 摘要 + 字数
- [ ] 卡片 hover 时右上角出现 ✕ 按钮
- [ ] 点 ✕ 直接删除（issue/issue_agent 级）
- [ ] project 级点 ✕ 时二次提示"所有 Agent 共享，确认删除？"
- [ ] 点击卡片内容区弹出弹窗，markdown 渲染知识全文
- [ ] 弹窗固定高度 75vh，内容区超出出滚动条，弹窗不撑高
- [ ] 弹窗底部有"删除"按钮（project 二次提示，其他直接删）
- [ ] 删除知识后文件 + DB 记录 + ref 记录全部清除
- [ ] 删除后对话产出面板刷新（该 scope 卡片消失）
- [ ] scope 展示名：仓库级 / Issue 级 / Agent 独享
- [ ] 后端两个 DELETE 接口都鉴权（仅仓库成员可操作）
- [ ] i18n 中英文文案完整

## 7. 改动文件

| 文件 | 改动项 |
|------|--------|
| src/api/routes/events.py | #1 DELETE /{seq}/chat + #2 DELETE /{seq}/knowledge/{scope} |
| src/api/static/js/pages/events.js | #3 renderOutcome 改按钮式 + 知识弹窗 + #4 清理对话按钮 + 确认气泡 |
| src/api/static/css/app.css | #3 知识弹窗 CSS + 知识按钮 CSS |
| src/api/static/locales/zh/events.json | #5 新增中文文案 |
| src/api/static/locales/en/events.json | #5 新增英文文案 |

> 注：确认气泡 CSS 复用 Chat Agent 的 `.chat-del-overlay` / `.chat-del-pop`（app.css:476-483），不新增。
