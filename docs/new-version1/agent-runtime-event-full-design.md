# Agent Runtime + Event 档案 完整设计文档

> 版本：v1.0
> 日期：2026-08-05
> 刷新：2026-08-08（基于 main@13709ee 代码结构校准）
> 状态：待评审
> 前置：`agent-runtime-event-design.md`（v0.1 设计稿）
> 说明：本文为设计文档，含 UI 原型图；技术实现细节见同目录 `agent-runtime-event-tech-impl.md`。

---

## 〇、现状基线核对（2026-08-08，基于 main 代码实测）

> 详见 `agent-runtime-design.md` §0（backend 引用量实测约 675 处）。本文档引用的代码结构现状要点：
> - 前端实际目录 **`src/api/static/`**（SPA：`index.html` + `js/app.js` + `js/pages/*.js`）；导航 `index.html:61` `#nav-backends`，现有文案即 "Backend Runtimes"（`app.js:234`）。
> - VCS 适配层实际为 `VcsBackend`/`GitCodeBackend`/`GitHubBackend`（`src/vcs/`），**不迁移**。
> - `events` 表含 `backend` TEXT 列（`db_setup.py:20`）；`chat_agent_session` 含 `backend` TEXT + `backend_id` INTEGER。
> - `stream_agent`（`executor.py:106`）现 yield 字符串事件，`tool_use` 为格式化字符串（对应 §1.1 问题 2 的"tool_use 丢失"）；`_execute_agent`（`event_pipeline.py:323`）后台线程本地执行。
> - `evaluate_agent_output`（`event_store.py:98`）正则 + tool_use 判定；`get_issue_summary`（`event_store.py:301`）单行流；`build_prompt`（`prompt_builder.py:224`）。
> - chat 现有 `POST /send` 与 SSE `POST /send/stream`（`chat.py:182`），直接调用 `stream_agent`（`chat.py:125/133/217`）。
> - `/api/events/report` 当前**不存在**（`events.py` 仅有 `GET /detail/{seq}` / `GET /{owner}/{repo}`）。
> - `requirements.txt` 无 `requests`（tech-impl `RuntimeClient` 若用 `requests` 需新增依赖，或改用 `aiohttp`）。

---

## 一、背景与目标

### 1.1 背景

devops-bot 当前采用 **Bot 进程内直接调度 Agent** 的模式：

```
GitCode Webhook / 飞书 IM
        │
        ▼
event_pipeline.py / chat.py ──► src/agent/executor.stream_agent()
                                      │
                                      ▼
                                本地 Popen CLI（opencode / codearts）
```

问题：
1. **事件内容空洞**：`events` 表 `result` 是正则匹配的粗糙结论，非 Agent 自述；tool_use 数据在 `stream_agent` 中丢失（yield 字符串 → 收集为空 `{}`）；日志截断，下游 Agent 拿不到完整过程。
2. **无法远端执行**：Agent 只在 bot 进程内 spawn，没有 server 侧 runtime 注册 / 心跳 / 任务队列 / 远端 dispatch。
3. **事件未成消费输入**：`get_issue_summary` 仅生成单行流，`build_prompt` 历史注入极简，前序 Agent 做了什么/结论/产出对下游不可见。

### 1.2 目标

1. **控制平面（Runtime）**：引入 runtime 注册 / 心跳 / 健康态 / 任务队列，Agent 执行具备可观测性与可恢复性；支持**远端 RuntimeAgent 边端**（仅外连方式接入）。
2. **内容平面（Event 档案）**：每个 Issue 维护一份高质量 `event.json`，记录该 Issue 全部 Agent 处理全过程（触发、输入、处理过程、结构化产出、A2A 接力、聚合结论），作为后续 Agent 的关键输入。
3. **分层统一**：任务生命周期（`queued→running→complete/fail`）驱动 event.json 生命周期（`init→append→finalize`），本地/远端零差异。
4. **结构化产出**：Agent 完成时输出结构化 JSON 块（`actions/conclusion/artifacts/next_step/issue_summary`），解析后写入 event.json；`events` 表保留 result 结论文本供列表页零成本展示。

### 1.3 非目标（本期不做）

- 不引入 WebSocket 加速唤醒（HTTP 轮询 claim，WS 列为后续演进）。
- 不改变 GitCode Webhook / 飞书 IM 的事件入口与 skills 体系。
- 不重写 opencode/codearts 的 CLI 参数构建逻辑。
- 不引入外部 MQ / Redis（任务队列落 SQLite）。

---

## 二、总体架构

### 2.1 分层模型

```
┌────────────────────────────────────────────────────────────────┐
│  控制平面（Runtime）——谁执行                                     │
│  · runtime_credentials / agent_runtimes / agent_tasks          │
│  · register / heartbeat / claim / report                       │
│  · 确定路由、健康态、任务生命周期                                 │
├────────────────────────────────────────────────────────────────┤
│  内容平面（Event 档案）——记录什么                                 │
│  · event.json（每 Issue 一份）                                  │
│  · init / append_log / finalize                                │
│  · 结构化产出 + summary 机制                                     │
└────────────────────────────────────────────────────────────────┘
        ▲                          │
        └────── 任务生命周期驱动 ────┘
```

### 2.2 架构图

```
GitCode Webhook / 飞书 IM / A2A
        │
        ▼
event_pipeline / chat
   ├─(1) append_event ──▶ events 表（seq，审计）
   ├─(2) init_event_json ──▶ event.json 建/追加 pipeline 段
   └─(3) enqueue_task ──▶ agent_tasks（带 event_seq + issue_id 快照）
        │
        ▼
┌──────────────────────────────────────────────┐
│ Runtime Service（控制平面，bot 进程内）          │
│  register / heartbeat / claim / report        │
│  agent_runtimes + runtime_credentials +        │
│  agent_tasks（权威队列，确定路由）               │
└───────────────┬──────────────────────────────┘
                │ claim（按 runtime_id 过滤）
                ▼
      RuntimeAgent（本地 worker 或远端边端，同协议）
         │ 执行 stream_runtime（NDJSON 流式）
         │ POST /tasks/{id}/messages（批量 NDJSON）
         │ POST /tasks/{id}/complete|fail（结构化 output）
         ▼
┌──────────────────────────────────────────────┐
│ 服务端 Task Ingestion Handler（内容平面）        │
│  messages    → append_event_archive_log       │
│  complete/fail → 解析 output → finalize_event_archive
│               → update_event_complete（表强一致）
│               → 更新 issue_summary（有则覆盖）
└──────────────────────────────────────────────┘
```

### 2.3 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | 同仓库管理：RuntimeAgent 建在 `src/runtime/`，`python -m runtime.agent` | 不拆独立包，降低运维面 |
| D2 | 独立凭据表 `runtime_credentials`，`rta_` token | 凭据隔离，可吊销 |
| D3 | 确定路由：任务经 `devops_agent_session.runtime_id` 绑定 runtime | 不跨机器抢任务 |
| D4 | 纯 HTTP 轮询 claim，本版无 WebSocket | 先保证正确性 |
| D5 | chat 流式改"入队 + 轮询任务状态" | 接受打字机效果降级 |
| D6 | 任务队列落 SQLite（agent_tasks） | 与现有 SQLite 一致 |
| D7 | 健康态按 runtime 设备派生（status + last_seen_at） | 对齐 multica |
| D8 | 保留 stream_agent 的 NDJSON 流式逻辑，仅加回报包装 | 最大化复用 |
| D9 | 同 session 严格串行：同一 agent_session_id 最多 1 个活跃 task | 保护共享会话上下文 |

---

## 三、术语重构（backend → runtime）

### 3.1 重命名映射表

**Python 代码（src/ 与 tests/）**

| 旧 | 新 | 涉及文件 |
|---|---|---|
| `AgentBackend` | `AgentRuntime` | `src/agent/base.py` |
| `register_backend()` | `register_runtime()` | `src/agent/executor.py` |
| `get_backend()` | `get_runtime()` | `src/agent/executor.py` |
| `discover_backends()` | `discover_runtimes()` | `src/agent/executor.py` |
| `stream_agent()` | `stream_runtime()`（保留别名） | 各调用方 |
| `_BACKENDS` | `_RUNTIMES` | `src/agent/executor.py` |
| `backend_agents` 表 | `agent_runtimes` 表 | `db_setup.py` / `user/db.py` |
| `backend_id` | `runtime_id` | 各查询/SQL |
| `backend_session_id` | `runtime_session_id` | 各查询/SQL |
| `default_backend` | `default_runtime` | `agents.py` / `session_manager.py` |
| `events.backend`（TEXT，`db_setup.py:20`） | `events.runtime` | `db_setup.py` / `event_store.py` |
| `get_agent_backend()`（`config.py:109`） | `get_agent_runtime()` | `config.py` |

**API 路由**

| 旧 | 新 |
|---|---|
| `GET/POST /api/settings/backend-agents`（GET 返回 `{"backends": [...]}`，`settings.py:178`） | `GET/POST /api/settings/agent-runtimes` |
| `GET /api/settings/backend-agents/refresh` | `GET /api/settings/agent-runtimes/refresh` |
| `/api/agents/...` 返回体 `backend` 字段 | `runtime` 字段 |

**前端 static（实际目录 `src/api/static/`）**

| 旧 | 新 |
|---|---|
| `src/api/static/js/pages/backends.js` | `src/api/static/js/pages/runtimes.js` |
| 导航 `#nav-backends` / `nav('backends')`（`index.html:61` / `app.js:3`） | `#nav-runtimes` / `nav('runtimes')` |
| 现有文案 "Backend Runtimes"（`index.html:61` / `app.js:234`） | "Agent Runtimes" |

> ⚠️ `VcsBackend` / `GitCodeBackend` / `GitHubBackend`（`src/vcs/`）属于 VCS 适配层，**不迁移**。

---

## 四、数据模型

### 4.1 `runtime_credentials`（独立凭据）

```sql
CREATE TABLE IF NOT EXISTS runtime_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL,             -- sha256(rta_xxx)，不存明文
    name         TEXT NOT NULL,             -- 管理端可读名（如 "CI-Worker-01"）
    device_name  TEXT DEFAULT '',
    scope        TEXT DEFAULT 'global',
    owner_id     TEXT DEFAULT '',
    status       TEXT DEFAULT 'active',     -- active / revoked
    last_seen_at TEXT DEFAULT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (token_hash)
);
```

生成流程：管理端 `POST /api/settings/runtime-credentials` → 返回 `rta_` token（**仅展示一次明文**）→ 存 sha256。吊销 = 置 `status='revoked'`。

### 4.2 `agent_runtimes`（注册/健康态）

```sql
CREATE TABLE IF NOT EXISTS agent_runtimes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER,
    name          TEXT NOT NULL,            -- opencode / codearts / hermes / cursor / custom
    display_name  TEXT DEFAULT '',
    provider      TEXT DEFAULT '',
    binary        TEXT DEFAULT '',
    cli_path      TEXT DEFAULT '',
    version       TEXT DEFAULT '',
    healthy       INTEGER DEFAULT 0,
    installed     INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'offline',   -- online / offline / recently_lost / about_to_gc
    last_seen_at  TEXT DEFAULT NULL,
    scope         TEXT DEFAULT 'global',
    owner_id      TEXT DEFAULT '',
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (name, scope, owner_id)
);
```

### 4.3 `agent_tasks`（任务队列，+ event 关联快照）

```sql
CREATE TABLE IF NOT EXISTS agent_tasks (
    id                TEXT PRIMARY KEY,
    agent_session_id  TEXT NOT NULL,        -- 外键 → devops_agent_session.id
    runtime_id        INTEGER NOT NULL,     -- 确定路由 → agent_runtimes.id
    event_seq         INTEGER,              -- ★ 关联 events 表 seq
    issue_id          TEXT DEFAULT '',      -- ★ 关联 event.json pipeline 段
    repo_key          TEXT DEFAULT '',
    agent_type        TEXT DEFAULT '',
    skill             TEXT DEFAULT '',
    prompt            TEXT NOT NULL,
    cwd               TEXT DEFAULT '',
    runtime_session_id TEXT DEFAULT '',
    event_type        TEXT DEFAULT '',
    account           TEXT DEFAULT '',
    user_id           INTEGER DEFAULT 0,
    repo_gitcode_token TEXT DEFAULT '',
    env_extra         TEXT DEFAULT '{}',      -- JSON（含服务端已注入的账号/用户凭据，见 skill-delivery-remote.md）
    skill_files       TEXT DEFAULT '{}',      -- ★ JSON: {skill_name: {rel_path: content}}，远端交付技能包
    server_url        TEXT DEFAULT '',        -- ★ A2A 外呼地址，worker 替换 {{SERVER_URL}}
    status            TEXT DEFAULT 'queued', -- queued/dispatched/running/completed/failed/cancelled
    exit_code         INTEGER DEFAULT NULL,
    result_text       TEXT DEFAULT '',
    errors            TEXT DEFAULT '',
    stderr            TEXT DEFAULT '',
    usage_json        TEXT DEFAULT '{}',
    started_at        TEXT DEFAULT NULL,
    completed_at      TEXT DEFAULT NULL,
    created_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_runtime ON agent_tasks(runtime_id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_session ON agent_tasks(agent_session_id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_event ON agent_tasks(event_seq);
```

> **不含 `output_json`**：结构化产出的唯一权威来源是 event.json。`result_text` 存 Agent 最终答复（complete 回传，含可解析的 JSON 块）；events 表 `result` 存解析后的结论文本供列表页展示。

### 4.4 `events` 表（审计，保留）

现状不变（DDL 在 `db_setup.py:8-31`）。`result` 字段继续存解析后的 Agent 结论文本；`backend` TEXT 列随术语重构更名为 `runtime`（见 §三）。

### 4.5 `event.json`（Issue 级档案，结构化产出唯一权威）

```
data/workspaces/{owner}/{repo}/issues/{issue_number}/event.json
```

Schema：

```json
{
  "issue": {"number": 5, "title": "登录超时", "body": "...", "status": "open", "creator": "alice", "created_at": "..."},
  "status": "completed",
  "created_at": "...", "updated_at": "...",
  "pipeline": [
    {
      "seq": 12, "agent": "issue", "event_type": "issue_opened",
      "runtime": "opencode", "skill": "issue",
      "status": "completed", "exit_code": 0, "duration": 12.3,
      "trigger": {"actor": "alice", "branch": "main", "raw_type": "Issue Hook"},
      "input": {"issue_id": "#5", "summary": "新 Issue #5: 登录超时"},
      "output": {
        "actions": ["labeled:bug", "assigned:@bob", "commented"],
        "conclusion": "分类为 bug，指派给 @bob",
        "artifacts": ["issue #5 labeled", "comment posted"],
        "next_step": "A2A 交办 coding Agent",
        "issue_summary": "Issue #5：登录超时 | 状态: 待开发 | 已完成: 分类bug | 下一步: coding"
      },
      "a2a_to": {"agent": "coding", "issue_number": "5", "pr_number": "", "context": "..."},
      "logs": [{"time": "...", "type": "step_start", "msg": "..."}],
      "started_at": "...", "completed_at": "..."
    }
  ],
  "summary": "Issue #5：登录超时 | 状态: 待开发 | 已完成: 分类bug | 下一步: coding"
}
```

### 4.6 数据关系

```
events 表（审计，1行=1事件）            event.json（档案，1份=1Issue）
   row(seq)  ◄─────────对应──────────►  pipeline[i](seq)
      ▲
      │ event_seq 快照
agent_tasks（任务，1行=1次执行）
      │ agent_session_id 外键
      ▼
devops_agent_session（绑定/配置，权威：runtime_id / runtime_session_id）
```

---

## 五、事件创建与任务入队（统一触发点）

### 5.1 触发点三连

`run_pipeline` / A2A / chat 触发时，同一触发点顺序执行：

```
1. append_event(...)          → events 表插入（status=running），返回 seq
2. init_event_json(repo_key, seq, evt, agent_type, backend, skill)
                              → event.json 建/追加 pipeline 段（trigger/input/summary 占位）
3. enqueue_task(
       agent_session_id, runtime_id,
       event_seq=seq, issue_id=...,
       prompt, cwd, runtime_session_id, env_extra, ...
   )                          → agent_tasks 插入（status=queued）
```

### 5.2 确定路由（D3）

```
(repo_key, agent_type)
      │ 管理端在 Agent 配置页选择 runtime（绑定设备）
      ▼
devops_agent_session.runtime_id → agent_runtimes.id
      │ 事件 / chat 触发
      ▼
enqueue_task：runtime_id = devops_agent_session.runtime_id（确定路由）
      ▼
agent_tasks(status=queued) → 只有该 runtime 的 RuntimeAgent 能 claim 到
```

### 5.3 边界情况

- `issue_id = #0`（无关联 Issue 的 push 事件）→ **只写 events 表**，不建 event.json、不入队。
- PR 场景关联到 Issue → 写进该 Issue 的 event.json（`issues/{n}/` 统一收敛）。

---

## 六、任务生命周期与回报协议

### 6.1 生命周期

```
queued → dispatched → running → completed / failed / cancelled
```

### 6.2 Runtime 协议端点

| 端点 | 方向 | 用途 |
|---|---|---|
| `POST /api/runtime/register` | RuntimeAgent → server | 凭据校验（rta_）+ upsert agent_runtimes |
| `POST /api/runtime/heartbeat` | RuntimeAgent → server | 更新 last_seen_at / status |
| `POST /api/runtime/runtimes/{rid}/tasks/claim` | RuntimeAgent → server | 按 runtime_id 取队首任务或 null |
| `POST /api/runtime/tasks/{tid}/start` | RuntimeAgent → server | 标记 running |
| `POST /api/runtime/tasks/{tid}/progress` | RuntimeAgent → server | 阶段进度 |
| `POST /api/runtime/tasks/{tid}/messages` | RuntimeAgent → server | **批量 NDJSON 执行消息 → append_event_archive_log** |
| `POST /api/runtime/tasks/{tid}/usage` | RuntimeAgent → server | token 用量 |
| `POST /api/runtime/tasks/{tid}/complete` | RuntimeAgent → server | **完成（含结构化 output）→ finalize_event_archive**（幂等） |
| `POST /api/runtime/tasks/{tid}/fail` | RuntimeAgent → server | **失败 → finalize_event_archive**（幂等） |
| `GET  /api/runtime/tasks/{tid}/status` | 前端 / RuntimeAgent | 任务状态轮询 |
| `POST /api/runtime/runtimes/{rid}/recover-orphans` | RuntimeAgent → server | 启动时清理遗留任务 |

**认证中间件**：`Authorization: Bearer rta_...` → sha256 → 查 `runtime_credentials`（status=active）→ 上下文注入 credential_id。所有 `/api/runtime/*` 仅凭据通过，不开放 user token（D2）。

### 6.3 可靠性

- 终态回调（complete/fail）带指数退避重试（4s→8s→16s→32s→64s），5xx/408/429 才重试。
- runtime_gone 检测：claim/heartbeat 收到 404 → 清理 → 幂等重注册。
- 串行槽（D9）：claim 后检测同 agent_session_id 已有活跃 task → 跳过或置回 queued。

---

## 七、事件档案写入（Task Ingestion 驱动）

### 7.1 messages → append_event_archive_log

RuntimeAgent 执行中通过 `POST /tasks/{id}/messages` 批量上报 NDJSON 事件，服务端 ingestion handler 将每个事件映射为 event.json 的 `logs`（全量、脱敏后）写入对应 pipeline 段（按 event_seq 定位）。

### 7.2 complete/fail → finalize_event_archive

```
POST /tasks/{id}/complete（含 result_text / output）
  → evaluate_agent_output 增强版：优先解析 result_text 中的 JSON 块
     （actions/conclusion/artifacts/next_step/issue_summary）
  → finalize_event_archive：
      写 pipeline 段 output
      更新整链 status（completed/failed）
      覆盖顶层 summary（有 issue_summary 才覆盖，无则保持）
  → update_event_complete：同步 events 表（status/exit_code/duration/agent_logs/result）
  → 更新 devops_agent_session.runtime_session_id（如需）
```

### 7.3 本地/远端零差异

本地执行 = RuntimeAgent 跑在 server 同一台机（`server_url=http://localhost:3000`），同一套协议。本地任务也入队 → claim → 回报，与远端完全一致。

> 由此，event-archive 设计中的独立 `POST /api/events/report` **被本协议取代并移除**，避免双上报通道。

---

## 八、结构化产出契约

### 8.1 统一注入

`build_prompt` 末尾统一追加「处理记录输出要求」段（所有 skill 生效，一处维护）：

```
## 处理记录输出要求

完成当前任务后，在最终答复末尾输出以下 JSON 块（系统会解析写入该 Issue 的 event.json 档案）：

{
  "actions": ["做了什么，如 labeled:bug / assigned:@bob / 提交了PR / 发布了审查评论"],
  "conclusion": "处理结论/分析结果（简要）",
  "artifacts": ["产生的产物：PR#N / 评论 / 标签 / 部署 / Issue"],
  "next_step": "下一步建议；如需交办其他 Agent 则说明调用 A2A 的目标",
  "issue_summary": "Issue 最新聚合结论（覆盖档案 summary；紧凑文本，用 | 分隔）"
}

注意：
- 只输出一个 JSON 块，放在答复末尾。
- 若无法确定动作或结论，相应字段填空数组/空字符串，不要伪造。
- `issue_summary` 应反映截至当前链尾的最新状态，供下游 Agent 快速浏览。
```

### 8.2 解析

`evaluate_agent_output` 增强版：**优先**从 `final_text`/`result_text` 提取 JSON 块；解析失败走现有正则（降级兜底）。本地与远端共用同一契约。

---

## 九、summary 机制

| 项 | 规则 |
|----|------|
| 写入方 | 当前正在执行的 Agent（= 当前链尾），完成时输出块带 `issue_summary` |
| 覆盖策略 | 有则覆盖顶层 summary；无则保持旧值（不做系统聚合兜底） |
| 初始化 | 无任何写入时占位 `Issue #N 处理中，尚无结论` |
| A2A 传递 | 每次 A2A 调下游时自动附带最新 summary（下游零读取成本） |
| 形式 | 紧凑文本（`|` 分隔），便于 Agent 快速浏览与 prompt 注入 |

---

## 十、安全方案

### 10.1 本期（D2）

- `runtime_credentials` 独立凭据，`rta_` token，明文仅展示一次，存 sha256
- `/api/runtime/*` 认证中间件：仅凭据通过，凭据隔离
- 确定路由（D3）：任务只能被绑定 runtime claim

### 10.2 后续（M5）

- 任务级 `auth_token`（对照 multica `MULTICA_TOKEN`，只对单个任务有效）
- HTTPS mTLS 双向认证（`ssl_verify_client on`）
- WebSocket 加速唤醒（task_available 帧，对照 multica daemonws hub）

---

## 十一、UI 原型图

> 以下原型沿用现有 SPA（`src/api/static/js/pages/*.js`）的视觉风格与布局（Sidebar + TopBar + 内容区）。

### 11.1 Runtime 管理页（替换原 "Backend Runtimes" 页）

导航：Sidebar → **Settings ▸ Agent Runtimes**（原 `/backends` 改 `/runtimes`）

```
┌─ Sidebar ────────────────┬─ Top Bar ────────────────────────────────────┐
│  [Logo] Grape             │  Agent Runtimes · 所有设备                     │
│  Multi-Agent DevOps       │  [⟳ Refresh]  [＋ 生成凭据]                    │
│                          ├──────────────────────────────────────────────┤
│  Workspace               │                                               │
│    ■ Dashboard            │  设备列表（按健康态分组）                       │
│    ▶ Pipeline             │                                               │
│                          │  ┌─ 在线 (2) ──────────────────────────────┐ │
│  DevOps Agents            │  │ ● CI-Worker-01   Linux x64              │ │
│    ♫ VOD Agent            │  │   承载: opencode v1.5.2 · codearts v2.1  │ │
│    ⚠ Issue Agent          │  │   心跳: 12s 前 · 任务: 3 进行中 / 12 完成  │ │
│    ⚙ Coding Agent         │  │   [查看任务] [解绑]                       │ │
│    ✔ QA Agent             │  ├────────────────────────────────────────┤ │
│    ♡ SRE Agent            │  │ ● 本机 Runtime   Linux x64（本地）        │ │
│                          │  │   承载: opencode v1.5.2                  │ │
│  DevOps Skills            │  │   心跳: 4s 前 · 任务: 1 进行中 / 8 完成    │ │
│    ◈ Skills Library       │  └────────────────────────────────────────┘ │
│                          │                                               │
│  Settings                 │  ┌─ 刚失联 (1) ────────────────────────────┐ │
│    ⚙ General Settings     │  │ ○ dev-worker-02  已 3 分钟未心跳          │ │
│    ⎘ GitCode Repo         │  │   承载: codearts v2.1                    │ │
│    ⎘ Agent Runtimes ◀     │  └────────────────────────────────────────┘ │
│    ☎ Chat Agent           │                                               │
│                          │  ┌─ 离线 (1) ──────────────────────────────┐ │
│    ↩ Logout               │  │ ○ legacy-worker  已 2 天未心跳（即将清理） │ │
│                          │  └────────────────────────────────────────┘ │
└──────────────────────────┴───────────────────────────────────────────────┘
```

**凭据管理弹窗**（点击 [＋ 生成凭据]）：

```
┌─ 生成 Runtime 凭据 ───────────────────────────────────┐
│                                                       │
│  名称*  [ CI-Worker-03            ]                    │
│                                                       │
│  作用域 [● 全局  ○ 指定仓库 ]                          │
│                                                       │
│  ⚠️ 生成的 token 仅展示一次，请立即保存：               │
│  ┌────────────────────────────────────────────────┐  │
│  │ rta_eJx2k9sWpLm4nQv8rTz0xYc5hB1uGf6dAk3sP      │  │
│  └────────────────────────────────────────────────┘  │
│  [📋 复制]                                            │
│                                                       │
│  RuntimeAgent 启动命令：                               │
│  RUNTIME_TOKEN=rta_... python -m runtime.agent         │
│                                                       │
│                              [关闭]                    │
└──────────────────────────────────────────────────────┘
```

**任务列表（点击设备 [查看任务]）**：

```
┌─ CI-Worker-01 · 任务 ──────────────────────────────────┐
│  [全部] [排队 2] [进行中 3] [完成 12] [失败 1]            │
│  ┌──────────┬───────────┬────────┬────────┬─────────┐  │
│  │ Task ID  │ 仓库/Agent │ 状态    │ 时长   │ 结果     │  │
│  ├──────────┼───────────┼────────┼────────┼─────────┤  │
│  │ t_8f2k   │ dev/ops:   │ 🔄      │ 1m 12s │ 处理中…  │  │
│  │          │ coding     │ running │        │         │  │
│  │ t_8e1x   │ dev/ops:   │ ⏳      │ —      │ —       │  │
│  │          │ issue      │ queued  │        │         │  │
│  │ t_7zc9   │ chat:      │ ✅      │ 45s    │ 已回复   │  │
│  │          │ user_1001  │ completed│        │         │  │
│  └──────────┴───────────┴────────┴────────┴─────────┘  │
└────────────────────────────────────────────────────────┘
```

### 11.2 Agent 配置页绑定 runtime

导航：Sidebar → 任一 **DevOps Agent** 详情 → [Config] 面板

```
┌─ Coding Agent · Config ───────────────────────────────┐
│                                                       │
│  Runtime（执行设备）                                    │
│  [CI-Worker-01 (opencode) ▼]                          │
│     ├─ 本机 Runtime (opencode)  ← 默认                 │
│     ├─ CI-Worker-01 (opencode)                        │
│     └─ CI-Worker-02 (codearts)                        │
│  ◇ 仅显示在线 runtime；离线设备置灰不可选                │
│                                                       │
│  Skill（最多选一个，默认官方自带）                       │
│  [Default · skills/implementation/SKILL.md ▼]         │
│                                                       │
│  [Save] [Cancel]                                      │
└───────────────────────────────────────────────────────┘
```

> 选择 runtime 后写入 `devops_agent_session.runtime_id`，后续该 agent 的事件任务将**确定路由**到该设备执行。

### 11.3 Chat Agent 任务状态轮询

导航：Sidebar → Chat Agent

```
┌─ Conversations ──┬─ Main Chat ───────────────────────┬─ Config ──────┐
│ ＋ 新对话         │  Hello! I'm your assistant         │ Runtime:      │
│                  │                                    │ [CI-Worker-01 │
│ 新对话            │  ┌──────────────────────────────┐ │  ▼]           │
│ 2026-08-05       │  │用户: 检查 issue #342 状态      │ │              │
│                  │  └──────────────────────────────┘ │ Agents:       │
│ 需求讨论          │                                    │ ● VOD 47     │
│ 2026-08-04       │  ⏳ [任务执行中…]                    │ ○ Iss 3      │
│                  │  ┌──────────────────────────────┐ │ ○ Cod 1      │
│ 问题反馈          │  │[●] Agent 正在处理 (12s)…      │ │ ○ QA 2       │
│ 2026-08-03       │  └──────────────────────────────┘ │ ○ SRE v2.3   │
│                  │                                    │              │
│                  │  ┌─[Type message...]───────────┐  │ [Save]       │
│                  │  │                      [Send]  │  │              │
│                  │  └──────────────────────────────┘  │              │
└──────────────────┴────────────────────────────────────┴──────────────┘
```

**交互说明**：
- 发送消息 → `POST /api/chat/send` → 返回 `task_id`，不再 SSE 流式
- 前端 `setInterval` 轮询 `GET /api/runtime/tasks/{id}/status` → 展示「任务执行中…」→ 完成后展示 `result_text`
- 打字机效果由轮询间隔（~500ms）近似实现（D5 已知取舍）

### 11.4 Event Detail 展示结构化 output

导航：Agent 详情 → 某 Event → Event Detail

```
┌─ Top Bar ────────────────────────────────────────────┐
│  Event Detail · Coding Agent · team/backend-service   │
├──────────────────────────────────────────────────────┤
│  [← 返回 Coding Agent]                                │
│                                                       │
│  ┌─ Event Context ───────────┐  ┌─ Event Info ──────┐ │
│  │  Trigger: a2a_event        │  │ Runtime: CI-Worker│ │
│  │  Repository: dev/ops       │  │ Skill: implementation│ │
│  │  Issue: #5                 │  │ Status: ✅ Success │ │
│  │  Agent: coding             │  │ Duration: 12.3s   │ │
│  │  Time: 2026-08-05 10:12   │  │  ──────────────── │ │
│  │                            │  │  💬 Feedback       │ │
│  │  ## Agent 处理记录          │  │  ┌──────────────┐ │ │
│  │  ✅ 动作:                  │  │  │Title:        │ │ │
│  │    • 创建 PR #12            │  │  │Description:  │ │ │
│  │  ✅ 结论:                  │  │  │              │ │ │
│  │    完成需求实现             │  │  └──────────────┘ │ │
│  │  ✅ 产出: PR #12           │  │  [提交反馈]        │ │
│  │  ▶ 下一步: QA 审查 PR #12   │  └─────────────────┘ │
│  │                            │                       │
│  │  ── 执行日志（logs）──      │                       │
│  │  [10:00:01] Step 1 started │                       │
│  │  [10:00:03] tool: bash…    │                       │
│  │  [10:00:09] 分析中…         │                       │
│  └────────────────────────────┘                       │
└────────────────────────────────────────────────────────┘
```

> 数据来源：`GET /api/events/detail/{seq}` 附带 `archive` 摘要（读取 `data/workspaces/{owner}/{repo}/issues/{n}/event.json` 对应 pipeline 段）。`output.actions / conclusion / artifacts / next_step` 以结构化卡片展示，替代原先仅展示 `result` 文本。

### 11.5 Dashboard / Pipeline 富上下文（可选增强）

Pipeline 页某 Issue 展开时，展示该 Issue 的 `event.json` summary 与各段结论（来自 `build_issue_context`），替代单行 `agent:action✓` 流。

---

## 十二、代码改动清单

### 新增

| 文件 | 职责 |
|------|------|
| `src/runtime/__init__.py` | 模块 |
| `src/runtime/base.py` | `AgentRuntime` 抽象基类（原 AgentBackend 更名） |
| `src/runtime/registry.py` | `register_runtime()/get_runtime()/discover_runtimes()` |
| `src/runtime/executor.py` | `stream_runtime()`（原 stream_agent，tool_use 结构化 + 全量 text） |
| `src/runtime/opencode.py` / `codearts.py` | 内置 runtime |
| `src/runtime/service.py` | 服务端 RuntimeService（register/heartbeat/claim/report + task ingestion） |
| `src/runtime/agent.py` | RuntimeAgent 边端入口（`python -m runtime.agent`） |
| `src/runtime/client.py` | RuntimeAgent HTTP 客户端（外连 + 退避重试） |
| `src/runtime/health.py` | 健康态派生（online/recently_lost/offline/about_to_gc） |
| `src/api/event_archive.py` | event.json 读写模块（init/append_log/finalize/load/build_issue_context，锁 + 原子写） |

### 修改

| 文件 | 改动 |
|------|------|
| `src/agent/executor.py` | `stream_agent` tool_use yield 结构化 dict；text/reasoning/raw 全量；改名 `stream_runtime`（保留别名） |
| `src/api/event_pipeline.py` | `run_pipeline`：append_event + init_event_json + enqueue_task 三连；`_execute_agent` 拆分为入队侧（enqueue_task）+ 移除本地执行 |
| `src/api/event_store.py` | `evaluate_agent_output` 增强（解析 JSON 块）；新增 `parse_agent_output_json`；`get_issue_summary` 用富上下文回退 |
| `src/prompt_builder.py` | A2A 富上下文 + summary 附带；追加「处理记录输出要求」段 |
| `src/api/routes/chat.py` | send/stream 改"入队 + 轮询任务状态"（D5） |
| `src/api/routes/settings.py` | `discover_backends` → `discover_runtimes`；runtime-credentials 管理 |
| `src/api/routes/webhook.py` | 读/写 runtime 字段；**移除** `/api/events/report` |
| `src/api/routes/scenes.py` | backend 字段 → runtime 字段 |
| `src/api/db_setup.py` | 迁移（backend→runtime）+ 新表（runtime_credentials/agent_runtimes/agent_tasks + 索引） |
| `src/user/db.py` | backend_agents→agent_runtimes CRUD；runtime 字段读写 |
| 前端 `src/api/static/js/pages/backends.js`→`runtimes.js` | 更名 + Runtime 管理页 + 绑定下拉 |
| 前端 `src/api/static/js/pages/events.js` | 展示结构化 output |
| 前端 `src/api/static/index.html` | 导航 `#nav-backends` → `#nav-runtimes`（`index.html:61`）；引入 runtimes.js；`?v=` 缓存版本号提升 |
| 前端 `src/api/static/js/app.js` | VIEWS `'backends'` → `'runtimes'`（`app.js:3`）；标题文案（`app.js:234`） |
| 前端 `src/api/static/css/app.css` | 设备卡片 / 健康态徽标 / 凭据弹窗样式 |

### 移除

- `/api/events/report`（并入 runtime 回报协议）——注：当前代码库**不存在**此端点（`events.py` 仅 `GET /detail/{seq}` / `GET /{owner}/{repo}`），本项指"不引入"。

### 测试

- `tests/test_event_archive.py`：骨架/追加/finalize/#0 跳过/并发/原子写/summary 覆盖/build_issue_context
- `tests/test_runtime.py`：register/heartbeat/claim 确定路由/串行槽/终态幂等/recover-orphans
- `tests/test_event_store.py`：evaluate 解析 JSON 块降级
- 同步更名既有测试（排除 VCS backend）——涉及 `test_api_routes.py`（`test_backend_agents` 断言 `{"backends": [...]}`）、`ui_test_playwright.py`（`test_backend_page`）、`ui_scenario_test.py`（`test_s10_backends`）

---

## 十三、里程碑

| 阶段 | 内容 |
|------|------|
| **M0** | 术语重构 backend→runtime（§三）：模块/DB/API/前端/测试 |
| **M1** | 控制平面：`agent_tasks`(+event_seq/issue_id) + RuntimeService + 本地 worker；event.json 模块 + 结构化产出契约 |
| **M2** | 回报协议：`messages/complete/fail` 驱动 event.json + summary；移除 `/api/events/report`；event_pipeline 切到 enqueue_task |
| **M3** | 远端 RuntimeAgent：`runtime_credentials`(rta_) + `/api/runtime/*` 认证中间件 + `src/runtime/agent.py` 边端 |
| **M4** | 前端：Runtime 管理页 + Agent 配置绑定 + chat 轮询 + Event Detail 结构化展示 |
| **M5** | 后续：任务级 auth_token、WS 唤醒、HTTPS mTLS |

---

## 十四、风险与兜底

| 场景 | 处理 |
|------|------|
| 旧数据无 event.json | 全部回退现有逻辑（不迁移旧 events） |
| archive 写失败 | 仅 `log.warning`，不阻断（events 表为可靠底账） |
| 并发写 event.json | 每 repo 一把 `_lock` + 原子替换 |
| summary 无人写 | 保持旧值/占位，不生成劣质聚合 |
| Agent 输出非法 JSON | 正则降级兜底，不因解析失败丢产出 |
| token 泄露 | 写入前统一 `[REDACTED]` 脱敏 |
| runtime 离线 | 任务保持 queued，health 页可见"等待 runtime 上线"；不随机派发 |
| 串行污染 | D9：同 agent_session_id 严格串行 |
| chat 流式降级 | D5：入队 + 轮询，打字机效果由轮询实现（已确认接受） |

---

## 十五、后续演进（M5）

- WebSocket 加速唤醒（task_available 帧）
- 任务级 `auth_token`
- 自定义 RuntimeProfile（command_name + fixed_args）
- runtime 自动更新 / GC
- HTTPS mTLS 双向认证
