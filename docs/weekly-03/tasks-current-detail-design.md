# 获取当前任务详情接口方案（`GET /api/tasks/current`）

> 分支：`weekly-03-lyz`；状态：**已实施（2026-09-16：仅会话通道 + 三表组装 + 契约/测试看护）**；日期：2026-09-16
> 目标：记录「Agent 通过 session token 获取本次任务详情」的设计（评审后确认实施，见 §四/§七 落地）。
> 关联：`notify-close-session-token-design.md`（session token 语义）、`issues-list-merge-design.md`。

---

## 一、现状：Agent 如何获得本次任务信息

- 执行期系统注入 `GRAPE_SESSION_TOKEN`（`tk_`，`src/runtime/service.py:641`）：
  `session_key=task_id`、`repo_key`、`agent_type`、`user_id`（devops 事件任务 user_id=0）。
- Agent 已有 `grape api` 命令可调 `/api/...`，但**无「当前执行实例详情」端点**——运行时无法便捷地
  按需查询自己正处理的 repo/issue/事件/技能，任务信息只能依赖 prompt 内嵌（一次性、不可增量查）。

## 二、session token → 任务链路的定位

`get_current_user_or_api_key` 对 `tk_` 返回（`deps.py:85-98`）：
`session_key`（=agent_tasks.id）、`repo_key`、`agent_type`、`user_id`。

```
tk_ 会话
 └─ session_key ──────► agent_tasks.id（本次执行实例）
       + repo_key      ├─ agent_type / skill / prompt / status / event_seq / issue_id
       （作用域校验）   └─ issue_id + repo_key ──► issue 表（标题/描述/assignee/stage/status）
                                              └─ 关联 events（按 repo_key + #issue_number）
```

> `tasks/complete`（`tasks.py:1144-1166`）实现了**同款定位**（session_key 自动；它是 dual——但**本接口仅会话通道**，
> 因「自省自身任务」属 Agent 执行期内网语义，不由用户侧调用）。

### 2.1 数据表结构（`src/api/db_setup.py`）

**`issue` 表**（需求卡片，每 Issue 一行；基础列 + Phase B2 收编 pipeline/场景列）

```sql
CREATE TABLE IF NOT EXISTS issue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,  -- 内部 id（API /issues/{issue_id} 定位键）
    user_id          INTEGER NOT NULL,                   -- 创建者用户 id
    assignee         TEXT NOT NULL,                      -- 责任人（GitCode 用户名）
    repo_key         TEXT NOT NULL,                      -- 仓库（gitcode:/github: 前缀）
    issue_number     INTEGER NOT NULL,                   -- GitCode/GitHub Issue 编号
    title            TEXT DEFAULT '',                    -- 标题
    task_description TEXT DEFAULT '',                    -- 任务描述（Issue body 本地快照）
    conversation_id  TEXT DEFAULT '',                    -- 关联对话（chat 会话）
    status           TEXT DEFAULT 'active',              -- 需求中心状态：active/processing/awaiting_input/pending_review/completed/cancelled
    completed_reason TEXT DEFAULT '',                    -- 完成原因
    created_by       TEXT DEFAULT '',                    -- 创建人
    created_at       TEXT DEFAULT (datetime('now')),
    completed_at     TEXT DEFAULT NULL
    -- Phase B2 收编列（来自原 issue_state）：
    --   stage TEXT DEFAULT 'vod'         pipeline 阶段：vod/issue/coding/qa/sre/pending_review/done
    --   owner_agent TEXT DEFAULT 'vod'   当前归属 agent
    --   scene_id TEXT DEFAULT ''         Issue 级开发场景
    --   scene_user_id INTEGER DEFAULT 0  用户级场景创建者 ID
    --   allow_scene_override INTEGER DEFAULT 0
    --   a2a_seq INTEGER                  触发当前 stage 的 A2A 事件 seq
    --   parent_issue_id TEXT DEFAULT ''  父任务 Issue ID（如 "#20"）
    --   parent_issue_number INTEGER DEFAULT 0
    --   is_subtask_mode INTEGER DEFAULT 0 1=多任务模式
    --   updated_at TEXT DEFAULT ''
)
```

**`agent_tasks` 表**（每次执行实例，一次 Agent 引擎执行落一行）

```sql
CREATE TABLE IF NOT EXISTS agent_tasks (
    id                 TEXT PRIMARY KEY,       -- task_id（session_key 即它定位）
    agent_session_id   TEXT NOT NULL,          -- 外键 -> devops_agent_session.id
    runtime_id         INTEGER NOT NULL,       -- 确定路由 -> agent_runtimes.id
    event_seq          INTEGER,                -- 关联 events 表 seq
    issue_id           TEXT DEFAULT '',        -- 关联 event.json pipeline 段（'#N'）
    repo_key           TEXT DEFAULT '',        -- 仓库
    agent_type         TEXT DEFAULT '',        -- vod/issue/coding/qa/sre/chat
    skill              TEXT DEFAULT '',        -- 技能名
    prompt             MEDIUMTEXT NOT NULL,    -- 完整执行 prompt（含注入上下文，敏感）
    cwd                TEXT DEFAULT '',
    runtime_session_id TEXT DEFAULT '',
    event_type         TEXT DEFAULT '',
    account            TEXT DEFAULT '',
    user_id            INTEGER DEFAULT 0,      -- 执行用户（devops 事件=0）
    repo_gitcode_token TEXT DEFAULT '',
    user_gitcode_token TEXT DEFAULT '',
    env_extra          MEDIUMTEXT DEFAULT '{}',   -- JSON(含服务端注入凭据)
    skill_files        MEDIUMTEXT,                -- JSON 技能包交付
    server_url         TEXT DEFAULT '',           -- A2A 外呼地址
    issue_session_id   TEXT DEFAULT '',
    project_info_files MEDIUMTEXT DEFAULT '{}',   -- 项目知识随任务交付
    project_info_meta  MEDIUMTEXT DEFAULT '{}',
    knowledge_files    MEDIUMTEXT DEFAULT '{}',   -- 共享知识快照交付
    project_info_file_urls  TEXT DEFAULT '',
    knowledge_file_urls     TEXT DEFAULT '',
    pool_engine_ids    TEXT DEFAULT '',       -- 限定广播引擎列表
    status             TEXT DEFAULT 'queued', -- queued/dispatched/running/completed/failed/cancelled
    exit_code          INTEGER DEFAULT NULL,
    result_text        MEDIUMTEXT DEFAULT '', -- 自报完成结论/结构化产出
    errors             MEDIUMTEXT DEFAULT '',
    stderr             MEDIUMTEXT DEFAULT '',
    usage_json         MEDIUMTEXT DEFAULT '{}',
    started_at         TEXT DEFAULT NULL,
    completed_at       TEXT DEFAULT NULL,
    created_at         TEXT DEFAULT (datetime('now'))
)
```

> 关联链路：
> - `session_key`（`tk_`）= `agent_tasks.id` → 定位本次执行实例；
> - `agent_tasks.repo_key + agent_tasks.issue_id`（去 `#`）→ `issue` 行的 `issue_number` → 关联需求；
> - `events` 表按 `repo_key + issue_id(#'N')` 关联事件流（`_group_events_by_issue`）。

## 三、接口设计（仅会话通道）

> **决策（2026-09-16 评审）**：本接口**已实施**，**仅支持 session token**（`tk_`，scope=session_only），
> 不接受 `grape_` API Key / WebUI JWT——因为是「Agent 执行期自省自身任务」的会话内动作，不由用户侧调用。

```
GET /api/tasks/current              # tk_: session_key 自动定位当前执行实例
    → 200 {task: TaskDetail} | 404 | 403(作用域不匹配) | 401
```

### 3.1 认证：仅 session token

| 通道 | task_id 来源 | 校验 |
|------|-------------|------|
| `tk_` session token（**唯一**） | `user["session_key"]` **自动定位** | `user["repo_key"]` == 该 task 的 repo（`tasks.py:1160`） |

- 守卫用 `session_only`（对齐 git-repo 覆盖表：`_SESSION_ONLY` 登记 operation_id，或独立校验
  `auth_type=="session_token"`——见 `token-scope-classification-design.md`）。
- `grape_` / JWT → **403**（非会话通道）。
- 无 task_id 入参：定位完全由 session_key 派生，客户端零入参。

### 3.2 返回体组装（TaskDetail = agent_tasks + issue + events）

```json
{
  "task": {
    "id": "t_xxx", "status": "running", "agent_type": "coding",
    "skill": "issue-advance", "event_seq": 102,
    "result_text": "…", "errors": "…", "exit_code": 0,
    "started_at": "…", "created_at": "…", "completed_at": null,
    "repo_key": "gitcode:o/r", "task_description": "…",
    "issue": {
      "id": 101, "issue_number": 5,
      "title": "优化前端UI",
      "assignee": "alice", "stage": "coding", "status": "processing", "scene_id": "default",
      "url": "https://gitcode.com/o/r/issues/5"
    },
    "events_count": 6,
    "latest_event": {"seq": 102, "event_type": "pull_request_updated", "agent_type": "qa", "status": "completed", "created_at": "…"}
  }
}
```

- **`agent_tasks` 行**（by task_id）→ task 基础字段（id/status/agent_type/skill/event_seq/result_text/errors/exit_code/时间）
  + **`repo_key`** / **`task_description`**（任务归属元数据，与 `issue` 并列，平级属于 task）。
- **`issue` 行**（by `repo_key` + `agent_tasks.issue_id` 去 `#` 得 issue_number）→ issue 子对象
  （`id`/`issue_number` + title/assignee/stage/status/scene_id/`url`，复用 `get_issue_stage` 或查 issue 表）。
- **`issue.url`**：按 repo_key 平台前缀拼 gitcode/github 的 issue 链接（收敛在 issue 结构体内）。
- **`events`**（by repo_key + issue_key=issue_number，复用 `_group_events_by_issue`）→ events_count/latest_event。

### 3.3 prompt 不在返回体

- **`prompt` 不放入返回体**：prompt 是指令性/知识性内容（项目知识、共享知识、skill 指令、A2A 协议），
  Agent 在 Worker 工作目录已可直接读取（`{{SKILL_DIR}}` / `{{KNOWLEDGE_DIR}}` / 物化 prompt），无需服务端回传；
  且整段返回体积大、内嵌注入上下文有敏感面。
- 返回体仅含**结构化任务信息**（task + issue + events），不含 prompt 正文。

## 四、实现要点

| 项 | 做法 |
|----|------|
| 路由 | `GET /api/tasks/current`（操作_id `tasks_current`，tags=tasks），归属 `tasks.py`（Agent 执行动作，与 complete 同文件） |
| 守卫 | **仅 session token**：校验 `auth_type=="session_token"` 且 repo 作用域匹配；否则 403（对齐 `_SESSION_ONLY` 覆盖表语义） |
| 作用域 | 复用 complete 的 session 分支校验（`tasks.py:1160`）；可抽取 `_resolve_task_scope(user)` 供 complete/current 共用 |
| 关联查询 | `agent_tasks`（by id）→ `issue`（by repo_key+issue_number）→ `events`（`_group_events_by_issue`）；单 task 直查 |
| 契约 | **scope=session_only**（覆盖表登记 `tasks_current`，或独立会话守卫）；`test_openapi` 补断言 |
| 文档 | `01-tasks.md` 加「获取当前任务详情」小节（仅会话、返回） |

> ⚠️ **评审决策**：本接口**已实施**，本节即落地说明（见文末实施记录）。

## 五、影响与风险

| 项 | 说明 |
|----|------|
| 兼容 | 新增端点，无破坏 |
| 安全 | 仅返回调用者本任务（session 作用域校验）；prompt 默认隐藏 |
| 语义 | Agent 执行实例详情（与 `/api/issues/{issue_id}` 需求详情互补，不冲突） |
| CLI | `grape api GET /api/tasks/current` 可由契约生成（session 会话内用） |

## 六、验收（已实施）

1. `tk_` 调 `GET /api/tasks/current` → 返回本会话 task 详情（200），零入参自动定位。
2. `grape_`/JWT / 无 token → 403 / 401（仅会话通道）。
3. 作用域不匹配（token 的 repo ≠ 该 task 的 repo）→ 403；task 不存在 → 404。
4. 契约 `tasks_current` = **session_only**；`test_openapi` 全绿。
5. 返回含 task（**含 `repo_key`**）+ issue（含 issue_number/url）+ events_count/latest_event；**不含 prompt**。

## 七、测试用例看护（已实施）

> 除常规「验收」冒烟外，需在 `tests/` 落**自动化看护用例**，防止后续回归：

| 用例 | 归属文件 | 断言 |
|------|----------|------|
| 契约：`tasks_current` 路径 + scope=session_only | `test_openapi.py` | `EXPECTED_PUBLIC` 加 `("tasks_current", ("get", "/api/tasks/current"))`；`_SESSION_ONLY` 覆盖表登记 `tasks_current`；L2/L3 scope 抽样含它 |
| 仅会话：JWT/api_key 调 → 403 | `test_task_*.py` 或新 `test_tasks_current.py` | `self_complete_task` 同款（`session_token` 放行、`grape_`/JWT → 403） |
| 无 token → 401 | 同上 | `client.get("/api/tasks/current") == 401` |
| 作用域不匹配 → 403 | 同上 | 构造 `tk_`（repo=o/r）查 repo=x/y 的 task → 403 |
| task 不存在 → 404 | 同上 | `tk_` 查不存在 task_id → 404 |
| 自动定位（零入参） | 同上 | 造 agent_tasks + 会话 token（session_key=该 task_id）→ 200 且返该 task |
| 组装完整性 | 同上 | 返回含 `task.repo_key` / `task.issue.*`（issue_number/url）/ `events_count`/`latest_event`；**无 `prompt`** |
| 数据修复防护（若含 issue 必填） | 同上 | 断言返回不因某字段缺失 500（如 issue 行缺失时仍 200 且 `issue: null` 或跳过） |

> **看护目的**：该接口是「Agent 执行期自省」核心，回归破坏会让技能无法获知任务上下文；
> 契约（L1/L2/L3）+ 行为用例并行，防 scope 漂移与字段损坏。

### 实施记录（2026-09-16）

- `openapi.py`：`_SESSION_ONLY_OPERATIONS` 登记 `tasks_current`（scope=session_only）。
- `tasks.py`：新增 `GET /api/tasks/current`（仅 session token；session_key 自动定位；agent_tasks → issue → events 三表组装；不含 prompt）。
- `test_openapi.py`：`EXPECTED_PUBLIC` 加 `tasks_current`；session_only 抽样含它。
- `test_tasks_current.py`（新增）：8 用例（自动定位/JWT·api_key 403/无 token 401/作用域 403/404/issue 组装完整/issue 缺失兜底）。
- `01-tasks.md`：加「获取当前任务详情」小节；`help.html` 重新生成。
- 回归：`test_tasks_current` + `test_openapi` 15 passed。