# Issue #51 技术方案：任务结束状态的确认和流程处理

> Issue: https://gitcode.com/grape-dev/grape/issues/51
> 需求：SRE 完成不代表任务完成，任务完成必须开发者真实确认。提供三种确认途径：Issue 评论、Chat Agent 会话、任务中心快捷按钮。

## 1. 现状分析

### 1.1 当前 stage 流转

```
vod → issue → coding → qa → sre → done
                                    ↑
                           SRE 完成后自动设为 done（A2A 交办）
```

**问题**：SRE Agent 完成部署后，A2A 信号触发 `set_issue_stage(repo_key, issue_id, "done")`，任务自动标记完成。但 SRE 完成只意味着"部署动作执行了"，不代表开发者验收通过。

### 1.2 当前任务关闭机制

| 触发源 | 代码路径 | completed_reason |
|--------|----------|------------------|
| Issue closed（webhook） | `close_issue_tasks()` | `issue_closed` |
| Issue 重新分配 | `_close_other_assignee_tasks()` | `reassigned` |
| Bot 接管 | `_close_issue_tasks_bot_takeover()` | `bot_takeover` |
| Issue Agent 关闭 | `close_tasks()` API | `issue_closed` |

**缺失**：没有"开发者主动确认完成"的途径。SRE 完成后 stage=done 是自动的，开发者没有确认环节。

### 1.3 当前 DB 状态分布

| stage | 数量 |
|-------|------|
| vod | 33 |
| issue | 37 |
| coding | 16 |
| qa | 70 |
| sre | 47 |
| done | 10 |

47 个 stage=sre 的任务中，可能很多已经部署完成但开发者未确认验收。

## 2. 设计目标

### 2.1 核心变更

1. **SRE 完成后 stage 不再自动设为 done**——改为 `stage=sre_completed`（新阶段）
2. **新增三种开发者确认途径**：
   - Issue 评论 "/done" 或 "完成任务"
   - Chat Agent 会话中输入 "任务完成"
   - 任务中心详情页"结束任务"快捷按钮
3. **确认后**：stage=done + task status=completed + completed_reason='user_confirmed'

### 2.2 阶段流转变更

```
原：sre → done（自动）
新：sre → sre_completed（自动，SRE 执行完成）→ done（开发者确认）
```

### 2.3 sre_completed 阶段语义

- SRE Agent 已完成部署/运维动作
- 等待开发者验收确认
- 事件不再分发到任何 Agent（与 done 类似，不再触发 pipeline）
- 任务中心看板中显示在 SRE 列，但卡片标注"待确认"状态

## 3. 技术方案

### 3.1 后端改造

#### 3.1.1 新增 `sre_completed` 阶段

**event_pipeline.py** — SRE 完成后的 stage 设置：

```python
# 原：SRE 完成后 A2A 信号 → set_issue_stage("done")
# 新：SRE 完成后 → set_issue_stage("sre_completed")

# event_pipeline.py 中 SRE 完成处理逻辑
if new_status == "completed" and agent_type == "sre":
    # 不再自动 done，改为 sre_completed 等待开发者确认
    set_issue_stage(repo_key, issue_id, "sre_completed", "sre")
    log.info(f"SRE completed → stage=sre_completed (awaiting user confirmation) issue={issue_id}")
```

**_STAGE_EVENT_AGENT_MAP** — 新增 sre_completed 行：

```python
_STAGE_EVENT_AGENT_MAP = {
    ...
    "pull_request_updated": {"vod": "issue", "issue": "issue", "coding": "qa", "qa": "qa", "sre": "sre", "sre_completed": "", "done": ""},
    ...
}
```

`sre_completed` 阶段所有事件返回空字符串——不分发到任何 Agent。

#### 3.1.2 新增 `POST /api/tasks/{task_id}/confirm-done` API

```python
class TaskConfirmBody(BaseModel):
    confirmation: str = "user_confirmed"  # user_confirmed / issue_comment / chat_agent

@router.post("/{task_id}/confirm-done")
async def confirm_task_done(task_id: int, body: TaskConfirmBody, user: dict = Depends(get_current_user)):
    """开发者确认任务完成（Issue #51）。
    
    将 task status → completed，issue_state stage → done，
    completed_reason → 'user_confirmed'。
    """
    with db_lock:
        c = db.cursor()
        # 1. 查 task 的 repo_key + issue_number
        c.execute("SELECT repo_key, issue_number FROM tasks WHERE id=? AND (user_id=? OR user_id=0)", (task_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        repo_key, issue_number = row[0], row[3] if len(row) > 3 else row[1]
        
        # 2. 更新 task 状态
        c.execute(
            "UPDATE tasks SET status='completed', completed_reason=?, completed_at=datetime('now') WHERE id=?",
            (body.confirmation, task_id))
        
        # 3. 更新 issue_state stage → done
        if issue_number:
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, f"#{issue_number}", "done", "sre")
        
        db.commit()
    return {"ok": True, "task_id": task_id, "status": "completed", "stage": "done"}
```

#### 3.1.3 Issue 评论指令识别

**webhook.py** — Issue comment webhook 中检测 "/done" 或 "完成任务"：

```python
# webhook.py issue_comment 处理中
comment_text = payload.get("comment", {}).get("content", "")
if comment_text and any(kw in comment_text.lower() for kw in ["/done", "完成任务", "任务完成", "确认完成"]):
    # 开发者确认完成
    from src.api.routes.tasks import confirm_issue_done
    confirm_issue_done(repo_key, issue_number, confirmed_by="issue_comment")
```

新增函数 `confirm_issue_done`：

```python
def confirm_issue_done(repo_key: str, issue_number: int, confirmed_by: str = "user_confirmed"):
    """Issue 评论确认完成 → 关闭该 issue 所有 active 任务 + stage=done"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE tasks SET status='completed', completed_reason=?, completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
            (confirmed_by, repo_key, issue_number))
        affected = c.rowcount
        db.commit()
    # 更新 stage
    from src.api.session_store import set_issue_stage
    set_issue_stage(repo_key, f"#{issue_number}", "done", "sre")
    log.info(f"Issue #{issue_number} confirmed done by {confirmed_by} ({affected} tasks closed)")
    return affected
```

#### 3.1.4 Chat Agent 指令识别

**chat.py** — 在 `submit_conversation` 的消息处理中检测"任务完成"：

```python
# chat.py submit_conversation 中
if body.message.strip() in ["任务完成", "完成任务", "/done", "确认完成"]:
    # 查当前会话关联的 task
    task_id = _get_conv_task_id(body.conversation_id, user)
    if task_id:
        from src.api.routes.tasks import confirm_task_done
        result = confirm_task_done(task_id, TaskConfirmBody(confirmation="chat_agent"), user)
        return {"type": "done", "text": "✅ 任务已确认完成", "task_id": task_id}
```

### 3.2 前端改造

#### 3.2.1 任务中心详情页 — "结束任务"按钮

在 `_detailHtml(t)` 中，当 `stage === 'sre_completed'` 时显示确认按钮：

```javascript
// tasks.js _detailHtml 函数中
const _canConfirm = t.stage === 'sre_completed' && t.status !== 'completed';
if (_canConfirm) {
  html += `<button class="tsk-btn tsk-btn-primary" onclick="confirmTaskDone(${t.id})" 
    style="margin-top:12px;width:100%;padding:10px;border-radius:10px">
    ✓ ${I18N.t('tasks.confirm_done')||'确认任务完成'}
  </button>`;
}

window.confirmTaskDone = function(taskId) {
  if (!confirm('确认此任务已完成？')) return;
  API.post('/tasks/' + taskId + '/confirm-done', {confirmation: 'user_confirmed'}).then(() => {
    app.showToast('任务已完成');
    loadTasks();
  }).catch(e => app.showToast('操作失败: ' + e.message));
};
```

#### 3.2.2 看板 SRE 列 — "待确认"标识

看板中 `sre_completed` 阶段的任务卡片显示"待确认"标签：

```javascript
// tasks.js _renderBoard 中 stages 数组新增
const stages = ['vod', 'issue', 'coding', 'qa', 'sre', 'sre_completed'];
// sre_completed 列标签：SRE（待确认），颜色用橙色虚线圆点
```

但为了保持 5 列简洁，sre_completed 的任务仍显示在 SRE 列中，但卡片状态标注"待确认"。

#### 3.2.3 Chat Agent — 完成确认回复

Chat Agent 识别到"任务完成"指令后，返回确认消息：

```javascript
// chat.js 中处理 SSE done 事件
if (data.type === 'done' && data.text && data.text.includes('任务已确认完成')) {
  // 刷新任务列表
  if (typeof loadTasks === 'function') setTimeout(loadTasks, 1000);
}
```

### 3.3 国际化

```json
// zh/tasks.json
"confirm_done": "确认任务完成",
"confirm_done_desc": "SRE 已完成部署，请确认任务完成",
"status.tag_pending_confirm": "待确认",
"completed_reason_user_confirmed": "开发者确认",
"completed_reason_issue_comment": "Issue 评论确认",
"completed_reason_chat_agent": "Chat 确认",

// en/tasks.json
"confirm_done": "Confirm Task Done",
"confirm_done_desc": "SRE deployment completed, please confirm",
"status.tag_pending_confirm": "Pending Confirm",
"completed_reason_user_confirmed": "User Confirmed",
"completed_reason_issue_comment": "Issue Comment",
"completed_reason_chat_agent": "Chat Agent",
```

## 4. 数据流

```
SRE Agent 完成
    ↓
stage = sre_completed（不再自动 done）
    ↓
开发者通过 3 种途径之一确认：
├── Issue 评论 "/done" → webhook → confirm_issue_done()
├── Chat Agent "任务完成" → chat.py → confirm_task_done()
└── 任务中心按钮 → API → confirm_task_done()
    ↓
task.status = completed
task.completed_reason = user_confirmed / issue_comment / chat_agent
issue_state.stage = done
```

## 5. 边界情况

| 场景 | 处理 |
|------|------|
| SRE 未完成就确认 | 不允许——只有 stage=sre_completed 时才显示确认按钮 |
| 多任务模式子任务 | 每个子任务独立确认；父任务在所有子任务完成后自动 done |
| 重复确认 | 幂等——task 已 completed 则跳过 |
| Issue closed（非评论确认） | 保持原有 `close_issue_tasks` 逻辑，completed_reason=issue_closed |
| 开发者拒绝验收 | 通过 Issue 评论反馈 → A2A 回流 SRE 修复（stage 回退 sre） |
| sre_completed 阶段新事件 | 不分发到任何 Agent（与 done 相同处理） |

## 6. 实施计划

| 步骤 | 文件 | 改动 |
|------|------|------|
| 1. 新增 sre_completed 阶段 | `event_pipeline.py` | SRE 完成后设 stage=sre_completed；_STAGE_EVENT_AGENT_MAP 新增行 |
| 2. 新增 confirm-done API | `tasks.py` | `POST /{task_id}/confirm-done` + `confirm_issue_done()` 函数 |
| 3. Issue 评论指令识别 | `webhook.py` | comment webhook 检测 /done/完成任务 |
| 4. Chat Agent 指令识别 | `chat.py` | submit_conversation 检测"任务完成" |
| 5. 前端确认按钮 | `tasks.js` | _detailHtml 中 sre_completed 阶段显示按钮 |
| 6. 看板待确认标识 | `tasks.js` | sre_completed 任务卡片显示"待确认" |
| 7. i18n | `zh/en tasks.json` | 新增 confirm_done/pending_confirm 等 key |
| 8. 版本号 | `index.html` | tasks.js bump |
| 9. 测试 | pytest | 新增 test_confirm_task_done |

## 7. 风险评估

- **中风险**：stage 流转变更（sre → sre_completed → done）可能影响已有 pipeline 逻辑，需全面检查 `_STAGE_EVENT_AGENT_MAP` 和 `_resolve_issue_stage_agent` 中对 done 的处理
- **低风险**：新增 API 端点不影响现有功能
- **低风险**：前端确认按钮仅在 sre_completed 阶段显示，不影响其他阶段
- **低风险**：Issue 评论和 Chat 指令识别是附加逻辑，不影响正常消息流

## 8. 兼容性处理

- 已有 stage=done 的 10 条记录保持不变（已经是完成状态）
- 已有 stage=sre 的 47 条记录需要人工判断是否需要改为 sre_completed（提供 DB 迁移脚本可选）
- 新 Issue 从 SRE 完成后自动进入 sre_completed 流程
