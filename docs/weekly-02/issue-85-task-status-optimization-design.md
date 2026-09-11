# Issue #85 技术方案：任务状态扭转分析与优化

> Issue: https://gitcode.com/grape-dev/grape/issues/85
> 需求：对任务中心中任务各状态进行分析优化，当前很多任务未按照规划的状态进行扭转。比如任务没有经过待审核就进入完成，需要开发者澄清的没有进入澄清状态。分析9月10日的任务状态扭转情况，给出优化方案。

## 1. 现状分析

### 1.1 两套状态体系

当前系统存在两套独立的状态体系，它们之间的联动不完整：

| 体系 | 字段位置 | 状态值 | 管理方式 |
|------|----------|--------|----------|
| 任务状态 (task.status) | `tasks` 表 | `active` / `processing` / `completed` / `cancelled` | `_TASK_STATUS_FLOWS` 规则校验 |
| 流水线阶段 (issue_state.stage) | `issue_state` 表 | `vod` / `issue` / `coding` / `qa` / `sre` / `pending_review` / `done` | A2A 交办时 `set_issue_stage()` |

前端看板 `_mapStatus()` 将两套状态合并为 6 列：
```
pending → in_progress → awaiting_input → pending_review → done → cancelled
```

### 1.2 当前状态扭转路径

#### 1.2.1 task.status 流转规则（_TASK_STATUS_FLOWS）

```python
_TASK_STATUS_FLOWS = {
    "active": ("processing", "cancelled"),
    "processing": ("completed", "cancelled", "active"),
    "completed": (),
    "cancelled": ("active",),
}
```

**问题**：`awaiting_input` 状态在前端看板中存在（有列定义、有颜色、有标签），但后端 `_TASK_STATUS_FLOWS` 中完全没有此状态。即后端永远不会将 task.status 设为 `awaiting_input`。

#### 1.2.2 issue_state.stage 流转路径

```
vod → issue → coding → qa → sre → pending_review → done
                                       ↑
                              SRE(或最后节点)完成后自动设为 pending_review
```

stage 流转由 A2A 交办驱动（`webhook.py` 的 A2A 路由），agent 完成后由 `event_pipeline.py` 或 `runtime/service.py` 推进。

### 1.3 核心问题定位

#### 问题一：任务跳过 pending_review 直接 completed

**根因**：`close_issue_tasks()` 函数（tasks.py:221-261）在 Issue closed 时，如果 `stage` 已经是 `done` 或 `pending_review`，直接将 task.status 设为 `completed`：

```python
if _stage in ("done", "pending_review"):
    c.execute("UPDATE tasks SET status='completed', completed_reason='issue_closed', ...")
```

这意味着即使开发者尚未确认验收，只要 Issue 被 close（可能是 bot 自动关闭），任务就跳过待审核直接完成。

此外，`confirm_task_done()` API 虽然设计了 pending_review 校验（line 604），但允许从 `sre`、`sre_completed` 阶段直接确认，绕过了 pending_review 阶段。

**实际场景**：
1. SRE Agent 部署完成 → stage 设为 `pending_review`
2. Issue 被 bot 自动 close（或被其他人 close）→ `close_issue_tasks()` 触发
3. task.status 直接从 `active/processing` 跳到 `completed`，completed_reason=`issue_closed`
4. 开发者从未审核过

#### 问题二：需要澄清的任务未进入 awaiting_input 状态

**根因**：`awaiting_input` 状态在前端定义了完整的 UI 支持（看板列、颜色 #f59e0b、标签"待澄清"），但后端代码中：

1. `_TASK_STATUS_FLOWS` 没有 `awaiting_input` 状态定义
2. 没有任何代码路径会将 task.status 设为 `awaiting_input`
3. agent 发送澄清消息时，只是发 Issue 评论和写入会话，不修改 task.status

**实际场景**：
1. VOD/Issue Agent 分析 Issue 后发现需要开发者补充信息
2. Agent 通过技能 curl 在 GitCode Issue 上发评论（澄清追问）
3. task.status 保持 `active` 或 `processing` 不变
4. 任务中心看板上该任务显示在"待处理"或"进行中"列，而非"待澄清"列
5. 开发者可能忽略需要回复的澄清消息

#### 问题三：task.status 与 issue_state.stage 联动缺失

**根因**：两套状态体系的更新路径完全独立：

| 操作 | task.status 更新 | issue_state.stage 更新 |
|------|------------------|------------------------|
| A2A 交办 | 不更新 | `set_issue_stage()` |
| Agent 完成 | `update_task_status()` | `set_issue_stage("pending_review")` |
| Issue closed | `close_issue_tasks()` | 条件性更新 |
| 开发者确认 | `confirm_task_done()` | `set_issue_stage("done")` |
| Agent 澄清 | **不更新** | 不更新 |

task.status 的 `active/processing/completed` 无法表达 issue_state.stage 的 `vod/issue/coding/qa/sre` 细粒度阶段。前端通过 `_mapStatus()` 桥接两套状态，但桥接逻辑只读不写，导致状态不一致。

### 1.4 9月10日任务状态扭转分析

基于代码路径分析，9月10日可能存在的异常扭转路径：

| 异常路径 | 触发条件 | 预期路径 | 实际路径 |
|----------|----------|----------|----------|
| 跳过待审核 | Issue 被 close + stage=pending_review | pending_review → 开发者确认 → done | pending_review → 直接 completed |
| 跳过待审核 | SRE 完成 + Issue Agent 自动关闭 | sre → pending_review → 等待确认 | sre → pending_review → issue_closed → completed |
| 未进入澄清 | Agent 发澄清评论 | active → awaiting_input → 开发者回复 → processing | active → 保持 active |
| 直接完成 | Bot 接管 + stage=done | bot_takeover → pending_review → 确认 | bot_takeover → 直接 completed |
| 状态不一致 | A2A 交办推进 stage | task.status 同步反映阶段 | task.status 保持 active 不变 |

## 2. 优化方案

### 2.1 引入 awaiting_input 状态（后端落地）

#### 2.1.1 扩展 _TASK_STATUS_FLOWS

```python
_TASK_STATUS_FLOWS = {
    "active": ("processing", "awaiting_input", "cancelled"),
    "processing": ("awaiting_input", "completed", "cancelled", "active"),
    "awaiting_input": ("processing", "cancelled"),  # 开发者回复后回到 processing
    "completed": (),
    "cancelled": ("active",),
}
```

#### 2.1.2 Agent 澄清时设置 awaiting_input

在 `event_pipeline.py` 的 `_handle_exec_agent_complete()` 中，检测 agent 输出包含澄清意图时，将 task.status 设为 `awaiting_input`：

```python
# 判断澄清意图：agent 输出包含"澄清""需要""请问""？"等关键词
_AWAITING_INPUT_MARKERS = ["澄清", "需要确认", "请问", "请提供", "需要补充", "请回复"]

def _detect_awaiting_input(result_text: str) -> bool:
    """检测 agent 输出是否包含澄清意图"""
    if not result_text:
        return False
    return any(marker in result_text for marker in _AWAITING_INPUT_MARKERS)
```

在 agent 完成回调中（event_pipeline.py ~line 2126 附近）：

```python
if new_status == "completed" and result_text and issue_id:
    if _detect_awaiting_input(result_text):
        # 设置 task.status = awaiting_input（而非保持 active/processing）
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE tasks SET status='awaiting_input' "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
                (repo_key, _issue_num_from(issue_id))
            )
            db.commit()
        log.info(f"Agent clarification detected → task awaiting_input issue={issue_id}")
```

#### 2.1.3 开发者回复澄清后恢复 processing

在 `reply_to_clarification()` API（tasks.py:1223）中，回复后将 task.status 恢复为 `processing`：

```python
# 在 reply_to_clarification 成功后追加
with db_lock:
    c = db.cursor()
    c.execute(
        "UPDATE tasks SET status='processing' "
        "WHERE repo_key=? AND issue_number=? AND status='awaiting_input'",
        (repo_key, issue_number)
    )
    db.commit()
log.info(f"Clarification replied → task back to processing issue=#{issue_number}")
```

### 2.2 强化 pending_review 强制校验

#### 2.2.1 修正 close_issue_tasks() 跳过 pending_review

将 `close_issue_tasks()` 中 stage=pending_review 时的行为从直接 completed 改为保持 pending_review：

```python
# 修改前（line 242-250）：
if _stage in ("done", "pending_review"):
    c.execute("UPDATE tasks SET status='completed', ...")

# 修改后：
if _stage == "done":
    # stage=done 表示开发者已确认 → 直接 completed
    c.execute("UPDATE tasks SET status='completed', ...")
elif _stage == "pending_review":
    # stage=pending_review → 保持，不自动 completed
    # 只更新 task.status 为 processing（如果还是 active）
    c.execute(
        "UPDATE tasks SET status='processing', completed_reason=NULL "
        "WHERE repo_key=? AND issue_number=? AND status='active'",
        (repo_key, issue_number)
    )
```

#### 2.2.2 收紧 confirm_task_done() 阶段校验

```python
# 修改前（line 604）：
if _stage not in ("pending_review", "sre_completed", "sre", "done"):
    raise HTTPException(...)

# 修改后：只允许 pending_review 和 done 阶段确认
if _stage not in ("pending_review", "done"):
    raise HTTPException(status_code=400, detail={
        "code": 400,
        "message": "任务尚未到待审核阶段，当前阶段：" + _stage
    })
```

#### 2.2.3 Bot 接管路径同步修正

`_close_issue_tasks_bot_takeover()` 中同样去掉 pending_review 直接 completed：

```python
# 修改后：pending_review 阶段不自动 completed
if _stage == "done":
    c.execute("UPDATE tasks SET status='completed', ...")
elif _stage == "pending_review":
    # 保持 pending_review，不自动完成
    c.execute("UPDATE tasks SET status='processing' WHERE ... AND status='active'")
```

### 2.3 统一状态映射（task.status ↔ issue_state.stage）

#### 2.3.1 新增 stage → task.status 同步机制

在 `set_issue_stage()` 函数中，当 stage 推进时同步更新 task.status：

```python
def set_issue_stage(repo_key: str, issue_id: str, stage: str, owner_agent: str,
                    a2a_seq: int | None = None, assignee: str = ""):
    # ... 原有 upsert 逻辑 ...
    
    # 新增：同步 task.status
    _STAGE_TO_TASK_STATUS = {
        "vod": "processing",       # VOD 分析中
        "issue": "processing",     # Issue Agent 处理中
        "coding": "processing",    # Coding 执行中
        "qa": "processing",        # QA 测试中
        "sre": "processing",       # SRE 部署中
        "pending_review": "pending_review",  # 待审核（新增 task.status 值）
        "done": "completed",       # 已完成
    }
    _task_status = _STAGE_TO_TASK_STATUS.get(stage)
    if _task_status:
        try:
            with db_lock:
                c = db.cursor()
                _issue_num = int(issue_id.lstrip("#"))
                c.execute(
                    "UPDATE tasks SET status=? "
                    "WHERE repo_key=? AND issue_number=? AND status NOT IN ('completed','cancelled','awaiting_input')",
                    (_task_status, repo_key, _issue_num)
                )
                db.commit()
        except Exception as _e:
            log.warning(f"set_issue_stage: sync task.status failed: {_e}")
```

#### 2.3.2 扩展 _TASK_STATUS_FLOWS 增加 pending_review

```python
_TASK_STATUS_FLOWS = {
    "active": ("processing", "awaiting_input", "cancelled"),
    "processing": ("awaiting_input", "pending_review", "completed", "cancelled", "active"),
    "awaiting_input": ("processing", "cancelled"),
    "pending_review": ("completed", "cancelled"),  # 待审核 → 完成/取消
    "completed": (),
    "cancelled": ("active",),
}
```

#### 2.3.3 更新前端 _mapStatus()

前端已有的 `_mapStatus()` 已经支持 pending_review 列，但需要处理新增的 `task.status=pending_review` 情况：

```javascript
// 修改前（line 330-338）：
function _mapStatus(t) {
    if (t.status === 'cancelled') return 'cancelled';
    if (t.status === 'completed' || t.stage === 'done') return 'done';
    if (t.stage === 'pending_review') return 'pending_review';
    if (t.status === 'awaiting_input') return 'awaiting_input';
    // ...
}

// 修改后：增加 task.status === 'pending_review' 判断
function _mapStatus(t) {
    if (t.status === 'cancelled') return 'cancelled';
    if (t.status === 'completed' || t.stage === 'done') return 'done';
    if (t.status === 'pending_review' || t.stage === 'pending_review') return 'pending_review';
    if (t.status === 'awaiting_input') return 'awaiting_input';
    // ...
}
```

### 2.4 看板列状态映射统一

当前看板 6 列与 task.status 的映射关系（优化后）：

| 看板列 | task.status | issue_state.stage | 触发条件 |
|--------|-------------|-------------------|----------|
| 待处理 (pending) | `active` | 无 issue_state | 任务刚创建，agent 尚未启动 |
| 进行中 (in_progress) | `processing` | vod/issue/coding/qa/sre | agent 正在执行 |
| 待澄清 (awaiting_input) | `awaiting_input` | 任意（agent 发了澄清评论） | agent 需要开发者补充信息 |
| 待审核 (pending_review) | `pending_review` | `pending_review` | 最后节点完成，等待开发者验收 |
| 已完成 (done) | `completed` | `done` | 开发者确认完成 |
| 已取消 (cancelled) | `cancelled` | 任意 | 开发者取消 |

## 3. 实施计划

### 3.1 代码改动清单

| 序号 | 文件 | 改动内容 | 优先级 |
|------|------|----------|--------|
| 1 | `src/api/routes/tasks.py` | 扩展 `_TASK_STATUS_FLOWS` 增加 `awaiting_input` 和 `pending_review` | P0 |
| 2 | `src/api/routes/tasks.py` | 修正 `close_issue_tasks()` 不再跳过 pending_review | P0 |
| 3 | `src/api/routes/tasks.py` | 收紧 `confirm_task_done()` 阶段校验 | P0 |
| 4 | `src/api/routes/tasks.py` | 修正 `_close_issue_tasks_bot_takeover()` 同理 | P0 |
| 5 | `src/api/routes/tasks.py` | `reply_to_clarification()` 回复后恢复 processing | P1 |
| 6 | `src/api/event_pipeline.py` | 新增 `_detect_awaiting_input()` + agent 完成后设置 awaiting_input | P1 |
| 7 | `src/api/session_store.py` | `set_issue_stage()` 同步 task.status | P1 |
| 8 | `src/api/static/js/pages/tasks.js` | `_mapStatus()` 增加 `task.status==='pending_review'` | P2 |
| 9 | `src/api/routes/tasks.py` | `change_task_status()` API 校验新增状态 | P2 |

### 3.2 数据迁移

对已有的不一致状态数据进行修正：

```sql
-- 1. stage=pending_review 但 task.status=completed 的 → 回退为 pending_review
UPDATE tasks SET status='pending_review', completed_reason=NULL, completed_at=NULL
WHERE issue_number IN (
    SELECT CAST(issue_id.lstrip('#') AS INTEGER) FROM issue_state WHERE stage='pending_review'
)
AND status='completed' AND completed_reason='issue_closed';

-- 2. stage=done 但 task.status≠completed 的 → 同步为 completed
UPDATE tasks SET status='completed', completed_reason='auto_sync'
WHERE issue_number IN (
    SELECT CAST(issue_id.lstrip('#') AS INTEGER) FROM issue_state WHERE stage='done'
)
AND status IN ('active','processing','pending_review');
```

### 3.3 测试用例

| 用例 | 操作 | 预期 task.status | 预期 issue_state.stage |
|------|------|------------------|------------------------|
| TC-01 | Issue 创建 → VOD 分析 | active → processing | vod |
| TC-02 | VOD 发澄清评论 | processing → awaiting_input | vod |
| TC-03 | 开发者回复澄清 | awaiting_input → processing | vod |
| TC-04 | A2A 交办 coding | processing | coding |
| TC-05 | SRE 完成部署 | processing → pending_review | pending_review |
| TC-06 | Issue closed（stage=pending_review） | 保持 pending_review | pending_review |
| TC-07 | 开发者确认完成 | pending_review → completed | done |
| TC-08 | 开发者取消任务 | → cancelled | 保持 |
| TC-09 | 开发者恢复任务 | cancelled → active | 回退到对应阶段 |
| TC-10 | Bot 接管（stage≠done） | 保持 pending_review | pending_review |

## 4. 风险与回退

### 4.1 风险

1. **存量数据修正风险**：批量 UPDATE 可能误修正正常状态的任务。建议先 SELECT 预览再执行。
2. **awaiting_input 误判**：`_detect_awaiting_input()` 基于关键词匹配，可能误判正常输出为澄清。建议初始版本只在 agent_type 为 `vod`/`issue` 时检测。
3. **set_issue_stage 同步 task.status 性能**：每次 stage 变更都 UPDATE tasks 表，但 issue 级任务通常只有 1-2 条记录，影响可忽略。

### 4.2 回退方案

所有改动通过 `_TASK_STATUS_FLOWS` 和 `close_issue_tasks()` 的条件分支控制。如需回退：
1. 恢复 `_TASK_STATUS_FLOWS` 到原始 4 状态版本
2. 恢复 `close_issue_tasks()` 的 `_stage in ("done", "pending_review")` 直接 completed 逻辑
3. 移除 `set_issue_stage()` 中的 task.status 同步代码

## 5. 验收标准

1. 任务必须经过 `pending_review` 阶段才能被开发者确认完成（`completed`）
2. Issue closed 时，stage=pending_review 的任务不被自动 completed
3. Agent 发送澄清评论后，task.status 变为 `awaiting_input`，看板显示在"待澄清"列
4. 开发者回复澄清后，task.status 恢复为 `processing`
5. `set_issue_stage()` 推进 stage 时，task.status 同步更新
6. 前端看板 6 列正确映射 task.status 和 issue_state.stage
