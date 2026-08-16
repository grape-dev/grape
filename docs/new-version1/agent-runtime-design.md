# Agent Runtime 接入设计（对齐 Multica Runtime 模式）

> 版本：v0.2（设计稿，含远端 RuntimeAgent）
> 日期：2026-08-03
> 刷新：2026-08-08（基于 main@13709ee 代码结构校准，见 §0）
> 状态：待评审

---

## 0. 现状基线核对（2026-08-08，基于 main 代码实测）

> 本节是落地前的现状快照，校准本文档中的代码引用与数据。实测方式：全库扫描 + 逐文件核对。

**术语引用量（`select-string` 全量扫描）**

| 范围 | `backend` 匹配数 |
|---|---|
| `src/**/*.py` | 约 474 处 |
| `tests/**/*.py` | 约 140 处 |
| 前端 `src/api/static/**/*.js/html` | 约 61 处 |
| **合计** | **约 675 处**（原稿"约 538 处"已过时） |

**代码结构现状（已核实）**

| 项 | 现状 |
|---|---|
| 前端目录 | 实际为 **`src/api/static/`**（`main.py:77` 挂载 `/static`），SPA = `index.html` + `js/app.js` + `js/pages/*.js`；**不存在顶层 `static/`** |
| 导航 | `index.html:61`：`<a id="nav-backends">⚡Backend Runtimes</a>`；`app.js:3` VIEWS 含 `'backends'`，`app.js:233` `nav('backends')` 标题为 "Backend Runtimes" |
| 后端页 | `src/api/static/js/pages/backends.js`（85 行）：引擎卡片 + 安装状态 + codearts AK/SK + 刷新；`GET /api/settings/backend-agents` 返回 `{"backends": [...]}` |
| VCS 适配层 | 实际命名为 `VcsBackend`（`src/vcs/base.py`）+ `GitCodeBackend`/`GitHubBackend`（`gitcode.py`/`github.py`）+ `register_vcs_backend`（`src/vcs/adapter.py`）；**无 `GitLabBackend` 实现文件**（仅 `tests/test_phase9.py` 测试引用） |
| Agent 引擎探测 | `discover_backends()`（`executor.py:26`）覆盖 opencode/codearts/hermes/cursor 四类，写入 `backend_agents`（含 `pip_package` 列） |
| `events` 表 | 含 **`backend` TEXT 列**（`db_setup.py:20`），与 `result` 并存 |
| `chat_agent_session` | 同时含 `backend` TEXT（默认 `'opencode'`，`session_manager.py:79`）与迁移补充的 `backend_id` INTEGER（`session_manager.py:94`） |
| `config.py` | `:109` `get_agent_backend()`（恒返回 `"opencode"`），属待更名点 |
| 未实现项 | `src/runtime/`、`src/api/event_archive.py`、`/api/runtime/*`、`/api/events/report` **均不存在**，全部为本版目标态 |
| 运行环境 | Python 3.14.3（SQLite ≥3.25，支持 `RENAME COLUMN`）；`requirements.txt` 无 `requests`（tech-impl §3.1 若用 `requests` 需新增依赖，或改用已依赖的 `aiohttp`） |

---

## 1. 背景与目标

### 1.1 背景

devops-bot 当前采用 **Bot 进程内直接调度 Agent** 的模式：

```
GitCode Webhook / 飞书 IM
        │
        ▼
event_pipeline.py / chat.py  ──►  src/agent/executor.stream_agent()
                                          │
                                          ▼
                                    本地 Popen CLI（opencode / codearts）
```

`src/agent/` 目前是"后端引擎注册表"模型：`AgentBackend` 抽象基类 + `_BACKENDS` 字典 + `register_backend()` 插件接口 + `discover_backends()` 本地探测。Bot 既是调度器又是执行器，任务就地 spawn，**没有 server 侧的 runtime 注册 / 心跳 / 任务队列 / 远程 dispatch**。

Multica（`D:\Developer\multica-bot\multica`）提供了一套成熟的 **控制平面(Server) 与 执行平面(Runtime/Daemon) 分离** 的 runtime 接入架构：安装在 worker 机器上的 daemon 即"仅外连的边缘客户端（VPN Client 式）"，主动外连 server，注册本机 agent CLI 为 runtime，轮询拉取任务并执行。

本文档：

1. 提炼 multica runtime 接入方式的要点（§2）。
2. 对比 devops-bot 现状，给出 Gap 分析（§3）。
3. 输出 devops-bot 引入 "Agent Runtime" 的设计方案（§4~§8）。
4. 给出 "backend agent → agent runtime" 的术语重构方案（§6）。

### 1.2 目标

- **术语对齐**：将 devops-bot 中 "backend agent" 统一更名为 "agent runtime"，与 multica 概念一致。
- **控制面引入**：新增 runtime 注册 / 心跳 / 健康态 / 任务队列，使 agent 执行具备可观测性与可恢复性。
- **远端执行（本版核心）**：引入独立于 bot 进程的 **RuntimeAgent 边端**（同仓库管理），安装到 worker 机器上，以"仅外连"方式接入 runtime service；本地部署 = RuntimeAgent 跑在 server 同一台机，同一套协议。
- **确定路由**：runtime worker 通过 runtime service 与 `devops_agent_session` 绑定，任务路由是确定的，不跨机器随机派发。
- **独立凭据**：runtime 使用独立凭据表（`rta_` token），不复用 user token。

### 1.3 非目标（本期不做）

- 不引入 WebSocket 加速唤醒（本版仅纯 HTTP 轮询 claim；WS 列为 §9 后续演进）。
- 不改变现有 GitCode Webhook / 飞书 IM 的事件入口与 skills 体系。
- 不重写 `src/agent/opencode.py` / `codearts.py` 的 CLI 参数构建逻辑。
- 不引入外部 MQ / Redis（任务队列落 SQLite）。

---

## 2. Multica Runtime 接入方式研究（对标物）

### 2.1 总体架构

```
┌─────────────┐    HTTP (Bearer token)    ┌──────────────────┐
│  Multica    │ ◄────────────────────────► │   Daemon (本地)   │
│  Server     │   register / heartbeat /   │   多 Runtime 宿主   │
│  (控制平面)  │   claim / report           │  spawn CLI 子进程   │
└─────┬───────┘   WebSocket (加速唤醒)      └──────────────────┘
      │
      │ DB: agent_runtime / agent_task_queue
      └──► Web UI (runtimes 页 / usage / activity / models / skills)
```

核心原则：**HTTP 拉取保证正确性，WebSocket 仅做低延迟唤醒提示**。

### 2.2 概念模型

| 概念 | 说明 | devops-bot 对应现状 |
|---|---|---|
| **Runtime**（RuntimeDevice / AgentRuntime） | 注册在 server 上、绑定 workspace 的"执行单元"，可承载多个 agent CLI；带 `status`(online/offline) + `last_seen_at` | `backend_agents` 表（静态记录，无在线态） |
| **Daemon / RuntimeAgent** | Runtime 的宿主进程（装在 worker 机器，仅外连） | 无（bot 进程即宿主） |
| **Task**（AgentTaskQueue） | server 与 runtime 之间流转的工作单元，有完整生命周期 `queued → dispatched → running → completed/failed/cancelled` | 无（事件→就地执行） |
| **RuntimeProfile** | workspace 级"自定义 runtime 定义"（command_name + fixed_args + protocol_family） | `register_backend()` 插件接口（进程内注册，非 DB 定义） |

### 2.3 关键协议（daemon ↔ server）

**认证**
- `Authorization: Bearer <token>`，支持 `mdt_`(daemon token) / `mul_`(PAT) / `mcn_`(cloud PAT) / JWT 四种回退（`middleware/daemon_auth.go`）。
- 身份头：`X-Client-Platform` / `X-Client-Version` / `X-Client-OS` / `X-Client-Capabilities`。
- 权限校验：daemon 只能访问自己 workspace 的 runtime / task（`handler/daemon.go` 的 `requireDaemonRuntimeAccess` / `requireDaemonTaskAccess`）。

**注册（register）** — `POST /api/daemon/register`
```json
{
  "workspace_id": "...", "daemon_id": "...", "device_name": "...",
  "cli_version": "...", "launched_by": "desktop",
  "runtimes": [{"name": "...", "type": "opencode", "version": "0.1.0", "status": "online", "profile_id": ""}],
  "failed_profiles": []
}
```
Server 端 `UpsertAgentRuntime` 幂等 upsert，返回 `runtimes[]`（server 权威 ID）+ `repos` + `settings`。Daemon 以 server 返回的 ID 为准维护本地 `runtimeIndex`。

**心跳（heartbeat）** — HTTP `POST /api/daemon/heartbeat`（约 15s）
- server 更新 `last_seen_at` / `status`。
- 离线判定：150s 无心跳 → 标记 offline（sweeper）；7 天 → GC 删除 runtime。
- 前端派生健康态：`online`(心跳内) / `recently_lost`(<5min) / `offline`(5min~7d) / `about_to_gc`(<1d 到 GC)（`derive-health.ts` 纯函数）。

**任务拉取（claim）** — `POST /api/daemon/runtimes/{runtimeId}/tasks/claim`
- 返回一个任务或 `{"task": null}`。每 runtime 一个独立 poller，poll interval + jitter 偏移避免惊群。
- **claim 前先占执行槽**（`waitForTaskSlot`）：容量不足时不 claim，防止任务卡在 `dispatched` 态被超时 sweep。
- claim 响应中携带完整任务上下文：agent(name/instructions/skills/custom_env/custom_args/model) + 触发上下文 + `auth_token`（任务级短效 token）。

**任务执行与回报** — `POST /api/daemon/tasks/{taskId}/...`
| 端点 | 用途 |
|---|---|
| `POST /tasks/{id}/start` | 标记 running |
| `POST /tasks/{id}/progress` | 阶段进度 (summary/step/total) |
| `POST /tasks/{id}/messages` | 批量 NDJSON 执行消息（seq/type/tool/content/input/output） |
| `POST /tasks/{id}/usage` | token 用量（provider/model/input/output/cache） |
| `POST /tasks/{id}/complete` | 完成（output/session_id/work_dir） |
| `POST /tasks/{id}/fail` | 失败（error/failure_reason） |
| `POST /tasks/{id}/session` | 中途持久化 session_id/work_dir（崩溃续接） |
| `GET  /tasks/{id}/status` | 轮询 server 侧状态（用于取消/中断） |
| `POST /runtimes/{id}/recover-orphans` | 重启后清理上个进程遗留的 dispatched/running 任务 |

**可靠性细节**
- 终态回调（complete/fail）带指数退避重试（4s→8s→16s→32s→64s），5xx/408/429 才重试，4xx 立即失败。
- runtime_gone 检测：claim/heartbeat 收到 404 "runtime not found" → 本地清理 → 幂等重注册（coalesce + failure backoff）。

**运行循环（daemon 后台 goroutine）**
- `heartbeatLoop`(15s)：更新存活。
- `pollLoop`：每 runtime 一个 poller。
- `workspaceSyncLoop`(30s)：发现新 workspace / profile 漂移重注册。
- `gcLoop`：清理旧 workdir 等。
- 本机 `/health`（19514）：进程存活自检，不对外。

### 2.4 可借鉴要点（提炼）

1. **注册即身份**：runtime 以 server 返回的 ID 为权威，本地索引只是缓存。
2. **心跳驱动健康态**：用 `status + last_seen_at` 而非"是否在跑"判定在线。
3. **拉取代推送**：任务用 poll + 单任务 slot 信号量，避免推送丢失与过度占用。
4. **生命周期闭环**：start/progress/messages/usage/complete/fail 全链路回报，支持重试与审计。
5. **任务级凭据**：`auth_token` 只对单个任务有效，不暴露 daemon 主凭据。
6. **终态幂等**：complete/fail 处理"已终态"为成功，重放安全。
7. **故障自愈**：404 runtime_gone → 清理重注册；孤儿任务 → recover-orphans。
8. **仅外连**：所有连接由 worker 端发起，穿透 NAT/防火墙，与 VPN 客户端同构。

---

## 3. devops-bot 现状与 Gap 分析

### 3.1 现状盘点

**`src/agent/` 模块（进程内模型）**
- `base.py`：`AgentBackend` 抽象基类（`name` / `binary` / `prompt_via_stdin` / `build_args` / `parse_output` / `get_env`）。
- `executor.py`：`_BACKENDS` 注册表；`register_backend()` 插件接口；`discover_backends()` 本地 CLI 探测（opencode/codearts/hermes/cursor）；`stream_agent()` 生成器（`Popen` → NDJSON 逐行解析 → yield 事件）。
- `opencode.py` / `codearts.py`：两个内置后端。

**"backend" 词汇分布（实测：src .py 474 处 + tests .py 140 处 + 前端 61 处，共约 675 处，需甄别；见 §0）**
- DB 表：`backend_agents`（name/display_name/installed/cli_path/version/healthy/**pip_package**/scope/owner_id）。
- 引用列：`devops_agent_session.backend_id`、`agent_type.default_backend_id`、`chat_agent_session.backend_id`（另有 `chat_agent_session.backend` TEXT）、`events.backend` TEXT。
- API：`GET /api/settings/backend-agents`（返回 `{"backends": [...]}`，`settings.py:178`）、`POST /api/settings/backend-agents/refresh`（`settings.py:185`）。
- 前端：`src/api/static/js/pages/backends.js`、导航 `nav('backends')`（`index.html:61` id=`nav-backends`）、页面/侧栏文案均为 **"Backend Runtimes"**。
- 会话：`backend_session_id`（`devops_agent_session` / `chat_conversations` / `vod_conversations` / `chat_agent_session` 均有）。
- 配置：`config.py:109` `get_agent_backend()`（恒返回 `"opencode"`）。

> 注意：VCS 适配层实际命名为 `VcsBackend`（`src/vcs/base.py`）/ `GitCodeBackend`（`gitcode.py`）/ `GitHubBackend`（`github.py`），**不属于** Agent backend，重命名时不得触碰。

### 3.2 Gap 对照表

| 维度 | Multica | devops-bot 现状 | 优先级 |
|---|---|---|---|
| 执行位置 | 独立 daemon 进程（worker 机） | bot 进程内 Popen | 高（解耦） |
| 注册 | server `agent_runtime` 表 + upsert | `backend_agents` 静态记录 | 高 |
| 心跳/健康态 | heartbeat + last_seen 四态 | 仅 refresh 时探测 healthy | 高 |
| 任务队列 | server 持久化 task queue | 无 | 高 |
| 生命周期回调 | start/progress/messages/usage/complete/fail | 一次性流式 yield | 中 |
| 中断/取消 | server 状态轮询 + 终止 agent | 无 | 中 |
| 并发控制 | claim 前 slot 信号量 | `max_concurrent_per_bot=10`（IM 层） | 中 |
| 任务级凭据 | 任务级 `auth_token` | 无 | 中（远端必需） |
| 认证 | 独立 daemon token（`mdt_`） | 无 | 高（远端必需） |
| 故障自愈 | runtime_gone / recover-orphans | 无 | 中 |

---

## 4. 目标架构

### 4.1 架构形态（三阶段，统一协议）

**T1（术语重构）**：仅更名，不改执行模型。
```
backend agent ──► agent runtime
src/agent/ ──► src/runtime/
backend_agents ──► agent_runtimes
```

**T2（控制面引入）**：在 bot 进程内引入 runtime 注册 / 心跳 / 任务队列 / 健康态，业务层入队，后台 worker 执行。
```
GitCode / 飞书 ──► 业务逻辑（enqueue task）
                        │
                        ▼
              ┌─────────────────────┐
              │  Runtime Service     │  ← 注册/心跳/健康态/任务队列
              │  (bot 进程内)         │
              └─────────┬───────────┘
                        │ claim + 回报
                        ▼
              ┌─────────────────────┐
              │  Runtime Worker      │  ← 复用 stream_agent（改名后）
              │  spawn opencode/…    │
              └─────────────────────┘
```

**T3（远端 RuntimeAgent，本版核心目标）**：RuntimeAgent 独立成边端进程（同仓库管理），装在 worker 机器上，以"仅外连"方式接入 runtime service。**本地部署 = RuntimeAgent 跑在 server 同一台机（`server_url=http://localhost:3000`），同一套协议**，T2 的进程内 worker 成为其特例。

```
┌── Worker Machine（可 = server 本机）────────────────┐
│  RuntimeAgent (python -m runtime.agent)            │
│  • discover_runtimes() → 本机 CLI 列表              │
│  • POST /api/runtime/register（带 rta_ token）      │
│  • 对每个注册到的 runtime_id 轮询 claim              │
│  • spawn opencode/codearts → NDJSON 流式            │
│  • report progress/usage/complete/fail              │
└──────────────────────┬──────────────────────────────┘
                       │ 外连 HTTP（无入站端口，NAT 友好）
                       ▼
┌──────────────────────────────────────────────────────┐
│ devops-bot Server (FastAPI)                          │
│  • runtime_credentials 凭据校验（rta_ token）          │
│  • agent_runtimes 注册表（status/last_seen_at）       │
│  • agent_tasks 队列（权威，确定路由）                  │
│  • 业务层 chat/event_pipeline 只负责 enqueue          │
└──────────────────────────────────────────────────────┘
```

### 4.2 关键设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | **同仓库管理**：RuntimeAgent 建在 `src/runtime/`，`python -m runtime.agent` 启动，与 bot 同仓、同依赖、同发布 | 用户决策：不拆独立包，降低运维面 |
| D2 | **独立 runtime 凭据表** `runtime_credentials`，发 `rta_` 前缀 token，独立于 user_accounts | 用户决策：凭据隔离，可吊销 |
| D3 | **确定路由**：任务经 `devops_agent_session.runtime_id` 精确绑定到 runtime，claim 按 runtime_id 过滤 | 用户决策：不跨机器抢任务，语义确定 |
| D4 | **纯 HTTP 轮询 claim**，本版不引入 WebSocket | 用户决策：先保证正确性，WS 列为后续优化 |
| D5 | **chat 流式改"入队 + 轮询任务状态"** | 用户决策：接受打字机效果改由轮询实现 |
| D6 | 任务队列落 SQLite（`agent_tasks` 表），不使用外部 MQ | 单机/多机共用同一 server DB，与现有 SQLite 一致 |
| D7 | 健康态按 runtime 设备派生（status + last_seen_at 四态） | 对齐 multica 但不过度设计 |
| D8 | 保留 `stream_agent` 的 NDJSON 流式逻辑，仅加回报包装 | 最大化复用现有成熟代码 |
| D9 | **同 session 严格串行**：同一 `agent_session_id` 同时最多 1 个活跃 task | 共享会话上下文不被并发污染（用户确认） |

---

## 5. 数据模型设计

### 5.1 新增表：`runtime_credentials`（独立凭据）

```sql
CREATE TABLE IF NOT EXISTS runtime_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL,             -- sha256(rta_xxx)，不存明文
    name         TEXT NOT NULL,             -- 管理端可读名（如 "CI-Worker-01"）
    device_name  TEXT DEFAULT '',           -- 首次注册时回填
    scope        TEXT DEFAULT 'global',     -- 与现有 scope 体系一致
    owner_id     TEXT DEFAULT '',
    status       TEXT DEFAULT 'active',     -- active / revoked
    last_seen_at TEXT DEFAULT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (token_hash)
);
```

生成流程：管理端 `POST /api/settings/runtime-credentials` → 返回 `rta_` token（**仅展示一次明文**）→ 存入 `token_hash`（sha256）。RuntimeAgent 以 `RUNTIME_TOKEN` 环境变量 / 配置文件持有明文。吊销 = 置 `status='revoked'`。

### 5.2 新增表：`agent_runtimes`

对应 multica `agent_runtime`（RuntimeDevice），替代/升级 `backend_agents`。增加 `credential_id` 绑定凭据，使"凭据 → 设备"可追溯。

```sql
CREATE TABLE IF NOT EXISTS agent_runtimes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER,                  -- 引用 runtime_credentials.id
    name          TEXT NOT NULL,            -- opencode / codearts / hermes / cursor / custom
    display_name  TEXT DEFAULT '',
    provider      TEXT DEFAULT '',          -- 引擎家族（对应 multica protocol_family）
    binary        TEXT DEFAULT '',          -- CLI 可执行名（shutil.which 目标）
    cli_path      TEXT DEFAULT '',
    version       TEXT DEFAULT '',
    healthy       INTEGER DEFAULT 0,
    installed     INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'offline',   -- online / offline / recently_lost / about_to_gc
    last_seen_at  TEXT DEFAULT NULL,        -- 心跳时间
    scope         TEXT DEFAULT 'global',
    owner_id      TEXT DEFAULT '',
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (name, scope, owner_id)
);
```

迁移：`backend_agents ──► agent_runtimes`（数据搬迁 + 引用列改名，见 §6.3）。

### 5.3 新增表：`agent_tasks`

对应 multica `agent_task_queue`，任务生命周期落库。`runtime_id` 为**确定路由**的目标设备（必填）。

```sql
CREATE TABLE IF NOT EXISTS agent_tasks (
    id                TEXT PRIMARY KEY,       -- 短 ID / uuid
    -- 与 devops_agent_session 的关联：显式外键 agent_session_id → devops_agent_session.id
    agent_session_id  TEXT NOT NULL,          -- 所属会话：引用 devops_agent_session.id（唯一关联字段）
    runtime_id        INTEGER NOT NULL,       -- 确定路由：引用 agent_runtimes.id（从 devops_agent_session.runtime_id 拷贝）
    repo_key          TEXT DEFAULT '',        -- 冗余快照（便于按仓库检索，非关联字段）
    agent_type        TEXT DEFAULT '',        -- 冗余快照：Agent 类型（issue/pr/vod/chat）
    skill             TEXT DEFAULT '',
    prompt            TEXT NOT NULL,
    cwd               TEXT DEFAULT '',
    runtime_session_id TEXT DEFAULT '',       -- 会话级共享续接指针：从 devops_agent_session.runtime_session_id 拷贝，会话期稳定，单次执行不改变
    event_type        TEXT DEFAULT '',        -- 触发事件类型（issue_opened/vod_signal/a2a_event），注入为 DEVOPS_EVENT_TYPE
    account           TEXT DEFAULT '',
    user_id           INTEGER DEFAULT 0,
    repo_gitcode_token TEXT DEFAULT '',
    env_extra         TEXT DEFAULT '{}',      -- JSON（含服务端已注入的账号/用户凭据，见 skill-delivery-remote.md）
    skill_files       TEXT DEFAULT '{}',      -- ★ JSON: {skill_name: {rel_path: content}}，远端交付技能包
    server_url        TEXT DEFAULT '',        -- ★ A2A 外呼地址，worker 替换 {{SERVER_URL}}
    status            TEXT DEFAULT 'queued',  -- queued/dispatched/running/completed/failed/cancelled
    exit_code         INTEGER DEFAULT NULL,
    result_text       TEXT DEFAULT '',
    errors            TEXT DEFAULT '',        -- JSON array
    stderr            TEXT DEFAULT '',
    usage_json        TEXT DEFAULT '{}',
    started_at        TEXT DEFAULT NULL,
    completed_at      TEXT DEFAULT NULL,
    created_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_runtime ON agent_tasks(runtime_id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_session ON agent_tasks(agent_session_id);
```

**与 `devops_agent_session` 的关联**：通过 `agent_tasks.agent_session_id → devops_agent_session.id` **唯一关联字段**（显式外键）。`repo_key` / `agent_type` 只是冗余快照，用于展示与检索，不承担关联职责。

```
devops_agent_session（绑定/配置，1 行 / repo+agent_type，权威）
   id               ←─ agent_tasks.agent_session_id（外键）
   runtime_id       ←─ agent_tasks.runtime_id（路由拷贝，非关联）
   runtime_session_id ══► agent_tasks.runtime_session_id（会话期稳定拷贝，无回写）
```

> 同 `agent_session_id`（即同 repo + 同 agent_type）下的所有 task **共享同一个 `runtime_session_id`**，单次任务执行不改变该值；仅在会话重建（如 `/clean` 清会话缓存）时由 `devops_agent_session` 更新，后续 task 取新值。

### 5.4 现有表字段更名（§6.3）

| 表 | 原字段 | 新字段 |
|---|---|---|
| `devops_agent_session` | `backend_id` | `runtime_id` |
| `devops_agent_session` | `backend_session_id` | `runtime_session_id` |
| `agent_type` | `default_backend_id` | `default_runtime_id` |
| `chat_agent_session` | `backend_id` | `runtime_id` |
| `chat_agent_session` | `backend`（TEXT，默认 `'opencode'`） | `runtime`（TEXT，默认 `'opencode'`） |
| `chat_conversations` | `backend_session_id` | `runtime_session_id` |
| `vod_conversations` | `backend_session_id` | `runtime_session_id` |
| `events` | `backend`（TEXT，`db_setup.py:20`） | `runtime`（TEXT） |

> 注：`chat_agent_session` 同时存在 `backend` TEXT 与 `backend_id` INTEGER（迁移补充），两者都需更名；`events` 表的 `backend` 列原稿未列，一并纳入。

---

## 6. 术语重构方案（T1：backend agent → agent runtime）

### 6.1 重命名映射表

**Python 代码（`src/` 与 `tests/`）**

| 旧 | 新 | 涉及文件 |
|---|---|---|
| `AgentBackend` | `AgentRuntime` | `src/agent/base.py` |
| `register_backend()` | `register_runtime()` | `src/agent/executor.py` |
| `get_backend()` | `get_runtime()` | `src/agent/executor.py` |
| `discover_backends()` | `discover_runtimes()` | `src/agent/executor.py` |
| `stream_agent()` | `stream_runtime()`（或保留别名） | 各调用方 |
| `_BACKENDS` | `_RUNTIMES` | `src/agent/executor.py` |
| `backend_name` 参数 | `runtime_name` | `stream_agent` 签名 |
| `backend_agents` 表 | `agent_runtimes` 表 | `db_setup.py` / `user/db.py` |
| `get_backend_agents()` | `get_agent_runtimes()` | `user/db.py` |
| `save_backend_agents()` | `save_agent_runtimes()` | `user/db.py` |
| `backend_id` | `runtime_id` | 各查询/SQL |
| `backend_session_id` | `runtime_session_id` | 各查询/SQL |
| `default_backend` / `default_backend_id` | `default_runtime` / `default_runtime_id` | `agents.py` / `session_manager.py` |
| `get_agent_backend()`（`config.py:109`） | `get_agent_runtime()` | `config.py` |

**API 路由**

| 旧 | 新 |
|---|---|
| `GET /api/settings/backend-agents`（返回 `{"backends": [...]}`） | `GET /api/settings/agent-runtimes` |
| `POST /api/settings/backend-agents/refresh` | `POST /api/settings/agent-runtimes/refresh` |
| `GET /api/agents/...` 返回体 `backend` 字段 | `runtime` 字段 |

**前端 static（实际目录 `src/api/static/`）**

| 旧 | 新 |
|---|---|
| `src/api/static/js/pages/backends.js` | `src/api/static/js/pages/runtimes.js` |
| 导航 `#nav-backends` / `nav('backends')`（`index.html:61` / `app.js:3`） | `#nav-runtimes` / `nav('runtimes')` |
| 现有文案 "Backend Runtimes"（`index.html:61` / `app.js:234`） | "Agent Runtimes" |
| API client 中的 `backend-agents` 路径 | `agent-runtimes` |

**测试**
- `tests/test_agent_executor.py`、`tests/test_api_routes.py`（`test_backend_agents` 断言 `{"backends": [...]}`）、`tests/test_session_manager.py`、`tests/test_phase9.py`、`tests/ui_test_playwright.py`（`test_backend_page`）、`tests/ui_scenario_test.py`（`test_s10_backends`）同步更名。
- `tests/test_github_normalize.py` / `tests/ui_*` 中仅涉及 VCS/GitHub backend 的部分**不改**。

### 6.2 兼容策略

1. **API 层**：旧路径 `/api/settings/backend-agents` 保留一个 301 重定向或同实现别名（低风险，前端逐步切换）。
2. **代码层**：`stream_agent` 保留为兼容包装，内部调用 `stream_runtime`，调用方渐进迁移。
3. **DB 层**：一次迁移（表重建 + 列改名），不做双写；迁移脚本幂等，见 §6.3。
4. **`backend` 作为 VCS 层术语**（`VcsBackend`/`GitCodeBackend`/`GitHubBackend`）：**不迁移**，明确排除在范围外。

### 6.3 DB 迁移脚本（幂等）

在 `db_setup.py` 的迁移区新增：

```python
# 1) backend_agents → agent_runtimes（重建表，搬迁数据）
if "backend_agents" in tables and "agent_runtimes" not in tables:
    c.execute("ALTER TABLE backend_agents RENAME TO agent_runtimes")
    _ensure_column(c, "agent_runtimes", "credential_id", "INTEGER")
    _ensure_column(c, "agent_runtimes", "provider", "TEXT DEFAULT ''")
    _ensure_column(c, "agent_runtimes", "binary", "TEXT DEFAULT ''")
    _ensure_column(c, "agent_runtimes", "status", "TEXT DEFAULT 'offline'")
    _ensure_column(c, "agent_runtimes", "last_seen_at", "TEXT DEFAULT NULL")
elif "backend_agents" in tables and "agent_runtimes" in tables:
    c.execute("DROP TABLE backend_agents")

# 2) 引用列改名（SQLite 3.25+ 支持 RENAME COLUMN）
for tbl, cols in {
    "devops_agent_session": [("backend_id", "runtime_id"), ("backend_session_id", "runtime_session_id")],
    "agent_type": [("default_backend_id", "default_runtime_id")],
    "chat_agent_session": [("backend_id", "runtime_id"), ("backend", "runtime")],  # backend TEXT 一并更名
    "chat_conversations": [("backend_session_id", "runtime_session_id")],
    "vod_conversations": [("backend_session_id", "runtime_session_id")],
    "events": [("backend", "runtime")],  # events.backend TEXT
}.items():
    for old, new in cols:
        _rename_column_if_exists(c, tbl, old, new)
```

> SQLite 版本说明：Python 3.14.3 内置 SQLite 支持 `ALTER TABLE ... RENAME COLUMN`（3.25+），直接可用；否则回退为"建新表-拷贝-改名"三步。

---

## 7. Runtime 协议与实现设计（服务端 + RuntimeAgent）

### 7.1 模块结构

```
src/runtime/                  # 由 src/agent/ 演进而来，同仓库管理
├── __init__.py
├── base.py                   # AgentRuntime 抽象基类（原 AgentBackend）
├── registry.py               # 原 executor.py 的注册表部分
│     register_runtime() / get_runtime() / discover_runtimes()
├── executor.py               # 保留 stream_runtime()（原 stream_agent）
├── opencode.py               # OpencodeRuntime
├── codearts.py               # CodeArtsRuntime
├── service.py                # ★ 新增：服务端 RuntimeService（FastAPI 依赖）
│     register/heartbeat/claim/report + health
├── agent.py                  # ★ 新增：RuntimeAgent 边端入口（python -m runtime.agent）
│     注册 → 轮询 claim → 执行 → 回报 循环
├── client.py                 # ★ 新增：RuntimeAgent 的 HTTP 客户端（外连）
│     封装 register/heartbeat/claim/report 请求 + 指数退避重试
└── health.py                 # ★ 新增：健康态派生（移植 multica derive-health）
      online / recently_lost / offline / about_to_gc
```

### 7.2 服务端 HTTP API（RuntimeService 对外暴露）

| 端点 | 方向 | 用途 |
|---|---|---|
| `POST /api/runtime/register` | RuntimeAgent → server | 凭据校验（`rta_`）+ upsert agent_runtimes，返回 runtime IDs |
| `POST /api/runtime/heartbeat` | RuntimeAgent → server | 更新 last_seen_at / status |
| `POST /api/runtime/runtimes/{runtime_id}/tasks/claim` | RuntimeAgent → server | 按 runtime_id 拉取队首任务或 null |
| `POST /api/runtime/tasks/{task_id}/start` | RuntimeAgent → server | 标记 running |
| `POST /api/runtime/tasks/{task_id}/progress` | RuntimeAgent → server | 阶段进度 |
| `POST /api/runtime/tasks/{task_id}/messages` | RuntimeAgent → server | 批量 NDJSON 执行消息 |
| `POST /api/runtime/tasks/{task_id}/usage` | RuntimeAgent → server | token 用量 |
| `POST /api/runtime/tasks/{task_id}/complete` | RuntimeAgent → server | 完成（幂等） |
| `POST /api/runtime/tasks/{task_id}/fail` | RuntimeAgent → server | 失败（幂等） |
| `GET  /api/runtime/tasks/{task_id}/status` | 前端 / RuntimeAgent | 任务状态轮询 |
| `POST /api/runtime/runtimes/{runtime_id}/recover-orphans` | RuntimeAgent → server | 启动时清理遗留任务 |
| `GET  /api/settings/runtime-credentials` | 前端 → server | 管理凭据（生成/吊销/列表） |

**认证中间件**：解析 `Authorization: Bearer rta_...` → sha256 → 查 `runtime_credentials`（status=active）→ 上下文注入 `credential_id`。所有 `/api/runtime/*` 端点只允许凭据通过，不开放给 user token（凭据隔离，D2）。

### 7.3 确定路由机制（D3）

绑定链路（复用现有数据，只改字段名）：

```
( repo_key, agent_type )
        │  管理端在 Agent 配置页选择 runtime（绑定设备）
        ▼
devops_agent_session.runtime_id ──► agent_runtimes.id（含 device_name + CLI type）
        │  事件 / chat 触发
        ▼
enqueue_task()：INSERT agent_tasks
   ├─ agent_session_id = devops_agent_session.id（外键关联）
   ├─ runtime_id       = devops_agent_session.runtime_id（确定路由）
   ├─ repo_key/agent_type = 冗余快照
   └─ runtime_session_id = devops_agent_session.runtime_session_id（稳定拷贝，共享）
        │
        ▼
agent_tasks(status=queued)
        │  只有该 runtime 的 RuntimeAgent 能 claim 到
        ▼
RuntimeAgent claim：UPDATE agent_tasks
      SET status='running' WHERE id=? AND runtime_id=? AND status='queued'
        │  执行：使用 task 自带的 runtime_session_id（稳定拷贝，不改会话）
        ▼
完成 / 失败（终态）
```

> **串行约束（D9）**：同一 `agent_session_id` 下 task **严格串行**——同一时刻最多 1 个活跃 task 写该会话，保护共享会话上下文不被并发污染。下一 task 在前一 task 进入终态后才被 claim。

- 入队时任务**必须**带 `agent_session_id`（关联 `devops_agent_session.id`）与 `runtime_id`（路由目标，来自 `devops_agent_session.runtime_id` / `chat_agent_session.runtime_id`）。
- claim 端点按 runtime_id 过滤，语义与 multica `ClaimTaskByRuntime` 一致。
- **绑定 runtime 离线**：任务保持 `queued`，health 页可见"等待 runtime 上线"；不随机派给其他机器（确定语义）。
- 管理端前端：Runtime 页面显示每台设备（device_name + 在线态 + 承载 CLI），Agent 配置页从在线 runtime 下拉选择。

### 7.4 RuntimeAgent 边端循环（D4：纯 HTTP 轮询）

```python
# src/runtime/agent.py
def main():
    token = os.environ["RUNTIME_TOKEN"]      # rta_...
    server = os.environ.get("RUNTIME_SERVER_URL", "http://localhost:3000")
    poll_interval = float(os.environ.get("RUNTIME_POLL_INTERVAL", "3"))
    heartbeat_interval = float(os.environ.get("RUNTIME_HEARTBEAT_INTERVAL", "15"))
    max_concurrent = int(os.environ.get("RUNTIME_MAX_CONCURRENT_TASKS", "20"))

    client = RuntimeClient(server, token)
    # 1) 发现本机 CLI → 注册为 runtimes（复用 discover_runtimes）
    runtimes = client.register(discover_runtimes())
    # 2) 启动时清理孤儿任务
    for rid in runtimes:
        client.recover_orphans(rid)

    loop(runtimes, client, poll_interval, heartbeat_interval, max_concurrent)

def loop(runtimes, client, poll_interval, heartbeat_interval, max_concurrent):
    while True:
        for rid in runtimes:
            if active_tasks(rid) >= max_concurrent:
                continue                       # runtime 全局执行槽占满则不 claim
            task = client.claim(rid)           # HTTP POST，带 rta_ token
            if task:
                if active_tasks_for_session(task["agent_session_id"]) >= 1:
                    # 同 session 严格串行：该会话已有活跃 task，跳过（D9）
                    continue
                spawn_executor_thread(task)    # stream_runtime + report_*
        if time since last heartbeat >= heartbeat_interval:
            client.heartbeat(runtimes)         # 更新 last_seen_at
        time.sleep(poll_interval + jitter)     # 带 jitter 避免惊群
```

> 执行线程内复用 `stream_runtime()`（原 `stream_agent`）的 NDJSON 流式逻辑，事件映射到 `report_progress / report_messages / report_usage / complete_task / fail_task`；终态回调带指数退避重试（对照 multica 4s→64s）。串行槽判断应在 claim 之后、spawn 之前（claim 已把任务置为 running，若同 session 已被占用则将该任务置回 queued 或等待下一轮释放后再 claim，实现上二选一，见 M2）。

### 7.5 健康态派生（移植 multica `derive-health.ts`）

```python
def derive_runtime_health(status: str, last_seen_at: str | None, now: float) -> str:
    if status == "online":
        return "online"
    if not last_seen_at:
        return "offline"
    offline_for = now - parse(last_seen_at)
    if offline_for < 5 * 60:
        return "recently_lost"          # 刚失联
    if offline_for > 6 * 24 * 3600:     # 距 GC（7d）不足 1d
        return "about_to_gc"
    return "offline"
```

### 7.6 与现有调用方对接（T2→T3 迁移）

| 调用方 | 现行为（已核实） | 迁移后 |
|---|---|---|
| `src/api/routes/chat.py` (send/stream) | `chat.py:125/133/217` 直接 `stream_agent`；`POST /send` 与 SSE `POST /send/stream`（`chat.py:182`） | `service.enqueue_task()` → 返回 task_id；前端轮询 `GET /api/runtime/tasks/{id}/status`（打字机效果） |
| `src/api/event_pipeline.py` | `run_pipeline`→`_execute_agent`（`event_pipeline.py:323`）后台线程直接 `stream_agent` | `service.enqueue_task()`，由绑定 runtime 的 RuntimeAgent 执行 |
| `src/im/handler_async.py` / `vod_handler_async.py` | 直接 `stream_agent` | 同 chat（入队 + 状态轮询） |
| `src/api/routes/settings.py` | `discover_backends`（`settings.py:188`） | `discover_runtimes` |
| `src/api/routes/webhook.py`（含 `POST /api/a2a`，`webhook.py:68`）/ `scenes.py` | 读写 backend 字段 | 读写 runtime 字段 |

---

## 8. 实施计划与里程碑

### M1：T1 术语重构（约 1-2 天，可独立回滚）
- [ ] 新建 `src/runtime/`，`src/agent/` 改名；保留兼容别名 `stream_agent`。
- [ ] DB 迁移（`backend_agents`→`agent_runtimes`，列改名），`db_setup.py` 幂等脚本。
- [ ] API 路由更名 + 旧路径兼容。
- [ ] 前端 `backends.js`→`runtimes.js`，导航与文案。
- [ ] 测试更名（排除 VCS backend），`python -m pytest tests/ -v` 全绿。

### M2：T2 控制面（约 3-5 天）
- [ ] `agent_tasks` 表 + `RuntimeService` + `client.py`。
- [ ] 健康态派生 `health.py` + heartbeat。
- [ ] 非流式调用方（event_pipeline / webhook / scenes）切到入队执行。
- [ ] chat 改"入队 + 轮询任务状态"（D5）。
- [ ] 取消/中断：`cancel_task` + 执行侧 kill 子进程。
- [ ] 串行槽逻辑（D9）：runtime 全局并发上限 + 每 `agent_session_id` 严格串行（同 session 1 个活跃 task；claim 后检测到同 session 占用则置回 queued 或等待释放再 claim）。

### M3：远端 RuntimeAgent（约 3-5 天，本版核心）
- [ ] `runtime_credentials` 表 + 管理端生成/吊销 `rta_` token 页面。
- [ ] 服务端 `/api/runtime/*` 端点 + `rta_` 认证中间件。
- [ ] `src/runtime/agent.py` 边端入口：register → claim → execute → report 循环。
- [ ] 确定路由：入队必带 `runtime_id`，claim 按 runtime_id 过滤（D3）。
- [ ] 本地 + 远端两种部署验证（localhost / 公网 wss）。

### M4：前端与报表（约 2-3 天）
- [ ] Runtime 管理页：设备列表（device_name + 健康四态 + 承载 CLI）+ 凭据管理。
- [ ] Agent 配置页绑定 runtime 下拉。
- [ ] chat 任务状态轮询 / 后续升级 SSE。
- [ ] usage/activity 报表页。

### M5：后续演进（不在本期）
- [ ] WebSocket 加速唤醒（task_available 帧，对照 multica daemonws hub）。
- [ ] 任务级 `auth_token`（对照 multica MULTICA_TOKEN）。
- [ ] 自定义 RuntimeProfile（command_name + fixed_args）。
- [ ] runtime 自动更新 / GC。

---

## 9. 后续演进与未决问题

### 9.1 已确认决策（用户拍板）
1. **部署方式**：同仓库管理（`src/runtime/`，`python -m runtime.agent`）。
2. **凭据**：独立 `runtime_credentials` 表，`rta_` token，明文仅展示一次（可接受）。
3. **路由**：确定路由，runtime worker 经 runtime service 与 `devops_agent_session` 绑定。
4. **唤醒**：纯 HTTP 轮询 claim（本版无 WebSocket）。
5. **chat 流式**：接受改为"入队 + 轮询任务状态"。
6. **会话共享**：同 repo + 同 agent_type 下 task 共享同一 `runtime_session_id`；单次任务执行不改变该值，仅在会话重建（如 `/clean`）时更新。
7. **同 session 严格串行**：同一 `agent_session_id` 同时最多 1 个活跃 task（D9）。

### 9.2 已明确排除
- VCS 层 `VcsBackend` / `GitCodeBackend` / `GitHubBackend` 等命名保持不动。
- `configs/agent-routing.json` 已废弃（README 提及但文件不存在），不纳入。
- 不引入外部 MQ / Redis。
- 本版不引入 WebSocket / 任务级 token（列为 M5）。

### 9.3 遗留待定（实现期细化）
1. `backend_session_id` 改名 `runtime_session_id` 涉及会话续接，测试面较大——确认随 M1 一并改（文档默认同步改）。
2. 旧 API 路径兼容期多长？（建议 ≥ 2 个发布周期。）
3. RuntimeAgent 的并发上限默认值（建议 `RUNTIME_MAX_CONCURRENT_TASKS=20`，对齐 multica）。
4. 串行槽实现二选一：claim 后检测到同 session 占用 →（a）任务置回 `queued` 待下轮重新 claim；（b）不 claim 该任务（claim 查询时排除有活跃 task 的 session）。实现期选优。

### 9.4 参考资料
- Multica daemon：`D:\Developer\multica-bot\multica\server\internal\daemon\`（daemon.go / client.go / config.go / identity.go / wakeup.go / types.go）
- Multica 服务端接入：`server\internal\handler\daemon.go` / `daemon_ws.go` / `runtime.go`
- Multica WebSocket hub：`server\internal\daemonws\hub.go`
- Multica 认证：`server\internal\middleware\daemon_auth.go`
- Multica 前端 runtime 模块：`packages\core\runtimes\`（derive-health.ts / queries.ts / cloud-runtime.ts）
- Multica 文档：`docs\custom-runtimes.md`、`CLI_AND_DAEMON.md`
