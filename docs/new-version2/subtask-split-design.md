# 大任务拆分子任务设计方案

> 状态：草案 | 日期：2026-08-28 | 分支：wise

## 一、背景与目标

**问题**：大任务（如"重构整个博客系统架构"）在单个流水线中执行时，coding 节点动辄 30-60 分钟，远超 20 分钟安全线，导致超时失败率高（exit_code=-15 SIGTERM 或 1200s timeout）。

**目标**：
1. 拆分后每个子任务在每个节点跑的时间不超过 20 分钟
2. VOD 澄清 → Issue Agent 方案设计 → Issue Agent 拆分为多个子任务
3. 每个子任务在 GitCode 创建独立 Issue，描述中注明父任务链接
4. 子任务走正常流水线，完成后最后一个节点登记完成情况到父 Issue
5. 任务中心创建任务时用户可选择子任务模式

## 二、核心概念

### 2.1 父子任务关系

```
父任务 Issue #20（重构博客系统架构）
  ├── 子任务 Issue #21（数据库迁移：SQLite → MySQL）
  ├── 子任务 Issue #22（模板分离：内联 → Jinja2）
  ├── 子任务 Issue #23（测试覆盖：0 → 80%）
  └── 子任务 Issue #24（CI/CD：手动 → 自动）
```

### 2.2 数据结构

#### issue_state 表加列

```sql
ALTER TABLE issue_state ADD COLUMN parent_issue_id TEXT DEFAULT '';
-- 父 Issue 的 issue_id（如 "#20"），空=普通任务
ALTER TABLE issue_state ADD COLUMN is_subtask_mode INTEGER DEFAULT 0;
-- 父任务标记：1=启用子任务模式（Issue Agent 应拆分）
```

#### tasks 表加列

```sql
ALTER TABLE tasks ADD COLUMN parent_issue_number INTEGER DEFAULT 0;
-- 父任务的 Issue 编号，0=普通任务
ALTER TABLE tasks ADD COLUMN is_subtask_mode INTEGER DEFAULT 0;
-- 创建时用户选择：1=子任务模式（Issue Agent 应拆分）
```

### 2.3 数据流

```
用户在任务中心创建任务（勾选"子任务模式"）
  │
  ▼
GitCode 创建父 Issue #20（body 标注 [subtask-mode]）
  │
  ▼
VOD 审视父 Issue → 澄清需求 → A2A 交办 Issue Agent
  │  payload: {is_subtask_mode: true}
  ▼
Issue Agent 生成方案 → 判断可拆分 → 创建子 Issue #21~#24
  │  每个子 Issue body 包含: "父任务: [Issue #20](gitcode-url)"
  │  父 Issue body 更新: "子任务: #21 #22 #23 #24"
  │  Issue Agent 不 A2A → coding（父任务到此为止）
  ▼
子 Issue #21 issue_opened → webhook → VOD 审视 → A2A Issue Agent
  │  VOD 识别 issue_state.parent_issue_id 非空 → 跳过澄清（直接 A2A）
  │  Issue Agent 识别 parent_issue_id → 跳过方案设计（直接生成 Spec → A2A coding）
  ▼
子 Issue #21 走正常流水线: vod → issue → coding → qa → sre
  │
  ▼
SRE 完成后 → _post_result_to_parent_issue()
  │  在父 Issue #20 评论: "子任务 #21 已完成：数据库迁移（PR #15 已合并）"
  │  检查所有子任务是否完成
  │  全部完成 → 父 Issue 评论汇总报告
  ▼
所有子任务完成 → 父任务自动关闭（可选）
```

## 三、详细设计

### 3.1 任务中心创建任务（前端）

`src/api/static/js/pages/tasks.js` 的 `openTaskCreateModal()` 增加子任务模式开关：

```html
<label style="display:flex;align-items:center;gap:8px;color:#333;cursor:pointer">
  <input type="checkbox" id="tcSubTaskMode" style="width:16px;height:16px;cursor:pointer">
  <span style="font-size:13px">子任务模式</span>
  <span style="font-size:11px;color:#888">大任务自动拆分为多个子 Issue，每个独立走流水线（≤20min/节点）</span>
</label>
```

`createTaskSubmit()` 增加 `is_subtask_mode` 字段：

```javascript
const isSubTask = document.getElementById('tcSubTaskMode')?.checked ? 1 : 0;
API.post('/tasks/create', {
  repo_key: repoKey.trim(),
  title: title.trim(),
  description: desc.trim(),
  scene_id: sceneId,
  is_subtask_mode: isSubTask
});
```

### 3.2 后端创建任务（API）

`src/api/routes/tasks.py` 的 `TaskCreateBody` 加字段：

```python
class TaskCreateBody(BaseModel):
    repo_key: str = ""
    title: str = ""
    description: str = ""
    assignee: str = ""
    scene_id: str = ""
    is_subtask_mode: int = 0  # 新增
```

`create_task_via_issue()` 中创建 GitCode Issue 时，body 追加标记：

```python
# 如果启用子任务模式，Issue body 追加标记
full_desc = (body.description or "").strip()
if body.is_subtask_mode:
    full_desc += "\n\n---\n[subtask-mode] 本任务启用子任务模式，将由 Issue Agent 拆分为子任务执行。"
```

创建后写入 issue_state：

```python
if body.is_subtask_mode:
    from src.api.session_store import set_issue_stage
    set_issue_stage(repo_key, f"#{issue_num}", "vod", "vod")
    # 标记为子任务模式（需新增 set_subtask_mode 函数）
    _set_subtask_mode(repo_key, f"#{issue_num}", True)
```

### 3.3 VOD 处理父任务

VOD SKILL.md 新增规则——识别 `[subtask-mode]` 标记：

```markdown
#### B7 子任务模式识别

检查 Issue body 是否包含 `[subtask-mode]` 标记：
- **是**：需求澄清后 A2A 交办 Issue Agent，payload 中增加 `is_subtask_mode: true`
  - Issue Agent 将根据方案拆分为子任务
  - VOD 不需要额外处理，正常走澄清 → A2A 流程
- **否**：正常流程
```

VOD A2A 调用 payload：

```json
{
  "agent": "issue",
  "payload": {
    "repository": "...",
    "issue_number": "20",
    "context": "VOD 已完成需求澄清...",
    "recommended_scene": "default",
    "is_subtask_mode": true
  }
}
```

### 3.4 A2A 端点接收子任务模式标记

`src/api/routes/webhook.py` 的 `a2a_endpoint()`：

```python
_is_subtask_mode = payload.get("is_subtask_mode", False)
if _is_subtask_mode and issue_number:
    _set_subtask_mode(repo_key, f"#{issue_number}", True)
    log.info(f"A2A: subtask_mode enabled for issue=#{issue_number}")
```

### 3.5 Issue Agent 拆分子任务（核心）

`skills/issue-advance/SKILL.md` 新增步骤——方案设计后判断是否拆分：

```markdown
### 8. 子任务拆分（is_subtask_mode 启用时）

当 Issue body 包含 `[subtask-mode]` 或 A2A payload 中 `is_subtask_mode=true` 时：

#### 8.1 方案设计后评估可拆分性

基于 Spec 评估：
- 任务总预估时间 > 20 分钟/节点？
- 是否可拆分为独立的子任务（每个子任务可独立 PR）？
- 子任务之间是否有强依赖？（有依赖 → 按依赖顺序创建，无依赖 → 并行创建）

#### 8.2 创建子 Issue

对每个子任务，调用 GitCode API 创建 Issue：

```bash
curl -X POST https://gitcode.com/api/v5/repos/{owner}/{repo}/issues \
  -H "Authorization: Bearer $BOT_G...OKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "子任务：数据库迁移 SQLite → MySQL",
    "body": "## 父任务\n[Issue #20](https://gitcode.com/{owner}/{repo}/issues/20)\n\n## 目标\n{从 Spec 中提取的子任务目标}\n\n## 验收标准\n{子任务验收标准}\n\n## 依赖\n{如有前置子任务，注明}",
    "assignee": ["grape_bot"]
  }'
```

每个子 Issue body 必须包含：
- `## 父任务` + GitCode Issue 链接
- 子任务目标 / 验收标准 / 依赖

#### 8.3 更新父 Issue

在父 Issue body 中追加子任务列表：

```bash
curl -X PATCH https://gitcode.com/api/v5/repos/{owner}/{repo}/issues/20 \
  -H "Authorization: Bearer $BOT_G...OKEN" \
  -d '{"body": "{原 body}\n\n---\n## 子任务\n- [ ] [#21 数据库迁移](issues/21)\n- [ ] [#22 模板分离](issues/22)\n- [ ] [#23 测试覆盖](issues/23)\n- [ ] [#24 CI/CD](issues/24)"}'
```

#### 8.4 不 A2A 交办 coding

子任务模式下，Issue Agent **不 A2A 交给 coding**。子 Issue 创建后 webhook 自动触发 VOD 审视，进入正常流水线。

父 Issue 的流水线到此结束（issue 阶段 → done，不再走 coding/qa/sre）。
```

### 3.6 VOD 处理子任务（快速通道）

VOD SKILL.md 新增规则——识别子任务：

```markdown
#### B8 子任务识别（快速通道）

检查 Issue body 是否包含 `## 父任务` + Issue 链接：
- **是子任务**：跳过需求澄清（父任务已澄清），直接 A2A 交办 Issue Agent
  - A2A payload 中增加 `is_subtask: true, parent_issue: "#20"`
  - Issue Agent 将跳过方案设计，直接基于父任务 Spec 生成子任务 Spec → A2A coding
- **否**：正常流程
```

服务端 `run_pipeline()` 中 VOD 执行前检查 `parent_issue_id`：

```python
# 如果是子任务，VOD prompt 注入父任务 Spec
_st = get_issue_stage(repo_key, issue_id)
if _st and _st.get("parent_issue_id"):
    # 注入父任务的 Spec 上下文
    evt["_parent_issue"] = _st["parent_issue_id"]
    evt["_is_subtask"] = True
```

prompt_builder 中为子任务的 VOD 注入：

```
## 子任务上下文
本 Issue 是父任务 Issue #20 的子任务。
父任务已完成需求澄清和方案设计，你不需要重复澄清。
请直接 A2A 交办 Issue Agent（payload: is_subtask=true）。
```

### 3.7 Issue Agent 处理子任务（快速通道）

Issue Agent SKILL.md 新增规则：

```markdown
### 9. 子任务处理（is_subtask=true 时）

当识别为子任务时（Issue body 含 `## 父任务` 或 A2A payload `is_subtask=true`）：

1. **跳过可开发性分析**（父任务已确认）
2. **跳过 Spec 生成**——直接从父 Issue body 中提取本子任务的目标和验收标准
3. **生成本子任务的精简 Spec**（仅目标 + 验收标准 + 实现建议）
4. **A2A 交办 coding**（正常流程）

精简 Spec 格式：
```
## 目标
{从父 Issue 子任务列表中提取}

## 验收标准
{从父 Issue 或子 Issue body 中提取}

## 实现建议
{基于项目知识基线给出的技术建议}
```
```

### 3.8 子任务完成后登记到父任务

`src/api/event_pipeline.py` 的 `_execute_agent_inner()` 中，SRE（最后一个节点）完成后：

```python
# SRE 完成后检查是否是子任务 → 登记到父 Issue
if new_status == "completed" and agent_type == "sre" and issue_id:
    _st = get_issue_stage(repo_key, issue_id)
    if _st and _st.get("parent_issue_id"):
        _parent_issue = _st["parent_issue_id"]
        _post_subtask_completion_to_parent(
            repository, issue_id, _parent_issue,
            result_text, repo_gitcode_token
        )
        # 检查所有子任务是否完成
        _check_all_subtasks_done(repo_key, _parent_issue, repository, repo_gitcode_token)
```

新增函数：

```python
def _post_subtask_completion_to_parent(repository, sub_issue_id, parent_issue_id, result_text, bot_token):
    """子任务完成后，在父 Issue 评论登记完成情况。"""
    _parent_num = _issue_num_from(parent_issue_id)
    _sub_num = _issue_num_from(sub_issue_id)
    # 提取结果摘要（conclusion）
    _summary = _extract_conclusion(result_text) or result_text[:200]
    _comment = f"### 子任务 #{_sub_num} 已完成\n\n{_summary}\n\n---\n子任务流水线已全部完成。"
    # POST comment to parent issue
    ...

def _check_all_subtasks_done(repo_key, parent_issue_id, repository, bot_token):
    """检查父任务的所有子任务是否全部完成。"""
    # 查 issue_state 中 parent_issue_id=parent_issue_id 且 stage != 'done' 的记录
    # 全部 done → 在父 Issue 发汇总评论 + 可选关闭
    ...
```

### 3.9 父任务 Issue State 生命周期

父任务的 issue_state 在 Issue Agent 拆分完成后直接进入 done：

```python
# Issue Agent done 后，如果是子任务模式 → 父任务 stage = done
if agent_type == "issue" and _is_subtask_mode(issue_id):
    set_issue_stage(repo_key, issue_id, "done", "issue")
    # 不 A2A coding
```

### 3.10 子任务的 issue_state 初始化

子 Issue 被 webhook 触发 issue_opened 时，`run_pipeline()` 中创建 issue_state 时设置 parent_issue_id：

```python
# 从 Issue body 解析父任务链接
_parent_issue = _parse_parent_issue_from_payload(payload)
if _parent_issue:
    set_issue_stage(repo_key, issue_id, "vod", "vod")
    _set_parent_issue(repo_key, issue_id, _parent_issue)
```

`_parse_parent_issue_from_payload()` 从 Issue body 中提取 `## 父任务` + `Issue #N` 链接。

## 四、改动清单

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | issue_state 加 `parent_issue_id` + `is_subtask_mode` 列；tasks 加 `parent_issue_number` + `is_subtask_mode` 列 |
| `src/api/session_store.py` | 新增 `set_parent_issue()` / `get_parent_issue()` / `set_subtask_mode()` / `is_subtask()` |
| `src/api/routes/tasks.py` | `TaskCreateBody` 加 `is_subtask_mode`；创建 Issue 时 body 追加 `[subtask-mode]` 标记 |
| `src/api/routes/webhook.py` | A2A 端点接收 `is_subtask_mode` 并持久化 |
| `src/api/event_pipeline.py` | `run_pipeline()` 解析子任务 parent_issue_id；SRE 完成后登记父 Issue；父任务 Issue Agent done 后 stage=done |
| `src/prompt_builder.py` | 子任务 VOD/Issue prompt 注入父任务上下文 |
| `src/api/static/js/pages/tasks.js` | 新建任务弹窗增加子任务模式 checkbox |
| `skills/vod/SKILL.md` | B7 子任务模式识别 + B8 子任务快速通道 |
| `skills/issue-advance/SKILL.md` | §8 子任务拆分 + §9 子任务快速通道 |

## 五、完整数据流

```
1. 用户在任务中心创建任务（勾选"子任务模式"）
   └─ POST /api/tasks/create {is_subtask_mode: 1}
      └─ GitCode 创建父 Issue #20（body 含 [subtask-mode]）
      └─ issue_state: {stage=vod, is_subtask_mode=1}

2. webhook issue_opened → VOD 审视父 Issue #20
   └─ VOD 识别 [subtask-mode] → 澄清需求 → A2A Issue Agent
      └─ A2A payload: {is_subtask_mode: true}

3. Issue Agent 处理父 Issue #20
   └─ 生成方案 Spec → 评估可拆分 → 创建子 Issue #21~#24
      └─ 每个子 Issue body: "## 父任务 [Issue #20](url)"
      └─ 父 Issue body 更新: "## 子任务 #21 #22 #23 #24"
   └─ 父 Issue issue_state: stage=done（父任务流水线结束）

4. 子 Issue #21 issue_opened → webhook → VOD 审视
   └─ VOD 识别"## 父任务" → 快速通道 → A2A Issue Agent
      └─ A2A payload: {is_subtask: true, parent_issue: "#20"}

5. Issue Agent 处理子 Issue #21
   └─ 跳过方案设计 → 从父 Spec 提取目标 → 精简 Spec → A2A coding

6. 子 Issue #21 正常流水线: coding → qa → sre
   └─ 每个节点 ≤20min（任务粒度小）

7. SRE 完成后 → 登记到父 Issue #20
   └─ POST comment: "子任务 #21 已完成：数据库迁移（PR #15）"
   └─ 检查所有子任务是否完成
      └─ 全部完成 → 父 Issue 汇总报告评论

8. 子 Issue #22~#24 同理并行/串行执行
```

## 六、边界处理

| 场景 | 处理 |
|------|------|
| Issue Agent 判断不可拆分 | 不创建子 Issue，按正常流程 A2A coding（回退为普通模式） |
| 子任务之间有依赖 | 子 Issue body 注明"依赖 #21 完成"，VOD 检查前置是否 done |
| 子任务失败 | 父 Issue 评论"子任务 #22 失败：原因"，不影响其他子任务 |
| 子任务数量过多（>10） | Issue Agent 应控制粒度，每个子任务预估 ≤20min/节点 |
| 父任务 Issue 被手动关闭 | 子任务继续执行，完成登记时跳过（父 Issue 已关） |
| 非子任务模式的普通任务 | 完全不受影响（is_subtask_mode=0） |

## 七、测试要点

1. **创建子任务模式任务** → Issue body 含 `[subtask-mode]`，issue_state.is_subtask_mode=1
2. **VOD 识别子任务模式** → A2A payload 含 is_subtask_mode=true
3. **Issue Agent 拆分** → 创建 N 个子 Issue，每个 body 含父任务链接，父 Issue body 更新子任务列表
4. **子任务 VOD 快速通道** → 识别 parent_issue_id → 跳过澄清 → A2A Issue Agent
5. **子任务 Issue Agent 快速通道** → 跳过方案设计 → 精简 Spec → A2A coding
6. **SRE 完成登记** → 父 Issue 收到完成评论
7. **全部完成汇总** → 父 Issue 收到汇总报告
8. **普通任务不受影响** → is_subtask_mode=0 的任务走正常流程
