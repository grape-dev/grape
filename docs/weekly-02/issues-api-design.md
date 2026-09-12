# Issues 对外接口设计 + Issue/Event/Task 概念澄清

> 目标：新增 `/api/issues` 对外接口，以 Issue 为中心整体开放「阶段 + 事件列表 + pipeline」；
> 同时澄清 Issue / Event / Task 三个易混淆概念（含 "task complete 实为 event complete"）。
> 目标仓库：grape 主仓库（`src/api/routes/` + `help/api/` OpenAPI 契约）
> 状态：**设计文档，待实施**
> 日期：2026-09-11

---

## 一、概念澄清（Issue / Event / Task）

### 1.1 三者本质区别（注意：有两个 "Task"）

> ⚠️ 项目里存在**两个同名 "Task"**，语义不同，需先分清：

| 词 | 载体 | 本质 | 判别 |
|----|------|------|------|
| **Task（执行实例）** | `agent_tasks` 表 | 一次 **Agent 引擎执行**（一次事件的具体落地） | `task_id`（`t_` 前缀） |
| **Task（任务卡片）** | `tasks` 表 | 一次 **Issue 任务卡**（任务中心展示对象，每 Issue 一行） | `id`（自增 int）+ `issue_number` |

| 概念 | 本质 | 载体 | 生命周期 | 判别关键 |
|------|------|------|----------|----------|
| **Issue** | GitCode 上的需求/任务单元（`#N`），pipeline 的**主对象** | `issue_state` 表 + GitCode | 创建 → 各阶段 → done/关闭 | **`stage`**（vod/issue/coding/qa/sre/done/pending_review） |
| **Event** | 一次**触发动作**（webhook/A2A），pipeline 的**执行单元** | `events` 表（`seq` 唯一） | 触发 → running → complete/fail | **`seq`** |
| **Task（执行实例）** | 一次 **Agent 引擎执行**，事件的具体落执行 | `agent_tasks` 表（`event_seq` 关联） | queued → running → completed/cancelled | **`task_id`**（`t_` 前缀） |
| **Task（任务卡片）** | 任务中心的 **Issue 任务卡**（每 Issue 一行） | `tasks` 表（`issue_number` 关联） | 待处理 → 处理中 → 待澄清 → 待审核 → 已完成 | **`status`** |

### 1.1.1 任务中心状态（`tasks.status`）vs Pipeline 阶段（`issue_state.stage`）

两个"状态字段"并行存在，需明确来源与关系：

**`issue_state.stage`（Pipeline 阶段，权威阶段机）**
```
vod / issue / coding / qa / sre / pending_review / done
```
- 表示流水线走到哪个 Agent / 是否待审核 / 是否完成
- 跟踪在 `issue_state` 表

**`tasks.status`（任务中心状态）**
```
active(待处理) / processing(处理中) / awaiting_input(待澄清) /
pending_review(待审核) / completed(已完成) / cancelled(已取消)
```
- 表示任务在"任务中心"视角下的用户可读状态
- 跟踪在 **`tasks` 表**（`_TASKS_DDL`，字段 `status`，默认 `active`）

**联动**（`set_issue_stage` 驱动，session_store.py:97-115）：
```python
_STAGE_TO_TASK_STATUS = {
    "vod": "processing", "issue": "processing", "coding": "processing",
    "qa": "processing", "sre": "processing",
    "pending_review": "pending_review", "done": "completed",
}
# stage 推进 → 同步 tasks.status（不覆盖 awaiting_input/completed/cancelled，用户操作优先）
UPDATE tasks SET status=? WHERE repo_key=? AND issue_number=? AND status NOT IN ('completed','cancelled','awaiting_input')
```

**前端展示映射**（`tasks.js _mapStatusL`）：

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

### 1.1.2 数据表字段详解

**`tasks` 表（任务中心卡片，每 Issue 一行）** — `src/api/db_setup.py _TASKS_DDL`

```sql
CREATE TABLE IF NOT EXISTS tasks (
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
)
```

> 补充：`is_subtask_mode` 不在本表（它存于 `issue_state`）；任务卡片表只保留创建/状态/完成快照。

**`issue_state` 表（Pipeline 阶段状态机，每 Issue 一行）** — `src/api/db_setup.py _ISSUE_STATE_DDL`

```sql
CREATE TABLE IF NOT EXISTS issue_state (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key     TEXT NOT NULL,
    issue_id     TEXT NOT NULL,               -- 如 "#5"
    stage        TEXT NOT NULL DEFAULT 'vod',   -- vod/issue/coding/qa/sre/pending_review/done
    owner_agent  TEXT NOT NULL DEFAULT 'vod',   -- 当前归属 agent（最新被交办者）
    assignee     TEXT DEFAULT '',              -- Issue 责任人（GitCode assignee，可为 grape_bot 或用户）
    a2a_seq      INTEGER,                      -- 触发当前 stage 的 A2A 事件 seq
    scene_id     TEXT DEFAULT '',              -- Issue 级开发场景（VOD 判定，空=回退仓库默认）
    parent_issue_id TEXT DEFAULT '',           -- 父任务 Issue ID（如 "#20"，空=普通任务）
    is_subtask_mode INTEGER DEFAULT 0,         -- 1=多任务模式（Issue Agent 应拆分子任务）
    scene_user_id INTEGER DEFAULT 0,           -- 用户级场景的创建者 ID（0=非用户级）
    updated_at   TEXT DEFAULT (datetime('now')),
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (repo_key, issue_id)
)
```

**两表关键字段对照**

| 含义 | `tasks`（任务中心） | `issue_state`（pipeline） |
|------|--------------------|--------------------------|
| 主键 | `id`（自增） | `(repo_key, issue_id)` 唯一 |
| 关联 Issue | `issue_number`（INT，无 # 前缀） | `issue_id`（TEXT，`#N`） |
| 子任务模式 | —（不在此表） | `parent_issue_id` / `is_subtask_mode` |
| 场景 | —（经 issue_number 关联） | `scene_id` / `scene_user_id` |
| 状态 | `status`（任务中心可读） | `stage`（pipeline 阶段）+ `owner_agent` |
| 触发者 | `created_by` / `user_id` | `assignee` / `a2a_seq` |
| 完成 | `completed_at` / `completed_reason` | `stage='done'` |

### 1.1.3 两表合并方案（新表 `issue`）

> 需求：`tasks` 与 `issue_state` 本质都描述**同一个 GitCode Issue**（一个存任务中心状态、一个存 pipeline 阶段），
> 可融合为一张 **`issue` 表**，同时包含 `status`（任务中心）与 `stage`（pipeline），与对外 `/api/issues` 语义一致。

**拆解：为什么两表能合成一张**

| tasks 独有（任务卡片） | issue_state 独有（pipeline） | 可合并 |
|----------------------|------------------------------|--------|
| `status`（任务中心状态） | `stage`（pipeline 阶段） | ✅ 同表两列 |
| `task_description`（Issue body） | `scene_id / scene_user_id`（场景） | ✅ 合并 |
| `completed_reason`（完成原因） | `owner_agent / a2a_seq`（当前 agent） | ✅ 合并 |
| `conversation_id`（对话） | `parent_issue_id / is_subtask_mode`（子任务） | ✅ 合并 |
| `created_by / user_id / created_at` | `created_at / updated_at / assignee` | ✅ 合并 |

**目标表结构（`issue`）**

```sql
CREATE TABLE IF NOT EXISTS issue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key         TEXT NOT NULL,
    issue_id         TEXT NOT NULL,               -- "#5"（与 events.issue_id 对齐）
    issue_number     INTEGER,                     -- 5（冗余存数字，便于查询/展示）
    title            TEXT DEFAULT '',
    body             TEXT DEFAULT '',             -- Issue body（原 task_description）
    description      TEXT DEFAULT '',             -- 描述（可选扩展）
    conversation_id  TEXT DEFAULT '',             -- 对话回话
    status           TEXT DEFAULT 'active',       -- 任务中心状态：active/processing/awaiting_input/pending_review/completed/cancelled
    stage            TEXT DEFAULT 'vod',          -- pipeline 阶段：vod/issue/coding/qa/sre/pending_review/done
    owner_agent      TEXT DEFAULT 'vod',          -- 当前归属 agent
    assignee         TEXT DEFAULT '',             -- Issue 责任人
    a2a_seq          INTEGER,                     -- 触发当前 stage 的 A2A 事件 seq
    scene_id         TEXT DEFAULT '',             -- 场景
    scene_user_id    INTEGER DEFAULT 0,
    parent_issue_id  TEXT DEFAULT '',             -- 父 Issue（#20）
    is_subtask_mode  INTEGER DEFAULT 0,           -- 多任务模式
    allow_scene_override INTEGER DEFAULT 0,       -- VOD 场景覆盖开关
    completed_reason TEXT DEFAULT '',
    created_by       TEXT DEFAULT '',
    user_id          INTEGER DEFAULT 0,
    status_updated_at TEXT DEFAULT '',            -- 任务中心状态更新时间（可选）
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now')),
    completed_at     TEXT DEFAULT NULL,
    UNIQUE (repo_key, issue_id)
)
```

**合并收益**
- 对外 `/api/issues` 单表即 `stage + status + events_count` 全出，无需 join
- 消除 `tasks.id == issue_state 行` 的"一 Issue 两行"冗余与手写同步（`_STAGE_TO_TASK_STATUS`）
- 状态语义统一：`status`（用户可读）+ `stage`（机器驱动）同表共存

**合并影响面（重构成本）**

| 消费方 | 改动 | 数量 |
|--------|------|------|
| `session_store.set_issue_stage/update_task_status` | 改为单表 update | 中 |
| `routes/tasks.py`（任务中心 CRUD/状态流转） | `tasks.*` → `issue.*` | 大 |
| `routes/pipeline.py` / `routes/events.py` | join 简化 | 中 |
| `im/handler_async.py` / `event_pipeline.py` / `service.py` / `cleanup.py` | 表名/列名替换 | 中 |
| `prompt_builder.py` / `repogit.py`（读 scene_id） | 表名替换 | 小 |
| `onboarding.cleanup_stale_repo_data`（清残留） | 表名替换 | 小 |
| 前端 `tasks.js`（状态展示） | 读 `issue.*`（字段名基本不变） | 小 |

**建议过渡（一次性方案）**
1. **新表 `issue` 先建**（`db_setup`），存量 `tasks`/`issue_state` 数据**一次性迁移**（按 repo_key+issue_id 合并）。
2. **写路径切换**：`set_issue_stage` / 任务状态流转先写 `issue`（双写容忍期可选）。
3. **读路径渐进**：任务中心/pipeline/issue API 改读 `issue`，旧表停止写入后 `DROP`（保留审计备份）。
4. `events.issue_id`、`agent_tasks.issue_id` 不变（仍以 `#N` 关联 `issue.issue_id`）。
5. 全量回归：任务中心建/改/催/完成/取消、pipeline 各阶段、Issue 详情 API。

### 1.1.4 渐进式迁移方案（**推荐**：tasks 改名 issue，逐步收编 issue_state）

> 替代一次性迁移的更平滑方案：**先把 `tasks` 表 `ALTER TABLE ... RENAME TO issue`**（数据结构原样保留、数据零迁移），
> 之后**逐步 `ADD COLUMN` 收编 `issue_state` 的字段**，最后一并停用并 DROP `issue_state`。

**核心思路**

```
Phase 1  表改名      ALTER TABLE tasks RENAME TO issue（结构不变，数据原地）
Phase 2  字段收编    分批 ADD COLUMN stage/owner_agent/a2a_seq/scene_id/... 
Phase 3  读写切换    写全走 issue；读 issue 优先、issue_state fallback
Phase 4  收尾        issue_state 停写 → 只读 → 合并验证 → DROP
```

**优点（对比一次性新建）**

| 维度 | 一次性新建 issue 表 | 改名渐进式（推荐） |
|------|--------------------|-------------------|
| 数据 | 需迁移合并（tasks + issue_state → issue） | **零迁移**（RENAME 数据原地） |
| 主键/id | 新表新 id | **保留原 tasks.id** |
| 风险 | 集中一次，回滚难 | 分批上线，每批可验证/回滚 |
| 与 API 一致性 | 需新建/join | **表名即 issue，天然对齐 /api/issues** |
| 前端 | 字段名需核对 | **API/字段名不变，前端无感** |
| 开发量 | 大（一次动 8+ 文件） | 小（先 rename，逐步 ALTER） |

**Phase 1 细节（表改名）**
- `ALTER TABLE tasks RENAME TO issue`（SQLite 支持；`db_setup._migrate_tables` 已有多处 RENAME 先例）。
- `_TASKS_DDL` → `_ISSUE_DDL`：`CREATE TABLE IF NOT EXISTS issue`；幂等：已存在 `issue` 则跳过 rename。
- 无外键依赖（SQLite 表无强 FK），rename 安全。
- `agent_tasks` **不动**（执行实例表，命名清晰，勿混淆）。

**Phase 2 字段收编批次建议（每批独立可上线）**
| 批 | 收编字段（ADD COLUMN） | 收编自 issue_state |
|----|----------------------|-------------------|
| A | `stage`（默认 vod）、`owner_agent`（默认 vod） | pipeline 阶段/当前 agent |
| B | `scene_id`、`scene_user_id`、`allow_scene_override` | 场景 + VOD 覆盖开关 |
| C | `a2a_seq`、`parent_issue_id`（对齐命名）、`is_subtask_mode`（对齐） | A2A 触发/子任务 |
| D | `body`（收编 task_description 语义）、`status_updated_at` | 正文/状态时间 |
| E | （保留字段） | assignee 已有，无需收编 |

> 注意：`tasks` 已有补列 `parent_issue_number / is_subtask_mode`（L2619-2620），与 issue_state 的
> `parent_issue_id / is_subtask_mode` **命名/语义有重叠**——收编时统一为 issue_state 命名（`parent_issue_id`），
> 旧列弃用或改列名，避免双语义。

**Phase 3 读写切换（关键）**
- **写**：rename 后新逻辑全写 `issue`；`issue_state` 在字段收编完成前**保留为同步落点或停止写入**（选择：若仍由 pipeline 写 issue_state，则双写过渡）。
- **读**：优先 `issue`，尚未收编的字段 fallback `issue_state`（`issue.status / issue.stage` 有则用，无则 `issue_state.stage`）。
- 提供 `_ensure_column(issue, ...)` 逐批补列（已有模式，L2619-2620）。

**Phase 4 收尾**
- `issue_state` 停止写入 → 只读（历史兼容）→ 数据核对（issue 已含全部字段）→ `DROP TABLE issue_state`（保留备份）。

**影响面（相比一次性方案更可控）**
| 消费方 | 改动 |
|--------|------|
| `session_store.set_issue_stage` | `UPDATE tasks`→`UPDATE issue`；逐步扩字段 |
| `routes/tasks.py` 等 8 文件 SQL | `tasks` → `issue` 表名替换（字段名基本不变） |
| `db_setup` | `_TASKS_DDL`→`_ISSUE_DDL` + rename 迁移 + 补列 |
| 前端 | **无感**（API 响应字段不变） |
| `onboarding/cleanup` | 清残留表名 `tasks`→`issue` |

**风险与注意**
- 全局表名替换（8 文件 SQL），需完整回归任务中心 + pipeline。
- rename 前确认无同名 `issue` 表；`CREATE TABLE IF NOT EXISTS issue` 防重。
- `agent_tasks` 不参与（勿误改）。
- 迁移幂等：已有 `issue` 表且列全 → 跳过所有阶段。

**结论（更新）**：渐进式改名是**推荐主方案**（零迁移、分批发、可回滚、前端无感、表名与 /api/issues 对齐）；
一次性新建保留为**备选**（若需彻底重设计字段布局时考虑）。

> 新 Issues 接口聚合时：`/api/issues` 若落在 Phase 2/3 之间，可先 join `issue`（读 status）+ `issue_state`（读 stage）返回；
> 收编完成后单表 `issue` 直接出全量（stage + status + 其他）。

> 一次性方案与渐进式均以 `issue` 为最终表；`events.issue_id`、`agent_tasks.issue_id` 继续以 `#N` 关联 `issue.issue_id`。

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
| **Issue 阶段推进** | `issue_state.stage` 更新（owner_agent 流转） | `set_issue_stage` |
| **Issue 完结** | **最后节点（next=done）complete → `pending_review`** → 审核 → done | 状态机 + 开发者审核 |

### 1.4 当前对外 API 的错位（本设计要解决）

- `01-tasks.md` 的 "task" 混用了两种语义：`agent_tasks`（执行实例 id）+ `tasks` 表（Issue 级任务卡片，含
  issue_number/is_subtask_mode/parent_issue_number）。**API 侧 task_id 更多指执行实例，与 Issue 卡片表并列时易混淆。**
- `08-events.md` 是**平铺事件列表**（按 repo 列 seq），**不聚合 Issue 的 stage / 状态 / pipeline**。
- **缺少「按 Issue 看 pipeline」的对外整体视角** —— 正是本设计的缺口。

---

## 二、新增 Issues 接口（/api/issues）

### 2.1 设计目标

以 **Issue 为中心** 对外整体开放：
- Issue 的**阶段**（stage）、当前 agent（owner_agent）、场景（scene_id）
- 该 Issue 对应的**事件列表**（event 流）
- Issue 的 **pipeline 路线图**（event.json 结构化链）

### 2.1.1 归属澄清：`/api/tasks/create` 本质是「Issues 创建」

> 现有 `POST /api/tasks/create`（`01-tasks.md:18`）虽然挂在 `/api/tasks`，但：
> - body = `{repo_key, title, description, assignee, scene_id, is_subtask_mode}` —— 即**创建 GitCode Issue** 的输入
> - 行为：调 GitCode 建 Issue + 建档 `tasks` 行（`ensure_issue_task`）→ 触发 pipeline
> - **结论：这是「Issue 创建」接口，语义上应归属 `/api/issues`，而非 task。**

**设计建议**：
- **新增 `POST /api/issues`（创建）**：作为对外统一入口，语义清晰（对应 /api/issues 列表/详情）。
- **`POST /api/tasks/create` 直接废弃/迁移**：不保留兼容别名（避免双入口混乱）。调用方（CLI/Agent）统一改用 `/api/issues`；`01-tasks.md` 移除该端点或标注「已迁移至 /api/issues」。
- 实现复用现有逻辑（`ensure_issue_task` / GitCode create），仅路由按 `/api/issues` 暴露。

### 2.1.2 Issue/Event/Task 归属对照（接口层）

| 对外接口 | 实际操作对象 | 语义 |
|----------|-------------|------|
| `POST /api/tasks/create`（现） | 创建 GitCode Issue + 建档 | **Issue 创建**（→ 建议 /api/issues） |
| `GET /api/issues/...`（新） | Issue 聚合（stage/events/pipeline） | **Issue 视角** |
| `POST /api/tasks/complete` | 某次执行实例完成（session_key→event_seq） | **Event 执行完成**（非 Issue 完结） |
| `POST /api/tasks/{id}/confirm-done` | 开发者确认 Issue 阶段 → done | **Issue 完结确认**（stage: pending_review→done） |
| `POST /api/tasks/{id}/cancel` | 取消任务 → 置 cancelled + 停流水线 | 任务暂停/取消 |

### 2.2 端点设计

```
GET  /api/issues/{owner}/{repo}                    # 仓库下的 Issue 列表（含 stage 聚合）
POST /api/issues/{owner}/{repo}                    # 创建 Issue（新建，语义归位；复用 tasks/create 逻辑）
GET  /api/issues/{owner}/{repo}/{number}           # 单个 Issue 详情（stage + events + 摘要）
GET  /api/issues/{owner}/{repo}/{number}/events    # 该 Issue 的事件列表
GET  /api/issues/{owner}/{repo}/{number}/pipeline  # Issue pipeline 路线图（event.json 内容）
```

### 2.3 数据来源映射

| 字段 | 来源 | 说明 |
|------|------|------|
| issue 基本信息（title/status/created） | `events` 表首条 issue 事件 或 GitCode API | 首次发生时的标题 |
| **stage** | `issue_state.stage` | Pipeline 阶段（vod/issue/coding/qa/sre/done/pending_review） |
| **status** | `tasks.status` | **任务中心状态**（active/processing/awaiting_input/pending_review/completed/cancelled）；由 stage 映射同步，用户操作可覆盖 |
| **owner_agent** | `issue_state.owner_agent` | 当前执行的 agent |
| **a2a_seq** | `issue_state.a2a_seq` | 触发当前 stage 的 A2A 事件号 |
| **scene_id** | `issue_state.scene_id` | 场景 |
| assignee / subtask | `issue_state.assignee / is_subtask_mode / parent_issue_id` | 责任人、子任务 |
| event 列表 | `get_events_grouped_by_issue(repo)` 按 issue_id 过滤 | 全部事件（seq/type/agent/status/时间） |
| pipeline 段 | `load_event_json(repo, number)` | event.json 完整链（各段 structured output/logs） |
| 摘要 | `get_issue_summary(repo)` 对应段 | 概览 |

> 说明：`stage`（`issue_state`）是 pipeline 权威阶段；`status`（`tasks`）是任务中心可读状态。
> 二者经 `set_issue_stage → _STAGE_TO_TASK_STATUS` 同步，但 `awaiting_input/completed/cancelled` 可被用户操作直接设置。

### 2.4 现有可复用能力（改动小）

| 能力 | 位置 | 用途 |
|------|------|------|
| `get_events_grouped_by_issue(repo_key)` | `event_store.py:505` | 按 issue 聚类事件 |
| `get_issue_stage(repo_key, issue_id)` | `session_store.py:42` | stage/owner/assignee/a2a_seq/scene |
| `load_event_json(repo_key, number)` | `event_archive.py:249` | pipeline 段 / 结构化产出 |
| `get_issue_summary(repo_key)` | `event_store.py:531` | Issue 摘要文本 |
| `normalize_event_time` | `event_store.py` | 时间格式化 |

### 2.5 认证与契约

- 认证：`get_current_user_or_api_key`（三通道：JWT / `tk_` 会话 token / `grape_` API Key）
- 白名单：进 `custom_openapi`，CLI `grape issues ...` 可用
- 文档：新增 `help/api/11-issues.md`，更新 `README.md` 目录 + `tests/test_help_directory.py` `EXPECTED_API_FILES`

### 2.6 出参示例

```json
// GET /api/issues/{owner}/{repo}
{
  "issues": [
    {
      "number": 5,
      "title": "优化前端UI",
      "stage": "qa",
      "task_status": "processing",          // tasks.status 任务中心状态（待处理active/处理中processing/待澄清awaiting_input/待审核pending_review/已完成completed/已取消cancelled）
      "owner_agent": "qa",
      "scene_id": "default",
      "assignee": "grape_bot",
      "events_count": 6,
      "latest_event": {"seq": 102, "agent": "qa", "status": "completed", "created_at": "..."}
    }
  ],
  "total": 1
}

// GET /api/issues/{owner}/{repo}/5
{
  "number": 5, "title": "优化前端UI", "stage": "qa", "task_status": "processing",
  "owner_agent": "qa", "scene_id": "default", "assignee": "grape_bot", "is_subtask_mode": 0,
  "events": [
    {"seq": 96, "event_type": "issue_opened", "agent": "vod", "status": "completed", "created_at": "..."},
    {"seq": 102, "event_type": "pull_request_updated", "agent": "qa", "status": "completed", "created_at": "..."}
  ],
  "pipeline": {
    "issue": {"number": 5, "title": "...", "status": "running"},
    "summary": "已按审查意见修复 | 待 QA 重新审查",
    "status": "running",
    "segments": [
      {"seq": 96, "agent": "vod", "status": "completed", "output": {"conclusion": "...", "next_step": "a2a_issue"}},
      {"seq": 102, "agent": "qa", "status": "running", "output": {}},
      {"seq": 107, "agent": "sre", "status": "pending", "output": {}}
    ]
  }
}
```

---

## 三、实施范围

| # | 项 | 说明 | 优先级 |
|---|----|------|--------|
| 1 | 新增 `src/api/routes/issues.py` | `/api/issues` 5 个端点（list/create/detail/events/pipeline） | 必须 |
| 2 | 新增 `POST /api/issues/{owner}/{repo}`（创建） | 复用 tasks/create 逻辑（ensure_issue_task + GitCode create）；标记为 Issue 创建官方入口 | 必须 |
| 3 | `POST /api/tasks/create` 废弃/迁移 | 移除兼容别名，调用方统一改 `/api/issues`；`01-tasks.md` 同步说明 | 必须 |
| 4 | `main.py` 挂载 router | `include_router` | 必须 |
| 5 | `help/api/11-issues.md` | 接口文档 + **概念澄清表**（Issue/Event/Task + task complete≠issue complete + tasks/create 归属） | 必须 |
| 6 | `help/api/01-tasks.md` | 修正 create 措辞：标注「实为创建 Issue」 | 必须 |
| 7 | `help/api/README.md` | 目录加第 11 项 | 必须 |
| 8 | `tests/test_help_directory.py` | `EXPECTED_API_FILES` 加 `11-issues.md` | 必须 |
| 9 | `tests/test_issues_api.py`（新增） | stage/events/pipeline 聚合 + create 断言 | 必须 |
| 10 | OpenAPI 契约 | `custom_openapi` 自动导出 | 自动 |
| 11 | CLI `grape issues` | 从契约生成 | 独立 |

---

## 四、验收标准

- [ ] `GET /api/issues/{owner}/{repo}` 返回仓库 Issue 列表（含 stage/task_status/owner_agent/events_count/latest_event）
- [ ] `POST /api/issues/{owner}/{repo}` 创建 Issue（复用 tasks/create 逻辑，触发 pipeline）
- [ ] `GET /api/issues/{owner}/{repo}/{number}` 返回 stage + task_status + events + pipeline 聚合
- [ ] `GET .../{number}/events` 仅返回该 Issue 的事件列表
- [ ] `GET .../{number}/pipeline` 返回 event.json 结构化链（含各段 conclusion/next_step）
- [ ] `POST /api/tasks/create` 已废弃（不保留兼容别名），调用方统一走 `POST /api/issues`
- [ ] **文档 11-issues.md 含概念澄清表**：Issue/Event/Task 三层级、task complete ≠ issue complete、tasks/create 归属、`tasks.status` vs `issue_state.stage` 双状态
- [ ] 认证三通道可用；契约导出；CLI `grape issues` 可用
- [ ] 现有 events/tasks 接口不回归

---

## 五、测试用例设计

> 基础设施：沿用现有 API 测试模式（`tests/test_api_routes.py` / `test_auth_routes.py`）——
> `TestClient(app)` + 测试 DB + `_jwt_access_token` / `session_token` 注入 + monkeypatch 平台调用。
> 建议新增 `tests/test_issues_api.py`。用 `_mk_issue_state(repo, num, stage, ...)` / `_mk_event(...)` 构造数据。

### 5.1 仓库 Issue 列表（GET /api/issues/{owner}/{repo}）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T1 | `test_list_empty_repo` | 无事件/无 issue_state 时返回 `{issues:[], total:0}` | 空仓库 |
| T2 | `test_list_aggregates_stage` | 返回的 issue 含 `stage/owner_agent/scene_id/assignee/events_count/latest_event` | issue_state×2 + 各自 events×2 |
| T3 | `test_list_only_issue_scoped_events` | `events_count` 只统计该 issue 的事件（非全仓库） | issue A events×2 + issue B events×1 |
| T4 | `test_list_sorted_by_latest` | 按最新事件时间倒序；无事件 issue 的排序兜底 | 跨 issue 时间分布 |
| T5 | `test_list_requires_auth` | 未认证 401 | — |

### 5.2 创建 Issue（POST /api/issues/{owner}/{repo}）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T6 | `test_create_ok` | 返回 issue_number + 建档 `tasks` + GitCode create 被调 | mock GitCode create_issue |
| T7 | `test_create_missing_fields` | 缺 repo_key/title/description → 400 | — |
| T8 | `test_create_duplicate_title` | 同标题已存在 open Issue → 409 | 已有同标题 event |
| T9 | `test_create_gitcode_fail` | GitCode 建 Issue 失败 → 502 | mock 抛异常 |
| T10 | `test_create_trigger_pipeline` | 创建后触发 pipeline（event_seq 产生 / stage=vod） | mock webhook/事件入队 |

### 5.3 单个 Issue 详情（GET /api/issues/{owner}/{repo}/{number}）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T11 | `test_detail_not_found` | 无该 issue 数据 → 404 | — |
| T12 | `test_detail_aggregate` | 返回 stage + owner_agent + scene_id + events[] + pipeline{} | issue_state + events×2 + event.json |
| T13 | `test_detail_events_linked` | events[] 只含该 issue 的事件（seq/event_type/agent/status/time） | 混入其他 issue 事件 |
| T14 | `test_detail_no_issue_state` | 无 issue_state 但 events 存在 → stage 兜底 `vod` 或空 | 仅 events |
| T15 | `test_detail_pipeline_from_json` | pipeline 来自 `load_event_json`（各段 conclusion/next_step） | event.json 已生成 |

### 5.4 Issue 事件列表（GET /api/issues/{owner}/{repo}/{number}/events）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T16 | `test_events_scope` | 仅返回该 issue 的事件；按 seq 升序 | 混合 issue |
| T17 | `test_events_empty` | 无事件 → `{events:[], total:0}` | 有 issue 无 events |
| T18 | `test_events_issue_not_found` | issue 无任何记录 → 404 | — |

### 5.5 Issue pipeline（GET /api/issues/{owner}/{repo}/{number}/pipeline）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T19 | `test_pipeline_from_archive` | 返回 event.json 链（summary/status/segments[]） | 已归档 event.json |
| T20 | `test_pipeline_missing_archive` | 无 event.json → 降级：由 events 表重构成 segments 或返回空 pipeline | 仅 events 无文件 |
| T21 | `test_pipeline_segment_fields` | 每段含 seq/agent/status/conclusion/next_step（output） | 结构化 output 归档 |

### 5.6 兼容与回归

| # | 用例 | 验证点 |
|---|------|--------|
| T22 | `test_tasks_create_removed_migrated` | `POST /api/tasks/create` 已移除/返回迁移提示，不再作为创建入口 |
| T23 | `test_events_api_unchanged` | `/api/events` 平铺接口不回归 |
| T24 | `test_tasks_api_unchanged` | `/api/tasks` 既有接口不回归（complete/cancel/confirm-done） |
| T25 | `test_openapi_contract_exported` | `issues` 操作出现在 `openapi.json`（`gitrepo` 或 `/api/issues` 路径） |

### 5.7 必跑命令

```bash
python -m pytest tests/test_issues_api.py -v
python -m pytest tests/test_api_routes.py tests/test_auth_routes.py -q     # 回归
python -m pytest tests/test_help_directory.py -q                          # 文档契约
```

---

## 六、影响评估

| 项 | 说明 |
|----|------|
| 视角 | 从「按事件平铺」升级为「按 Issue 看 pipeline」，补全对外整体视图 |
| 概念 | 文档澄清 Issue/Event/Task，消除 task_id 混用歧义 |
| 工作量 | 中偏小：复用 `get_events_grouped_by_issue` / `get_issue_stage` / `load_event_json` |
| 兼容性 | 新增端点，`/api/events`、`/api/tasks` 不动 |

## 七、关键代码位置

| 位置 | 说明 |
|------|------|
| `src/api/routes/events.py` | 现有事件接口（平铺；不聚合 issue） |
| `src/api/event_store.py:505` | `get_events_grouped_by_issue`（新接口聚合基础） |
| `src/api/session_store.py:42` | `get_issue_stage`（stage/owner/a2a_seq/scene） |
| `src/api/event_archive.py:249` | `load_event_json`（pipeline 段） |
| `src/api/event_store.py:531` | `get_issue_summary`（摘要） |
| `src/api/db_setup.py` `_ISSUE_STATE_DDL` | `issue_state` 表（stage 权威字段） |
| `src/api/db_setup.py` `_TASKS_DDL` | `tasks` 表（status 任务中心状态字段） |
| `src/api/session_store.py:97-115` | `set_issue_stage → _STAGE_TO_TASK_STATUS`（stage→status 同步） |
| `src/api/main.py` | 路由挂载 |
| `help/api/README.md` | 目录（加第 11 项） |
| `tests/test_help_directory.py` | `EXPECTED_API_FILES` |