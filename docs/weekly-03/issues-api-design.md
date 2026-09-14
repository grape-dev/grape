# Issues 对外接口设计 + Issue/Event/Task 概念澄清

> 目标：新增 `/api/issues` 对外接口，以 Issue 为中心整体开放「阶段 + 事件列表 + pipeline」；
> 同时澄清 Issue / Event / Task 三个易混淆概念（含 "task complete 实为 event complete"）。
> 目标仓库：grape 主仓库（`src/api/routes/` + `help/api/` OpenAPI 契约）
> 状态：**已实施（A/B/C 全阶段完成，2026-09-14，生产已上线）**
> 实施计划：`docs/weekly-03/issues-api-execution-plan.md`
> 日期：2026-09-11（2026-09-14 刷新：对齐当前实现；`tasks`→`issue` 收编、`issue_state` DROP、`tasks/create` 移除）

---

## 一、概念澄清（Issue / Event / Task）

### 1.1 三者本质区别（注意：有两个 "Task"）

> ⚠️ 项目里存在**两个同名 "Task"**，语义不同，需先分清：

| 词 | 载体 | 本质 | 判别 |
|----|------|------|------|
| **Task（执行实例）** | `agent_tasks` 表 | 一次 **Agent 引擎执行**（一次事件的具体落地） | `task_id`（`t_` 前缀） |
| **Task（任务卡片）** | `issue` 表 | 一次 **Issue 任务卡**（任务中心展示对象，每 Issue 一行） | `id`（自增 int）+ `issue_number` |

| 概念 | 本质 | 载体 | 生命周期 | 判别关键 |
|------|------|------|----------|----------|
| **Issue** | GitCode 上的需求/任务单元（`#N`），pipeline 的**主对象** | `issue` 表 + GitCode | 创建 → 各阶段 → done/关闭 | **`stage`**（vod/issue/coding/qa/sre/done/pending_review） |
| **Event** | 一次**触发动作**（webhook/A2A），pipeline 的**执行单元** | `events` 表（`seq` 唯一） | 触发 → running → complete/fail | **`seq`** |
| **Task（执行实例）** | 一次 **Agent 引擎执行**，事件的具体落执行 | `agent_tasks` 表（`event_seq` 关联） | queued → running → completed/cancelled | **`task_id`**（`t_` 前缀） |
| **Task（任务卡片）** | 任务中心的 **Issue 任务卡**（每 Issue 一行） | `issue` 表（`issue_number` 关联） | 待处理 → 处理中 → 待澄清 → 待审核 → 已完成 | **`status`** |

> **2026-09-14 变更**：任务卡片表已由 `tasks` **改名并收编为 `issue` 单表**（见 §1.1.4）——
> `issue` 表同时承载 `status`（任务中心视角）与 `stage`（pipeline 视角），`issue_state` 表已停用并 DROP。

### 1.1.1 任务中心状态（`issue.status`）vs Pipeline 阶段（`issue.stage`）

两个"状态字段"同表并存，需明确来源与关系：

**`issue.stage`（Pipeline 阶段，机器驱动）**
```
vod / issue / coding / qa / sre / pending_review / done
```
- 表示流水线走到哪个 Agent / 是否待审核 / 是否完成
- 跟踪在 `issue` 表（原 issue_state.stage 收编）

**`issue.status`（任务中心状态，用户可读）**
```
active(待处理) / processing(处理中) / awaiting_input(待澄清) /
pending_review(待审核) / completed(已完成) / cancelled(已取消)
```
- 表示任务在"任务中心"视角下的用户可读状态
- 跟踪在 `issue` 表（字段 `status`，默认 `active`）

**联动**（`set_issue_stage` 驱动，`src/api/session_store.py`）：
```python
_STAGE_TO_TASK_STATUS = {
    "vod": "processing", "issue": "processing", "coding": "processing",
    "qa": "processing", "sre": "processing",
    "pending_review": "pending_review", "done": "completed",
}
# stage 推进 → 同步 issue.status（不覆盖 awaiting_input/completed/cancelled，用户操作优先）
UPDATE issue SET status=? WHERE repo_key=? AND issue_number=? AND status NOT IN ('completed','cancelled','awaiting_input')
```

**前端展示映射**（`issues.js _mapStatusL`）：

| 任务中心显示 | 判定逻辑 | 底层 status / stage |
|-------------|----------|---------------------|
| 待处理 | 兜底 `'pending'` | `status='active'` 或 stage 非上述 |
| 处理中 | `stage ∈ {vod..sre}` 或 `status='processing'` | `processing` |
| 待澄清 | `status='awaiting_input'` | `awaiting_input` |
| 待审核 | `status='pending_review'` 或 `stage='pending_review'` | `pending_review` |
| 已完成 | `status='completed'` 或 `stage='done'` | `completed` |
| 已取消 | `status='cancelled'` | `cancelled` |

> **本质关系**：`stage` 是 pipeline 视角（机器驱动），`status` 是任务中心视角（用户可读）。
> `status` 由 `stage` 映射驱动同步，但 `awaiting_input / completed / cancelled` 可由用户操作直接设置（更高优先级）。

### 1.1.2 数据表字段详解（当前实现）

**`issue` 表（原 tasks 改名 + 收编 issue_state 字段；每 Issue 一行，含任务中心 + pipeline 全量）** — `src/api/db_setup.py`

基础列（原 tasks 结构，Phase B1 rename 保留）：

```sql
id               INTEGER PRIMARY KEY AUTOINCREMENT,
user_id          INTEGER NOT NULL,        -- 创建者用户 id
assignee         TEXT NOT NULL,           -- 责任人（GitCode 用户名）
repo_key         TEXT NOT NULL,           -- 仓库（如 gitcode:owner/repo 或 owner/repo）
issue_number     INTEGER NOT NULL,        -- 对应 GitCode Issue 号
title            TEXT DEFAULT '',         -- Issue 标题
task_description TEXT DEFAULT '',         -- Issue body / 描述
conversation_id  TEXT DEFAULT '',         -- 关联对话（chat 会话）
status           TEXT DEFAULT 'active',   -- 任务中心状态：active/processing/awaiting_input/pending_review/completed/cancelled
completed_reason TEXT DEFAULT '',         -- 完成原因（issue_closed / reassigned / bot_takeover 等）
created_by       TEXT DEFAULT '',         -- 创建人（GitCode 用户名）
created_at       TEXT DEFAULT (datetime('now')),
completed_at     TEXT DEFAULT NULL
```

Phase B2 收编追加列（`_ensure_column`，来自原 `issue_state`）：

```sql
stage                TEXT DEFAULT 'vod',   -- pipeline 阶段：vod/issue/coding/qa/sre/pending_review/done
owner_agent          TEXT DEFAULT 'vod',   -- 当前归属 agent（最新被交办者）
scene_id             TEXT DEFAULT '',      -- Issue 级开发场景（VOD 判定，空=回退仓库默认）
scene_user_id        INTEGER DEFAULT 0,    -- 用户级场景创建者 ID（0=非用户级）
allow_scene_override INTEGER DEFAULT 0,    -- VOD 场景覆盖开关
a2a_seq              INTEGER,              -- 触发当前 stage 的 A2A 事件 seq
parent_issue_id      TEXT DEFAULT '',      -- 父任务 Issue ID（如 "#20"，空=普通任务）
parent_issue_number  INTEGER DEFAULT 0,    -- 兼容前端展示（parent_issue_id 推导）
is_subtask_mode      INTEGER DEFAULT 0,    -- 1=多任务模式（Issue Agent 应拆分子任务）
updated_at           TEXT DEFAULT '',      -- 最近更新时间
```

> `parent_issue_number`（INTEGER）为前端兼容的派生列（由 `parent_issue_id` TEXT 推导写入）；
> `is_subtask_mode` 在 issue 表与旧 issue_state 命名对齐，无重复语义。

**`issue_state` 表（已停用并 DROP，2026-09-14）**
> `_ISSUE_STATE_DDL` 已从建表清单移除；`_migrate_tables` 的 `Phase C5` 块在「无孤儿 + stage 全对齐」时 DROP。
> 全部字段由 `issue` 单表承载，代码读路径（`get_issue_stage` 等）以 issue 单表为权威。

### 1.1.3 两表合并（已成事实：`issue` 单表）

> `tasks`（任务中心）与 `issue_state`（pipeline）已合并为**一张 `issue` 表**，同时包含 `status`（任务中心）与 `stage`（pipeline）。

| 原 tasks（任务卡片） | 原 issue_state（pipeline） | 现 issue 单表 |
|---------------------|---------------------------|----------------|
| `status`（任务中心状态） | `stage`（pipeline 阶段） | ✅ 同表两列 |
| `task_description`（Issue body） | `scene_id / scene_user_id`（场景） | ✅ 合并 |
| `completed_reason`（完成原因） | `owner_agent / a2a_seq`（当前 agent） | ✅ 合并 |
| `conversation_id`（对话） | `parent_issue_id / is_subtask_mode`（子任务） | ✅ 合并 |
| `created_by / user_id / created_at` | `updated_at / assignee` | ✅ 合并 |

**收益（已实现）**
- `/api/issues` 单表出 `stage + status + events_count`，无需 join
- 消除「一 Issue 两行」冗余与手写同步（`_STAGE_TO_TASK_STATUS` 改单表 UPDATE）
- 状态语义统一：`status`（用户可读）+ `stage`（机器驱动）同表共存

### 1.1.4 渐进式迁移方案（**实际执行路径**，2026-09-14 完成）

> 采用渐进式改名方案：**先把 `tasks` 表 `RENAME TO issue`**（数据零迁移），之后**分批 `ADD COLUMN` 收编 `issue_state` 字段**，最后停用并 DROP `issue_state`。

**执行进度（均已落地）**

```
Phase 1 表改名      ALTER TABLE tasks RENAME TO issue（2026-09-12）✅
    - _tasks_to_issue_rename_pre_create：在 _create_tables 前执行，幂等处理空 issue 表误建（坑1）
    - src 53 处 + tests 74 处 SQL 表名替换（坑2）
Phase 2 字段收编    分批 ADD COLUMN（2026-09-12）✅
    - 批 A/B/C：stage/owner_agent/scene_id/scene_user_id/allow_scene_override/a2a_seq/parent_issue_id/updated_at
    - 回填：按 issue_id == "#"+issue_number 从旧 issue_state 权威对齐（幂等）
Phase 3 读写切换    写全走 issue；读 issue 权威 + issue_state 兜底（2026-09-14）✅
Phase 4 收尾        issue_state 停写 → 条件 DROP（2026-09-14）✅
    - 生产 2026-09-14：清理 46 孤儿（issue_state 无对应 issue，已备份），两表 stage 全对齐
    - C5 迁移块：孤儿=0 && mismatch=0 才 DROP，否则告警保留（SQLite 自动 / MySQL 手动）
```

**关键产物/坑位**（详见 `issues-api-execution-plan.md`）
- `db_setup`：`_ISSUE_DDL` + `_tasks_to_issue_rename_pre_create` + B2 补列/回填 + C5 DROP
- `session_store`：`_sync_issue_fields`（ensure_row 建档兜底）+ 5 个 set_* 写 issue + `get_issue_stage` 读 issue 权威
- 读路径：`issues._issue_stage_map`、`tasks._batch_fetch_issue_state`、`cleanup._done_issues` 均改读 issue 单表

### 1.2 层级关系

```
Issue (#5, stage=qa)
  └── Event (seq=102, agent=qa, event_type=pull_request_updated)
        └── Task (t_xxx, event_seq=102)      ← 引擎实际执行
        └── Task (t_yyy, 重派/重试)            ← 同一事件可多次 task
```

### 1.3 关键澄清：task complete ≠ issue complete

> **`POST /api/tasks/complete` 的 `task_id` 是会话 token 的 `session_key`（M2 以 task_id 签发），
> 它完成的是「agent_tasks 的那一次执行实例」，对应 `event_seq` —— 本质是「某次 Event 执行完成」，
> 而非「Issue 的任务完成 / Issue 完结」。**

| 完成层级 | 表达 | 触发 |
|----------|------|------|
| **Task** 完成 | `agent_tasks.status → completed` | `complete_task`（会话 token） |
| **Event** 完成 | `events.status → completed`（含 result/agent_logs） | `update_event_complete` |
| **Issue 阶段推进** | `issue.stage` 更新（owner_agent 流转） | `set_issue_stage` |
| **Issue 完结** | **最后节点（next=done）complete → `pending_review`** → 审核 → done | 状态机 + 开发者审核 |

### 1.4 错位已解决（本设计要解决的）

- ✅ `01-tasks.md` 的 "task" 混用已澄清：`agent_tasks`（执行实例 id）+ `issue` 表（Issue 级任务卡片）。
- ✅ `08-events.md` 平铺事件列表保留；`/api/issues` 提供「按 Issue 看 pipeline」的整体视角。

---

## 二、Issues 接口（/api/issues，已实现）

### 2.1 设计目标（已达成）

以 **Issue 为中心** 对外整体开放：
- Issue 的**阶段**（stage）、当前 agent（owner_agent）、场景（scene_id）
- 该 Issue 对应的**事件列表**（event 流）
- Issue 的 **pipeline 路线图**（event.json 结构化链）

### 2.1.1 归属澄清：`/api/tasks/create` 本质是「Issues 创建」（已移除）

> `POST /api/tasks/create`（原挂 `/api/tasks`）本质是「Issue 创建」——**2026-09-14 已移除**，
> 对外唯一创建入口为 `POST /api/issues/{owner}/{repo}`（`tasks.create_task_via_issue` 函数保留，供 issues 复用）。

### 2.1.2 Issue/Event/Task 归属对照（当前接口层）

| 对外接口 | 实际操作对象 | 语义 |
|----------|-------------|------|
| `POST /api/issues/{owner}/{repo}` | 创建 GitCode Issue + 建档 | **Issue 创建**（官方唯一入口） |
| `GET /api/issues/...` | Issue 聚合（stage/events/pipeline） | **Issue 视角** |
| `POST /api/tasks/complete` | 某次执行实例完成（session_key→event_seq） | **Event 执行完成**（非 Issue 完结） |
| `POST /api/tasks/{id}/confirm-done` | 开发者确认 Issue 阶段 → done | **Issue 完结确认**（stage: pending_review→done） |
| `POST /api/tasks/{id}/cancel` | 取消任务 → 置 cancelled + 停流水线 | 任务暂停/取消 |

### 2.2 端点设计（已实现，`src/api/routes/issues.py`）

```
GET  /api/issues/{owner}/{repo}                    # 仓库下 Issue 列表（含 stage/status 聚合）  operation_id=issues_list
POST /api/issues/{owner}/{repo}                    # 创建 Issue（对外唯一创建入口）           operation_id=issues_create
GET  /api/issues/{owner}/{repo}/{number}           # 单个 Issue 详情（stage+events+pipeline）  operation_id=issues_detail
GET  /api/issues/{owner}/{repo}/{number}/events    # 该 Issue 的事件列表                       operation_id=issues_events
GET  /api/issues/{owner}/{repo}/{number}/pipeline  # Issue pipeline 路线图（event.json）       operation_id=issues_pipeline
```

### 2.3 数据来源映射（当前实现）

| 字段 | 来源 | 说明 |
|------|------|------|
| issue 基本信息（title/status/created） | `issue` 表 + `events` 表首条 | 首次发生时的标题 |
| **stage** | `issue.stage` | Pipeline 阶段（vod/issue/coding/qa/sre/done/pending_review） |
| **status** | `issue.status` | 任务中心状态；由 stage 映射同步，用户操作可覆盖 |
| **owner_agent** | `issue.owner_agent` | 当前执行的 agent |
| **a2a_seq** | `issue.a2a_seq` | 触发当前 stage 的 A2A 事件号 |
| **scene_id** | `issue.scene_id` | 场景 |
| assignee / subtask | `issue.assignee / is_subtask_mode / parent_issue_id` | 责任人、子任务 |
| event 列表 | `events` 表按 issue_id 过滤，seq 升序 | 全部事件（seq/type/agent/status/时间） |
| pipeline 段 | `load_event_json(repo, number)` | event.json 完整链；无档案降级由 events 重构 |

> 阶段字段统一读 `issue` 单表（`_issue_stage_map` 为 issue 权威 + issue_state 兜底兼容）。

### 2.4 现有可复用能力（已落地）

| 能力 | 位置 | 用途 |
|------|------|------|
| `get_issue_stage(repo_key, issue_id)` | `session_store.py` | stage/owner/assignee/a2a_seq/scene（读 issue 单表） |
| `_issue_stage_map(repo_key)` | `routes/issues.py` | 批量 stage 聚合 |
| `_task_status_map(repo_key)` | `routes/issues.py` | 任务中心 status 聚合 |
| `_group_events_by_issue(repo_key)` | `routes/issues.py` | 按 issue 聚类事件 |
| `load_event_json(repo_key, number)` | `event_archive.py` | pipeline 段 / 结构化产出 |
| `normalize_event_time` | `event_store.py` | 时间格式化 |

### 2.5 认证与契约

- 认证：`get_current_user_or_api_key`（三通道：JWT / `tk_` 会话 token / `grape_` API Key）
- 白名单：进 `custom_openapi`，`test_openapi.EXPECTED_PUBLIC` 含 `issues_list/create/detail/events/pipeline`
- 文档：`docs/openapi/11-issues.md` + `help/api/11-issues.md`；`README.md` 目录 + `EXPECTED_API_FILES` 已含

### 2.6 出参示例（与实现一致）

```json
// GET /api/issues/{owner}/{repo}
{
  "issues": [
    {
      "number": 5,
      "title": "优化前端UI",
      "stage": "qa",
      "task_status": "processing",          // issue.status 任务中心状态
      "owner_agent": "qa",
      "scene_id": "default",
      "assignee": "grape_bot",
      "is_subtask_mode": 0,
      "parent_issue_id": "",
      "events_count": 6,
      "latest_event": {"seq": 102, "event_type": "pull_request_updated", "agent_type": "qa", "status": "completed", "created_at": "..."}
    }
  ],
  "total": 1
}

// GET /api/issues/{owner}/{repo}/5
{
  "number": 5, "title": "优化前端UI", "stage": "qa", "task_status": "processing",
  "owner_agent": "qa", "scene_id": "default", "assignee": "grape_bot",
  "events": [
    {"seq": 96, "event_type": "issue_opened", "agent_type": "vod", "status": "completed", "created_at": "..."},
    {"seq": 102, "event_type": "pull_request_updated", "agent_type": "qa", "status": "completed", "created_at": "..."}
  ],
  "pipeline": {
    "issue": {"number": 5},
    "summary": "已按审查意见修复 | 待 QA 重新审查",
    "status": "running",
    "segments": [
      {"seq": 96, "agent": "vod", "event_type": "issue_opened", "status": "completed", "runtime": "", "skill": "", "output": {"conclusion": "...", "next_step": "a2a_issue", "actions": [], "artifacts": []}},
      {"seq": 102, "agent": "qa", "event_type": "pull_request_updated", "status": "running", "output": {}},
      {"seq": 107, "agent": "sre", "status": "pending", "output": {}}
    ]
  }
}
```

---

## 三、实施范围（已完成）

| # | 项 | 说明 | 状态 |
|---|----|------|------|
| 1 | 新增 `src/api/routes/issues.py` | `/api/issues` 5 个端点（list/create/detail/events/pipeline） | ✅ |
| 2 | 新增 `POST /api/issues/{owner}/{repo}`（创建） | 复用 create_task_via_issue（ensure_issue_task + GitCode create）；官方创建入口 | ✅ |
| 3 | `POST /api/tasks/create` 废弃/移除 | **端点已移除**（C2），调用方统一 `/api/issues`；`01-tasks.md` 标注迁移 | ✅ |
| 4 | `main.py` 挂载 router | `include_router(issues_router)` | ✅ |
| 5 | `help/api/11-issues.md` | 接口文档 + 概念澄清表 | ✅ |
| 6 | `help/api/01-tasks.md` | 标注「已迁移至 /api/issues」 | ✅ |
| 7 | `help/api/README.md` | 目录加第 11 项 | ✅ |
| 8 | `tests/test_help_directory.py` | `EXPECTED_API_FILES` 加 `11-issues.md` | ✅ |
| 9 | `tests/test_issues_api.py`（新增） | stage/events/pipeline 聚合 + create 断言（14 passed） | ✅ |
| 10 | OpenAPI 契约 | `custom_openapi` 自动导出（issues_* operationId） | ✅ |
| 11 | CLI `grape issues` | 从契约生成（独立仓库 grape-cli） | 契约已就绪 |

---

## 四、验收标准（全部达成）

- [x] `GET /api/issues/{owner}/{repo}` 返回仓库 Issue 列表（含 stage/task_status/owner_agent/events_count/latest_event）
- [x] `POST /api/issues/{owner}/{repo}` 创建 Issue（复用 create_task_via_issue 逻辑，触发 pipeline）
- [x] `GET /api/issues/{owner}/{repo}/{number}` 返回 stage + task_status + events + pipeline 聚合
- [x] `GET .../{number}/events` 仅返回该 Issue 的事件列表（seq 升序）
- [x] `GET .../{number}/pipeline` 返回 event.json 结构化链；无档案降级 events 重构
- [x] `POST /api/tasks/create` 已移除（C2），调用方统一走 `POST /api/issues`（405 验证）
- [x] **文档 11-issues.md 含概念澄清表**：Issue/Event/Task 三层级、task complete ≠ issue complete、tasks/create 归属、双状态
- [x] 认证三通道可用；契约导出；CLI `grape issues` 可用（契约驱动）
- [x] 现有 events/tasks 接口不回归（基线 12 失败均为既有遗留，与本次无关）

---

## 五、测试状态（已实现）

- `tests/test_issues_api.py`（新增）：14 用例全绿——列表聚合/空仓库/事件范围/详情/events/pipeline/认证/创建校验。
  构造数据经 `set_issue_stage` 写 issue 单表（真实双写路径），`_mk_task` 用 INSERT..WHERE NOT EXISTS 不清零收编字段。
- 回归：`test_openapi`（契约）、`test_help_directory`（文档一致）、`test_git_repo_api`、任务中心系列均无新增回归。
- 生产核对（2026-09-14）：issue 182 行、issue_state 清理后 DROP、`/api/health` 200。

---

## 六、影响评估（已达成）

| 项 | 说明 |
|----|------|
| 视角 | 从「按事件平铺」升级为「按 Issue 看 pipeline」，补全对外整体视图 ✅ |
| 概念 | 文档澄清 Issue/Event/Task，消除 task_id 混用歧义 ✅ |
| 数据模型 | `tasks`/`issue_state` 两表收敛为 `issue` 单表，`issue_state` DROP ✅ |
| 兼容性 | 新增端点，`/api/events`、`/api/tasks` 其余接口不动 ✅ |

## 七、关键代码位置（当前实现）

| 位置 | 说明 |
|------|------|
| `src/api/routes/issues.py` | `/api/issues` 5 端点 + 聚合辅助（_issue_stage_map/_task_status_map/_group_events_by_issue） |
| `src/api/session_store.py` | `get_issue_stage`（读 issue 单表权威）+ `set_*` 5 个写路径 + `_sync_issue_fields` |
| `src/api/db_setup.py` `_ISSUE_DDL` | `issue` 表（基础列）；B2 补列 + C5 DROP 迁移 |
| `src/api/db_setup.py` `_tasks_to_issue_rename_pre_create` | tasks→issue rename（pre-create 时机） |
| `src/api/routes/tasks.py` | `create_task_via_issue`（去路由，供 issues 复用）+ `_batch_fetch_issue_state`（读 issue） |
| `src/api/event_archive.py` | `load_event_json`（pipeline 段） |
| `src/runtime/cleanup.py` | `_done_issues`（读 issue 单表） |
| `src/api/main.py` | 路由挂载 |
| `help/api/11-issues.md` / `docs/openapi/11-issues.md` | 接口文档 + 概念澄清 |
| `tests/test_issues_api.py` / `test_help_directory.py` / `test_openapi.py` | 接口/文档/契约测试 |