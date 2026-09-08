# Issue #51 技术方案：任务结束状态的确认和流程处理（终版 v3）

> Issue: https://gitcode.com/grape-dev/grape/issues/51
> 需求：SRE 完成不代表任务完成，任务完成必须开发者真实确认。提供三种确认途径：Issue 评论、Chat Agent 会话、任务中心快捷按钮。
> 修订：pipeline 节点可配置（最后节点未必是 SRE）；中间节点确认非必选（由 skill 动态判定）；开发者可随时取消/恢复任务。

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

### 1.4 当前 task.status 状态分析

当前 `tasks.status` 有三个值：active / processing / completed。

| 状态 | 含义 | 触发条件 |
|------|------|----------|
| active | 任务已创建，开发者未介入 | `ensure_issue_task()` 创建时默认 |
| processing | 开发者已打开会话介入 | `mark_task_processing_by_conversation()` 在 chat.py 中触发（会话收到第一条消息） |
| completed | 任务已关闭 | 各种 close 函数触发 |

**问题分析**：

1. **active 和 processing 的区分意义不大**——它们都不反映 Agent 的实际执行状态
2. Agent 执行状态在 `agent_tasks` 表中（queued/dispatched/running/completed/failed），是独立的状态机
3. `tasks.status` 实际只反映"开发者是否打开过会话"，不反映 pipeline 进展
4. 缺少 `cancelled` 状态（`_TASK_STATUS_FLOWS` 中定义了但实际未使用）
5. 缺少 `pending_review`（待审核）和 `awaiting_input`（待澄清）状态

**状态迁移规则**（当前代码 `_TASK_STATUS_FLOWS`）：
```python
_TASK_STATUS_FLOWS = {
    "active": ("processing", "cancelled"),
    "processing": ("completed", "cancelled", "active"),
    "completed": (),
    "cancelled": ("active",),
}
```

### 1.5 三套独立的状态机

当前系统存在三套互不关联的状态机：

| 状态机 | 存储位置 | 值 | 反映什么 |
|--------|----------|------|----------|
| task.status | tasks 表 | active/processing/completed | 开发者是否打开过会话 |
| issue_state.stage | issue_state 表 | vod/issue/coding/qa/sre/done | 任务在 pipeline 中的位置 |
| agent_tasks.status | agent_tasks 表 | queued/dispatched/running/completed/failed/cancelled | Agent 执行状态 |

**核心问题**：task.status 与 issue_state.stage 脱节——task 可能是 processing 但 stage 已经到 sre，或者 task 是 active 但 stage 已经到 coding（Agent 自动流转不需要开发者介入）。

## 2. 设计目标

### 2.1 核心原则

1. **pipeline 节点可配置**——最后一个节点未必是 SRE，可以是任意 Agent
2. **最后节点完成后 → pending_review**（等待开发者审核）→ 审核通过才 done
3. **中间节点确认非必选**——由 skills 在执行过程中动态判定是否需要开发者澄清
4. **开发者可随时取消任务**（任何阶段/任何状态），取消后进入 cancelled 态
5. **取消可恢复**——开发者恢复后回到取消前 stage 继续流转

### 2.2 目标 task.status 简化

将 active/processing 合并为 `in_progress`，新增 `awaiting_input`/`pending_review`/`cancelled`：

| 新状态 | 含义 | 对应旧状态 |
|--------|------|-----------|
| pending | 任务创建，等待 pipeline 分发 | active（未打开会话） |
| in_progress | Agent 正在执行（任意 stage） | active + processing 合并 |
| awaiting_input | skill 触发，等待开发者澄清 | 无（新增） |
| pending_review | 最后节点完成，等待开发者审核 | 无（新增） |
| done | 开发者审核通过 | completed |
| cancelled | 开发者取消 | 无（新增，_TASK_STATUS_FLOWS 已定义但未用） |

### 2.3 目标状态流转图

```
                    Issue 创建 (GitCode webhook)
                              │
                              ▼
                    ┌─────────────────┐
                    │  stage=node[0]   │
                    │  task=pending    │
                    └────────┬────────┘
                             │ Agent 开始执行
                             ▼
                    ┌─────────────────┐
                    │  task=in_progress│
                    │  Agent 执行中    │
                    │  (skill 检查)    │
                    └────────┬────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
              skill 判定           skill 判定
              需要澄清             不需要澄清
                    │                 │
                    ▼                 │
           ┌──────────────┐           │
           │awaiting_input │           │
           │(等待开发者)    │          │
           └──────┬───────┘           │
                  │                   │
           开发者回应                  │
                  │                   │
                  ▼                   │
           (继续执行)                  │
                  │                   │
                  └───────┬───────────┘
                          │
                          ▼
                  ┌───────────────┐
                  │node_completed │
                  │(节点完成)      │
                  └───────┬───────┘
                          │
                 ┌────────┴────────┐
                 │                 │
           非最后节点          最后节点
           (A2A 下一节点)        │
                 │              │
                 │              ▼
                 │     ┌──────────────────┐
                 │     │ pending_review   │
                 │     │ (待开发者审核)    │
                 │     └────────┬─────────┘
                 │              │
                 │     ┌────────┴────────┐
                 │     │                 │
                 │   通过              不通过
                 │     │                 │
                 │     ▼            回退到指定节点
                 │  ┌──────────────┐  (A2A 回流)
                 │  │  stage=done  │
                 │  │ task=done    │
                 │  │reason=user_   │
                 │  │  confirmed   │
                 │  └──────────────┘
                 │
                 ▼
           (继续 pipeline 流转)

  ┌──────────────┐    取消（任何状态）    ┌──────────────┐
  │  pending     │ ──────────────────►  │  cancelled   │
  │  in_progress │                      │  (已取消)     │
  │awaiting_input│                      └──────┬───────┘
  │pending_review│                             │
  └──────────────┘                        开发者恢复
                                               │
                                               ▼
                                    (回到取消前 stage 继续流转)
```

### 2.4 skill 触发开发者澄清（非必选，动态判定）

Agent 执行过程中，skill 可以判定是否需要开发者输入：

```
  ┌──────────┐  skill 检查     ┌──────────────────┐
  │executing │ ─────────────► │ need_input?      │
  │(执行中)  │                 │ (skill 自行判定)  │
  └──────────┘                 └────────┬─────────┘
                                        │
                                ┌───────┴───────┐
                                │               │
                              是              否
                                │               │
                                ▼               │
                        ┌──────────────┐       │
                        │awaiting_input│       │
                        │(等待澄清)     │       │
                        └──────┬───────┘       │
                               │               │
                        开发者回应             │
                        (Issue/Chat/按钮)      │
                               │               │
                               ▼               │
                        (澄清完成)             │
                               │               │
                               └───────┬───────┘
                                       │
                                       ▼
                               (继续执行)
```

触发场景示例（由 skill 决定，非必选）：

| stage | skill 触发条件 |
|-------|--------------|
| vod | VOD skill 判定需求模糊 → 触发澄清 |
| issue | Issue skill 判定技术方案有分歧 → 触发澄清 |
| coding | Coding skill 判定实现方向不确定 → 触发澄清 |
| qa | QA skill 判定验收标准不明确 → 触发澄清 |
| sre | SRE skill 判定部署参数需确认 → 触发澄清 |

→ skill 在 SKILL.md 中声明 `need_input` 条件
→ 执行到该条件时 Agent 暂停，标记 `awaiting_input`
→ 开发者通过 Issue 评论 / Chat Agent / 任务中心回应
→ 回复后 Agent 继续执行

## 3. 技术方案

### 3.1 后端改造

#### 3.1.1 新增 `pending_review` 阶段

**event_pipeline.py** — 最后节点完成后不再自动 done，改为 pending_review：

```python
# event_pipeline.py 中最后节点完成处理
if new_status == "completed" and agent_type == _last_node:
    # 不再自动 done，改为 pending_review 等待开发者审核
    set_issue_stage(repo_key, issue_id, "pending_review", agent_type)
    # task.status → pending_review
    _update_task_status(repo_key, issue_id, "pending_review")
    log.info(f"Last node completed → pending_review (awaiting user review) issue={issue_id}")
```

**_STAGE_EVENT_AGENT_MAP** — 新增 pending_review 行：

```python
_STAGE_EVENT_AGENT_MAP = {
    ...
    "pull_request_updated": {..., "pending_review": "", "done": ""},
    ...
}
```

`pending_review` 阶段所有事件返回空字符串——不分发到任何 Agent。

#### 3.1.2 新增 `POST /api/tasks/{task_id}/confirm-done` API

```python
class TaskConfirmBody(BaseModel):
    confirmation: str = "user_confirmed"  # user_confirmed / issue_comment / chat_agent

@router.post("/{task_id}/confirm-done")
async def confirm_task_done(task_id: int, body: TaskConfirmBody, user: dict = Depends(get_current_user)):
    """开发者确认任务完成（Issue #51）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number, status FROM tasks WHERE id=? AND (user_id=? OR user_id=0)",
                  (task_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        repo_key, issue_number, status = row[0], row[1], row[2]
        # 幂等：已 done 则跳过
        if status == 'done':
            return {"ok": True, "already_done": True}
        # 仅 pending_review 阶段可确认
        if status != 'pending_review':
            raise HTTPException(400, "Task is not pending review")

        c.execute(
            "UPDATE tasks SET status='done', completed_reason=?, completed_at=datetime('now') WHERE id=?",
            (body.confirmation, task_id))
        if issue_number:
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, f"#{issue_number}", "done", "user")
        db.commit()
    return {"ok": True, "task_id": task_id, "status": "done"}
```

#### 3.1.3 新增 `POST /api/tasks/{task_id}/cancel` API

```python
@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int, user: dict = Depends(get_current_user)):
    """开发者取消任务（任何阶段可取消）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, repo_key, issue_number FROM tasks WHERE id=? AND (user_id=? OR user_id=0)",
                  (task_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        status, repo_key, issue_number = row
        # 幂等：已 cancelled 则跳过
        if status == 'cancelled':
            return {"ok": True, "already_cancelled": True}
        # 已 done 不可取消
        if status == 'done':
            raise HTTPException(400, "Completed task cannot be cancelled")

        # 终止运行中 Agent 任务
        try:
            from src.runtime.service import cancel_task as _cancel_agent_task
            _cancel_agent_task(task_id)
        except Exception:
            pass

        c.execute(
            "UPDATE tasks SET status='cancelled', completed_reason='user_cancelled', completed_at=datetime('now') WHERE id=?",
            (task_id,))
        # issue_state.stage 保留（快照取消前位置，供恢复使用）
        db.commit()
    return {"ok": True, "task_id": task_id, "status": "cancelled"}
```

#### 3.1.4 新增 `POST /api/tasks/{task_id}/resume` API

```python
@router.post("/{task_id}/resume")
async def resume_task(task_id: int, user: dict = Depends(get_current_user)):
    """开发者恢复已取消的任务。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM tasks WHERE id=? AND (user_id=? OR user_id=0)", (task_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        status = row[0]
        if status != 'cancelled':
            raise HTTPException(400, "Only cancelled task can be resumed")

        # 恢复：status → in_progress，清空 completed_reason/at
        c.execute(
            "UPDATE tasks SET status='in_progress', completed_reason=NULL, completed_at=NULL WHERE id=?",
            (task_id,))
        db.commit()
    # issue_state.stage 保持取消前的值，重新进入 pipeline 分发
    return {"ok": True, "task_id": task_id, "status": "in_progress"}
```

#### 3.1.5 Issue 评论指令识别

**webhook.py** — Issue comment webhook 中检测指令：

```python
comment_text = payload.get("comment", {}).get("content", "")
comment_lower = comment_text.lower()

# 确认完成
if any(kw in comment_lower for kw in ["/done", "完成任务", "任务完成", "确认完成"]):
    from src.api.routes.tasks import confirm_issue_done
    confirm_issue_done(repo_key, issue_number, confirmed_by="issue_comment")

# 取消任务
elif any(kw in comment_lower for kw in ["/cancel", "取消任务"]):
    from src.api.routes.tasks import cancel_issue_task
    cancel_issue_task(repo_key, issue_number)

# 恢复任务
elif any(kw in comment_lower for kw in ["/resume", "恢复任务"]):
    from src.api.routes.tasks import resume_issue_task
    resume_issue_task(repo_key, issue_number)
```

#### 3.1.6 Chat Agent 指令识别

**chat.py** — `submit_conversation` 中检测指令：

```python
msg = body.message.strip()
if msg in ["任务完成", "完成任务", "/done", "确认完成"]:
    # 确认完成
    task_id = _get_conv_task_id(body.conversation_id, user)
    if task_id:
        confirm_task_done(task_id, TaskConfirmBody(confirmation="chat_agent"), user)
        return {"type": "done", "text": "✅ 任务已确认完成"}
elif msg in ["取消任务", "/cancel"]:
    task_id = _get_conv_task_id(body.conversation_id, user)
    if task_id:
        cancel_task(task_id, user)
        return {"type": "done", "text": "任务已取消"}
elif msg in ["恢复任务", "/resume"]:
    task_id = _get_conv_task_id(body.conversation_id, user)
    if task_id:
        resume_task(task_id, user)
        return {"type": "done", "text": "任务已恢复"}
```

### 3.2 前端改造

#### 3.2.1 任务中心详情页 — 操作按钮

根据 task.status 动态显示：

```javascript
// tasks.js _detailHtml 函数中
const _canConfirm = t.status === 'pending_review';
const _canCancel = !['done', 'cancelled'].includes(t.status);
const _canResume = t.status === 'cancelled';

if (_canConfirm) {
  html += `<button onclick="confirmTaskDone(${t.id})">✓ 确认任务完成</button>`;
}
if (_canCancel) {
  html += `<button onclick="cancelTask(${t.id})">✕ 取消任务</button>`;
}
if (_canResume) {
  html += `<button onclick="resumeTask(${t.id})">↻ 恢复任务</button>`;
}
```

#### 3.2.2 看板卡片状态标识

| task.status | 卡片标识 |
|-------------|---------|
| in_progress | 正常显示（当前 stage 对应列） |
| awaiting_input | 黄色"待澄清"标签 |
| pending_review | 橙色"待审核"标签 |
| cancelled | 灰色"已取消"标签（列内灰显） |
| done | 不在看板显示 |

#### 3.2.3 任务列表 Tab 调整

```
全部 | 进行中 | 待审核 | 已完成 | 已取消
```

### 3.3 国际化

```json
// zh/tasks.json 新增
"confirm_done": "确认任务完成",
"cancel_task": "取消任务",
"resume_task": "恢复任务",
"status.tag_pending_review": "待审核",
"status.tag_awaiting_input": "待澄清",
"status.tag_cancelled": "已取消",
"status.tag_in_progress": "进行中",
"completed_reason_user_confirmed": "开发者确认",
"completed_reason_issue_comment": "Issue 评论确认",
"completed_reason_chat_agent": "Chat 确认",
"completed_reason_user_cancelled": "开发者取消",
"tab.in_progress": "进行中",
"tab.pending_review": "待审核",
"tab.cancelled": "已取消"

// en/tasks.json 新增
"confirm_done": "Confirm Done",
"cancel_task": "Cancel Task",
"resume_task": "Resume Task",
"status.tag_pending_review": "Pending Review",
"status.tag_awaiting_input": "Awaiting Input",
"status.tag_cancelled": "Cancelled",
"status.tag_in_progress": "In Progress",
"completed_reason_user_confirmed": "User Confirmed",
"completed_reason_issue_comment": "Issue Comment",
"completed_reason_chat_agent": "Chat Agent",
"completed_reason_user_cancelled": "User Cancelled",
"tab.in_progress": "In Progress",
"tab.pending_review": "Pending Review",
"tab.cancelled": "Cancelled"
```

## 4. 数据流

### 4.1 确认完成

```
最后节点完成
    ↓
stage = pending_review（不再自动 done）
task.status = pending_review
    ↓
开发者通过 3 种途径之一确认：
├── Issue 评论 "/done" → webhook → confirm_issue_done()
├── Chat Agent "任务完成" → chat.py → confirm_task_done()
└── 任务中心按钮 → API → confirm_task_done()
    ↓
task.status = done
task.completed_reason = user_confirmed / issue_comment / chat_agent
issue_state.stage = done
```

### 4.2 取消/恢复

```
取消：
开发者 → POST /api/tasks/{id}/cancel
    → task.status = cancelled
    → task.completed_reason = 'user_cancelled'
    → cancel_task() 终止运行中 Agent
    → issue_state.stage 保留（快照）

恢复：
开发者 → POST /api/tasks/{id}/resume
    → task.status = in_progress
    → task.completed_reason = NULL
    → issue_state.stage 恢复
    → 重新进入 pipeline 分发
```

## 5. 目标状态流转总结

```
必选流程：节点执行 → ... → 最后节点完成 → pending_review → 开发者审核 → done
可选流程：任何节点执行中 → skill 触发 → awaiting_input → 开发者澄清 → 继续
取消流程：任何状态 → cancelled → 恢复 → in_progress（回到取消前 stage）
```

### 完整状态机

```
  pending ──► in_progress ──► node_completed ──► pending_review ──► done
     ▲            │                                   │
     │       skill 触发                          不通过 → 回退节点
     │            │
     │            ▼
     │      awaiting_input
     │            │
     │      开发者回应
     │            │
     │            ▼
     │      (继续 in_progress)
     │
     │  恢复
     │ ◄──────────┐
     │             │
  cancelled ◄─────┘ 取消（任何状态）
```

### completed_reason 完整值

| 值 | 来源 |
|----|------|
| user_confirmed | 开发者审核通过（任务中心按钮） |
| issue_comment | Issue 评论确认（"/done"） |
| chat_agent | Chat Agent 确认（"任务完成"） |
| issue_closed | Issue 关闭（webhook） |
| reassigned | 重新分配 |
| bot_takeover | Bot 接管 |
| user_cancelled | 开发者取消 |

## 6. 边界情况

| 场景 | 处理 |
|------|------|
| issue_state 无记录（新 Issue） | stage 默认第一个节点 |
| 取消已 done 的任务 | 不允许（已完成不可取消） |
| 恢复已 done 的任务 | 不允许（已完成不可恢复） |
| 重复取消/恢复 | 幂等（已 cancelled/active 则跳过） |
| 取消时 Agent 正在运行 | cancel_task() 终止 + 标记 cancelled |
| 恢复后 Agent 重新执行 | 重新派发到取消前 stage 对应的 Agent |
| 多任务模式子任务取消 | 只取消该子任务，不影响父任务和其他子任务 |
| pending_review 取消 | 允许（审核阶段也可取消） |
| awaiting_input 取消 | 允许（等待澄清阶段也可取消） |
| skill 不触发澄清 | 正常流转，不经过 awaiting_input |
| 最后节点不是 SRE | pipeline 配置决定，pending_review 逻辑通用 |

## 7. 与原方案差异说明

| 原方案 | 终版 v3 | 原因 |
|--------|---------|------|
| 硬编码 sre_completed 阶段 | 通用 pending_review（最后节点完成后触发） | pipeline 节点可配置 |
| 中间节点确认是必选步骤 | 非必选，由 skill 动态判定 | 用户反馈 |
| 无取消/恢复机制 | 新增 cancelled 状态 + cancel/resume API | 用户要求 |
| active/processing 两状态 | 合并为 in_progress + 新增 awaiting_input/pending_review | 状态语义不清晰 |
| 三套状态机独立 | task.status 与 issue_state.stage 对齐 | 消除状态脱节 |

## 8. 实施计划

| 步骤 | 文件 | 改动 |
|------|------|------|
| 1. 新增 pending_review 阶段 | `event_pipeline.py` | 最后节点完成后设 stage=pending_review |
| 2. 新增 confirm-done API | `tasks.py` | `POST /{task_id}/confirm-done` |
| 3. 新增 cancel API | `tasks.py` | `POST /{task_id}/cancel` |
| 4. 新增 resume API | `tasks.py` | `POST /{task_id}/resume` |
| 5. Issue 评论指令识别 | `webhook.py` | 检测 /done /cancel /resume |
| 6. Chat Agent 指令识别 | `chat.py` | 检测"任务完成"/"取消任务"/"恢复任务" |
| 7. 前端操作按钮 | `tasks.js` | 确认/取消/恢复按钮 |
| 8. 看板状态标识 | `tasks.js` | pending_review/awaiting_input/cancelled 标签 |
| 9. 任务列表 Tab | `tasks.js` | 新增"待审核"/"已取消" Tab |
| 10. i18n | `zh/en tasks.json` | 新增所有 key |
| 11. 版本号 | `index.html` | tasks.js bump |
| 12. 测试 | pytest | 新增 test_confirm/cancel/resume |

## 9. 风险评估

- **中风险**：stage 流转变更（最后节点 → pending_review → done）需全面检查 `_STAGE_EVENT_AGENT_MAP` 和 `_resolve_issue_stage_agent`
- **中风险**：task.status 从 active/processing 迁移到新状态需要 DB 数据迁移
- **低风险**：新增 API 端点不影响现有功能
- **低风险**：前端操作按钮仅在特定 status 显示
- **低风险**：Issue 评论和 Chat 指令识别是附加逻辑

## 10. 兼容性处理

- 已有 stage=done 的 10 条记录保持不变
- 已有 active/processing 状态的 task 可选迁移到 in_progress（或保持兼容映射）
- `_TASK_STATUS_FLOWS` 更新为新状态迁移规则
- skill 的 `need_input` 机制是增量——不影响不声明 need_input 的 skill

## 11. 验证用例

### 11.1 API 端点验证

| 用例 | API | 预期 | 验证方法 |
|------|-----|------|----------|
| TC-01 confirm-done 端点存在 | POST /api/tasks/99999/confirm-done | 404 (task not found) | 端点注册成功 |
| TC-02 cancel 端点存在 | POST /api/tasks/99999/cancel | 404 (task not found) | 端点注册成功 |
| TC-03 resume 端点存在 | POST /api/tasks/99999/resume | 404 (task not found) | 端点注册成功 |
| TC-04 confirm-done 幂等 | POST /api/tasks/{已completed的id}/confirm-done | 200 {already_done: true} | 不重复操作 |
| TC-05 cancel 幂等 | POST /api/tasks/{已cancelled的id}/cancel | 200 {already_cancelled: true} | 不重复操作 |
| TC-06 cancel 已完成任务 | POST /api/tasks/{已completed的id}/cancel | 400 (已完成不可取消) | 边界保护 |
| TC-07 resume 非取消任务 | POST /api/tasks/{active的id}/resume | 400 (仅已取消可恢复) | 边界保护 |

### 11.2 状态流转验证

| 用例 | 初始状态 | 操作 | 预期结果 |
|------|----------|------|----------|
| TC-08 最后节点完成→pending_review | stage=sre | A2A done 信号 | stage=pending_review |
| TC-09 pending_review 事件不分发 | stage=pending_review | issue_comment webhook | 不创建新 Agent 任务 |
| TC-10 确认完成→done | stage=pending_review | POST confirm-done | stage=done, status=completed |
| TC-11 取消任务→cancelled | status=active | POST cancel | status=cancelled, reason=user_cancelled |
| TC-12 恢复任务→active | status=cancelled | POST resume | status=active, reason=NULL |
| TC-13 取消终止 Agent | status=processing, Agent running | POST cancel | cancel_task() 调用, Agent 终止 |
| TC-14 恢复后 stage 保留 | stage=coding, 取消后恢复 | POST resume | stage 仍为 coding |

### 11.3 指令识别验证

| 用例 | 输入 | 触发位置 | 预期 |
|------|------|----------|------|
| TC-15 Issue 评论 "/done" | Issue comment "/done" | webhook → event_pipeline | confirm_issue_done() 调用 |
| TC-16 Issue 评论 "完成任务" | Issue comment "完成任务" | webhook → event_pipeline | confirm_issue_done() 调用 |
| TC-17 Issue 评论 "/cancel" | Issue comment "/cancel" | webhook → event_pipeline | cancel_issue_task() 调用 |
| TC-18 Issue 评论 "/resume" | Issue comment "/resume" | webhook → event_pipeline | resume_issue_task() 调用 |
| TC-19 Chat "任务完成" | Chat message "任务完成" | chat.py send_message | confirm_issue_done() + 回复 |
| TC-20 Chat "取消任务" | Chat message "取消任务" | chat.py send_message | cancel_issue_task() + 回复 |
| TC-21 Chat "恢复任务" | Chat message "恢复任务" | chat.py send_message | resume_issue_task() + 回复 |
| TC-22 Chat 普通消息不触发 | Chat message "你好" | chat.py send_message | 正常流程，不触发指令 |
| TC-23 系统回帖不触发 | <!-- grape-auto-comment --> | event_pipeline | 跳过（原有逻辑） |

### 11.4 前端验证

| 用例 | 条件 | 预期 UI |
|------|------|---------|
| TC-24 确认完成按钮 | stage=pending_review, status≠completed | 显示"确认任务完成"按钮 |
| TC-25 取消按钮 | status≠completed且≠cancelled | 显示"取消任务"按钮 |
| TC-26 恢复按钮 | status=cancelled | 显示"恢复任务"按钮 |
| TC-27 无按钮 | status=completed | 不显示任何操作按钮 |
| TC-28 确认成功 toast | 点击确认完成 | showToast + loadTasks 刷新 |

### 11.5 pytest 单元测试

| 用例 | 测试文件 | 测试函数 | 验证点 |
|------|----------|----------|--------|
| TC-29 | test_issue_lifecycle_routing.py | test_pending_review_no_dispatch | pending_review 阶段事件不分发 |
| TC-30 | test_issue_lifecycle_routing.py | test_last_node_to_pending_review | 最后节点完成→pending_review |
| TC-31 | test_issue_lifecycle_routing.py | test_pr_stage_pending_review_empty | PR updated + pending_review → 空 |

