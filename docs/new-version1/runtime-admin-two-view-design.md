# Agent Runtime 双视图原型设计（Engines / Workers 两个子页面）

> 版本：v0.10（原型设计稿，未改代码）
> 日期：2026-08-09（v0.10 同日按评审刷新：仓库级 worker/引擎对**仓库所有成员**可见（不只 owner）——§1 概念、§4.1 仓库表多成员示例、§5.1 可见性、§7 API 可见性过滤、§10 S8 成员可见性测试）
> 背景：Runtime Agent 页当前单页混排"设备"与"引擎"两个概念；而 vod/issue 等 Agent 配置页的 **Backend Engine 下拉选的是引擎（opencode/codearts）**，不是 worker。多 worker 并存后（worker-123249、grape-server-local），需要把"谁在执行"（worker）与"用什么引擎干活的 agent"（engine 实例）两个维度在 UI 上分开。

---

## 1. 概念澄清（设计前提）

| 维度 | 实体 | 对应数据 | 使用者 |
|------|------|----------|--------|
| **Engine 实例（引擎）** | 某 worker 上安装的一个 CLI（opencode / codearts / cursor / codex …） | `agent_runtimes` 行（实例列表 `runtimes`） | **Agent 配置绑定**：vod/issue 页 "Backend Engine" 选的就是它 |
| **Worker（设备）** | 一台执行主机 / 一个边端进程 / 一张凭据 | `runtime_credentials` + 其 `agent_runtimes` 行（设备视图 `devices`） | 运维：上线/下线、看心跳/IP/scope、看设备任务、吊销 |

- 一个 **worker 可承载多个引擎**（grape-server 上有 opencode + codearts）；一个**引擎可存在于多台 worker**（opencode 在 grape-server 与 worker-123249 上各一份）。
- 多实例键上线后，`agent_runtimes` 是"引擎实例"粒度：`(credential_id, name)` 唯一 → 引擎名可对应多行（每 worker 一行）。
- **Engine 实例名（唯一）**：为每个引擎实例分配全局唯一的可读名，作为前端展示与 Agent 配置下拉的选择标识（见 §3.2）。规则：`{engine_type}-{worker 短名}` → `opencode-local`、`opencode-worker-23249`；`engine_type` 与实例名解耦（实例名唯一，type 可重复）。
- **Worker 归属（owner）**：每个 worker 隶属一个 owner（`user_name`）= **生成凭据的当前登录用户**。**暂不对全局做 owner 限制**（任何用户的凭据都可设为全局）；**默认新建 worker 均为个人**。来源：凭据的 owner 字段。
- **Worker scope（全局/仓库/个人）**：沿用 skills 库页面的 scope 语义（全局=global、仓库=repo、个人=user，见 `skills.js` `SCOPE_NAMES`）；**默认 worker scope = 个人**，用户可为自己的凭据设置 全局/仓库/个人。
- **仓库级 worker 共享**：仓库 scope 的 worker/引擎对该仓库**所有成员**可见、可绑定（**不限于仓库 owner 名下**）；个人 scope 的 worker/引擎仅供其 owner 可见/绑定；全局 scope 对所有用户可见。
- **Engine scope/owner**：引擎实例的 scope 与 owner **与其承载 worker 保持一致**（引擎不独立归属，随 worker 走）。
- **凭据关联用户**：生成凭据时 **owner 自动为当前登录用户（不可选）** + 手动选 scope（个人/仓库/全局）；worker 注册时携带该凭据，服务端即可识别其归属用户与范围（见 §4.2 生成凭据）。

---

## 2. 页面结构原型

`Agent Runtime` 导航下拆 **两个子页**（Tab 切换，**默认 Engines**）：

```
Agent Runtime
├─ [Engines 引擎]  ← 默认
└─ [Workers 设备]
```

导航实现：**对齐 skills 库页面的 Tab 实现**（`src/api/static/js/pages/skills.js`，`_skillTab` 状态 + 顶部按钮 `onclick="_skillTab='...';renderSkills()"` 切换并整体重渲染）：
- `runtimes.js` 加模块级 `_runtimeTab`（`'engines'`/`'workers'`，默认 `'engines'`），顶部两个 Tab 按钮切换并调用对应渲染入口（`renderEngines` / `renderWorkers`）。
- scope 三表分组（全局/仓库/个人）同 skills 页 scope 分组风格（`SCOPE_NAMES = {global:'全局', repo:'仓库', user:'个人'}`）。
- 不拆 `app.js` VIEWS，沿用单视图内 Tab 切换（最小改动、与 skills 页一致）。

---

## 3. 子页一：Engines（引擎 / Agent）— 默认

> 数据源：`GET /api/settings/agent-runtimes` 的 `runtimes` 数组（引擎实例列表，每行 = 某 worker 上的某引擎），每行补 `instance_name`（唯一引擎名，见 §3.2）与 `worker_name`/`worker_ip`（经 `credential_id` 关联）。

### 3.1 表格展示（按 scope 分三张表：全局 / 仓库 / 个人）

> 参照 skills 库页面的 scope 分组；**全局表在最上、仓库表居中、个人表在最下**。

```
[⟳ Refresh]

【全局】
┌── engine name ────────────────┬─ engine type ──┬─ version ──┬─ worker name ───────┬─ scope ──┬─ owner ──────┬─ status ─┐
│ opencode-local                │ opencode       │ 1.17.7     │ grape-server-local  │ 全局     │ Jason7602    │ ● 在线   │
│ codearts-local                │ codearts       │ 26.6.2     │ grape-server-local  │ 全局     │ Jason7602    │ ● 在线   │
└───────────────────────────────┴────────────────┴────────────┴────────────────────┴──────────┴──────────────┴─────────┘

【仓库】
┌── engine name ────────────────┬─ engine type ──┬─ version ──┬─ worker name ───────┬─ scope ──┬─ owner ──────┬─ status ─┐
│ opencode-repo-abc             │ opencode       │ 1.18.0     │ repo-worker-a       │ 仓库     │ RepoOwner    │ ● 在线   │
└───────────────────────────────┴────────────────┴────────────┴────────────────────┴──────────┴──────────────┴─────────┘

【个人】
┌── engine name ────────────────┬─ engine type ──┬─ version ──┬─ worker name ───────┬─ scope ──┬─ owner ──────┬─ status ─┐
│ opencode-worker-23249         │ opencode       │ 1.18.15    │ worker-123249       │ 个人     │ Jason7602    │ ● 在线   │
│ codex-worker-23249            │ codex          │ 0.1.0      │ worker-123249       │ 个人     │ Jason7602    │ ○ 离线   │
└───────────────────────────────┴────────────────┴────────────┴────────────────────┴──────────┴──────────────┴─────────┘
```

**分组规则**：以实例的 `scope` 分组（全局/仓库/个人），组标题即 scope；表内 `scope` 列保留（便于单独筛选/对照）。空分组不渲染。

**列**：
| 列 | 说明 |
|----|------|
| **engine name** | 引擎实例唯一名（选择标识，如 `opencode-local`、`opencode-worker-23249`） |
| **engine type** | 引擎类型（opencode / codearts / cursor / codex …），可重复 |
| **version** | 该实例 CLI 版本 |
| **worker name** | 承载该实例的 worker，**= 凭据名称（全局唯一）**，与 Workers 表一致（`device_name` 作辅助） |
| **scope** | 该实例所属范围（全局 / 仓库 / 个人），**与其 worker 一致**（引擎不独立归属） |
| **owner** | 该实例归属用户（`user_name`），**与其 worker 一致**（= 生成凭据的用户；全局暂不设 owner 限制） |
| **status** | 在线 / 刚失联 / 离线 / 待回收 |

### 3.2 唯一引擎名（instance_name）规则

- 由 `agent_runtimes` 行派生，全局唯一、可读、稳定，供展示与下拉选择：
  ```
  instance_name = "{name}-{device_suffix}"
  device_suffix = worker 短名（device_name 去非法字符并截断），无 device_name 时回退 "{name}-cred{credential_id}"
  ```
  例：device_name=`grape-server` → `opencode-grape-server`；device_name=`worker-123249` → `opencode-worker-123249`。
- 归属：**服务端派生**（`GET /api/settings/agent-runtimes` 响应中为每行计算，或 register 时写入 `agent_runtimes.display_name`）；前端只读展示，不自行拼接。
- `engine name` 与 `engine type` 解耦：同一 type 可有多个唯一 name（多实例并存）。

**语义**：本页回答"**每个引擎实例（opencode/codearts/…）叫什么、什么类型、什么版本、在哪台 worker、是否在线**"——行即 Agent 配置下拉的一个可选项。

---

## 4. 子页二：Workers（设备）

> 数据源：`GET /api/settings/agent-runtimes` 的 `devices` 数组。本页为**表格展示**（对齐 Engines 页），并把原"凭据管理"区**合入表格**的"凭据"列。

### 4.1 表格展示（按 scope 分三张表：全局 / 仓库 / 个人）

> 与 Engines 页一致：**全局表在最上、仓库表居中、个人表在最下**。

```
[⟳ Refresh] [＋ 生成凭据]

【全局】
┌── worker name ──────────┬─ ip address ──────┬─ scope ──┬─ owner ──────┬─ 心跳 ─────────┬─ 凭据 ──────────────┬─ 承载引擎 ─┐
│ grape-server-local      │ 127.0.0.1         │ 全局     │ Jason7602    │ ● 在线 · 12s 前 │ active · [吊销]      │ opencode v1.17.7 ✓ · codearts v26.6.2 ✓ │
└─────────────────────────┴───────────────────┴──────────┴──────────────┴─────────────────┴─────────────────────┴──────────────────────────┘

【仓库】
┌── worker name ──────────┬─ ip address ──────┬─ scope ──┬─ owner ──────┬─ 心跳 ─────────┬─ 凭据 ──────────────┬─ 承载引擎 ─┐
│ repo-worker-a           │ 10.0.0.5          │ 仓库     │ RepoOwner    │ ● 在线 · 20s 前 │ active · [吊销]      │ opencode v1.18.0 ✓ │
│ repo-worker-b           │ 10.0.0.6          │ 仓库     │ MemberX      │ ● 在线 · 15s 前 │ active · [吊销]      │ opencode v1.17.9 ✓ │
└─────────────────────────┴───────────────────┴──────────┴──────────────┴─────────────────┴─────────────────────┴──────────────────────────┘

【个人】
┌── worker name ──────────┬─ ip address ──────┬─ scope ──┬─ owner ──────┬─ 心跳 ─────────┬─ 凭据 ──────────────┬─ 承载引擎 ─┐
│ worker-123249           │ 123.249.12.67     │ 个人     │ Jason7602    │ ● 在线 · 8s 前  │ active · [吊销]      │ opencode v1.18.15 ✓ │
└─────────────────────────┴───────────────────┴──────────┴──────────────┴─────────────────┴─────────────────────┴──────────────────────────┘
```

**分组规则**：以 worker 的 `scope` 分组（全局/仓库/个人），组标题即 scope；表内 `scope` 列保留。空分组不渲染。

**仓库表可见范围**：展示**该仓库所有成员**名下的仓库级 worker（不限于仓库 owner，如 `repo-worker-b` 为成员 MemberX 所建）；当前用户仅见「全局 + 自己的个人 + 我所在仓库的仓库级」。

**列**：
| 列 | 说明 |
|----|------|
| **worker name** | **= 凭据名称（`runtime_credentials.name`，全局唯一）**；与凭据名保持一致。边端自报的 `device_name`（如 `grape-server`）作为辅助信息显示（如 "grape-server（device_name）"），不作为主名 |
| **ip address** | `last_ip`（M3 已回填） |
| **scope** | worker 归属范围：**全局 / 仓库 / 个人**（默认个人；用户可为自己的凭据设为任意范围，暂不限全局） |
| **owner** | worker 归属用户（`user_name`）= **生成凭据的当前登录用户**（暂不对全局设 owner 限制） |
| **心跳** | 健康态（在线/刚失联/离线/待回收）+ 心跳相对时间（`last_seen_at`） |
| **凭据** | 凭据状态（`active`/`revoked`）+ `[吊销]` 按钮（合入原"凭据管理"区） |
| 承载引擎 | 该 worker 上安装的引擎（`name v版本 ✓/✗`），可选列 |

- **凭据信息合入**：原独立"凭据管理"列表（凭据名/状态/心跳/吊销）并入本表格"凭据"列；生成凭据入口保留在表格上方 `[＋ 生成凭据]`。
- 吊销后：凭据 `status='revoked'`，其引擎行级联 `offline`，该 worker 从在线分组消失（保留行，标 revoked）。

### 4.2 生成凭据（关联用户）

```
[＋ 生成凭据] 弹出表单：
  凭据名称:  [CI-Worker-01       ]   ← 可修改，但全局唯一（重名拒绝）
  归属用户:  Jason7602（当前登录用户，自动，不可选）
  范围:      [个人 / 仓库 / 全局   ]   ← 默认个人；用户可为自己的凭据设任意范围
  [生成]  → 展示 rta_ token（仅一次）+ 启动命令
```

- 生成的凭据写入 `runtime_credentials`：`name`（凭据名，**全局唯一**）、`scope`（个人=user、仓库=repo、全局=global）+ `owner`（= 当前登录用户，服务端自动注入，前端不可选）。
- **凭据名称可修改，但必须全局唯一**（与其他凭据名不重复；重名则拒绝）。凭据名即 worker 名（见 §4.1），改名后 Workers/Engines 两表同步显示新名，`instance_name` 随之更新。
- **暂不对全局设 owner 限制**：任何用户的凭据都可设为全局（`grape_bot` 限制暂不做）。
- **worker 注册时携带该凭据 → 服务端凭据校验（`rta_` → `runtime_credentials`）即知归属用户与范围**，引擎行的 scope/owner 随之确定（随 worker 一致）。

### 4.3 查看任务

- 展示该设备（凭据）名下所有引擎实例已入队的 `agent_tasks`（队列/进行中/完成/失败，含仓库/Agent/状态/Exit/结果）。
- ⚠️ 现状接口 `GET /api/runtime/runtimes/{runtime_id}/tasks` 是**按 runtime_id** 查；设备可能承载多个引擎（多个 runtime_id），且前端按钮当前传的是 `credential_id`（与 runtime_id 语义不符）。设计上改为**按凭据聚合**：新增 `GET /api/runtime/credentials/{cid}/tasks`（按该凭据所有 runtime_id 汇总），或前端按该设备各 runtime_id 分别拉取后合并。

**语义**：本页回答"**有哪些 worker，叫什么、IP 多少、什么 scope、是否在线、凭据是否有效**"。

---

## 5. 与 Agent 配置页的联动（关键）

现状：`agents.js` 的 Backend Engine 下拉读 `d.runtimes` 按 `name` 去重 → 只显示 `opencode` / `codearts` 两个引擎类型，选中后服务端 `get_runtime_id_by_name` 解析到具体实例（S2 健康选择）。

**设计目标**：下拉**直接选择唯一引擎名（engine name）**，即 Engines 页表格中的一行；选择即绑定到该实例（对应 `agent_runtimes.id` = 任务路由 `runtime_id`，显式实例绑定）：

```
Backend Engine:  [opencode-worker-23249 ▾]   ← 直接选 engine name（唯一实例）
  可选值（来自 Engines 页表格）：
    opencode-local               opencode v1.17.7 · grape-server · 在线
    opencode-worker-23249        opencode v1.18.15 · worker-123249 · 在线
    codearts-local               codearts v26.6.2 · grape-server · 在线
    codex-worker-23249           codex v0.1.0 · worker-123249 · 离线   ← 离线置灰
```

- 保存后服务端按所选 `engine name` 定位 `agent_runtimes.id`，写入 `devops_agent_session.runtime_id` / `chat_agent_session.runtime_id`（**显式实例绑定**，替代按 name 的 S2 隐式选择）。
- 离线实例在下拉中**置灰不可选**（可选保留一个"自动（按健康实例）"项兜底，等价现状 S2 行为）。
- 历史已绑定 `runtime_id` 的会话照常生效（id 不变）。

### 5.1 agent 类型 → engine scope 绑定规则

下拉选项按 **agent 类型**过滤 engine 的 `scope`：

| Agent | 可选的 engine scope | 说明 |
|-------|--------------------|------|
| **DevOps Agent**（issue / vod / coding / qa / sre，写 `devops_agent_session`） | **仓库 / 全局** | 业务 Agent 用共享/仓库级引擎；**不可选个人引擎** |
| **Chat Agent**（`chat_agent_session` / web chat / IM） | **个人** | 个人对话用个人引擎；**不可选仓库/全局引擎** |

```
DevOps Agent（如 issue）的 Backend Engine 下拉：
  opencode-repo-abc          opencode v1.18.0 · repo-worker-a · 仓库 · 在线   ✓ 可选
  opencode-local             opencode v1.17.7 · grape-server-local · 全局 · 在线 ✓ 可选
  opencode-worker-23249      opencode v1.18.15 · worker-123249 · 个人 · 在线   ✗ 不出现（个人引擎）

Chat Agent 的 Backend Engine 下拉：
  opencode-worker-23249      opencode v1.18.15 · worker-123249 · 个人 · 在线   ✓ 可选
  opencode-local             opencode v1.17.7 · grape-server-local · 全局 · 在线 ✗ 不出现（全局）
```

- 前端按 agent 类型过滤 `scope` 渲染下拉；**服务端保存时二次校验**（所选引擎的 scope 必须匹配该 agent 类型，不匹配拒绝，防绕过）。
- 引擎可见性叠加：个人引擎仅供其 owner 绑定；**仓库级引擎供该仓库所有成员绑定（不只 owner）**；全局引擎对所有用户可用（与 skills scope 语义对齐）。

**优势**：与多实例设计 §4.2 显式实例绑定对齐 → 不同 Agent 可绑不同 worker 上的引擎，实现负载分担；避免"选了 opencode 却不知道跑在哪台 worker"。

---

## 6. 数据表结构设计

### 6.1 `runtime_credentials`（worker 凭据表）——修改

现状 DDL（`db_setup.py`，`_RUNTIME_CREDENTIALS_DDL`）：

```sql
CREATE TABLE IF NOT EXISTS runtime_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL,             -- sha256(rta_xxx)
    name         TEXT NOT NULL,             -- 管理端可读名（worker 名）
    device_name  TEXT DEFAULT '',           -- 边端自报，辅助展示
    scope        TEXT DEFAULT 'global',
    owner_id     TEXT DEFAULT '',
    status       TEXT DEFAULT 'active',
    last_seen_at TEXT DEFAULT NULL,
    last_ip      TEXT DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (token_hash)
)
```

| 列 | 现状 | 设计改动 |
|----|------|----------|
| `name` | 无唯一约束 | **加 `UNIQUE (name)`**（凭据名=worker 名全局唯一；改名 = `UPDATE name`，id 不变，`instance_name`/视图随之更新） |
| `scope` | 默认 `'global'` | 语义收窄为 `user`/`repo`/`global`，**默认 `'user'`（个人）**；存量 `'global'` 行语义保留（全局） |
| `owner_id` | `''` 空闲 | 复用作 **owner user_name**（生成凭据的当前登录用户；全局暂不设 owner 限制） |
| `owner_name` | — | 可选新增 `owner_name TEXT DEFAULT ''`（与数值 `owner_id` 解耦，存 `user_name`，查询/展示用） |

新增唯一索引（老库幂等）：`CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_credentials_name ON runtime_credentials(name)`；新库 DDL 加 `UNIQUE(name)`。

### 6.2 `agent_runtimes`（引擎实例表）——**不改表**，派生展示字段

现状 DDL（`db_setup.py`，多实例键已上线）：

```sql
CREATE TABLE IF NOT EXISTS agent_runtimes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER DEFAULT 0,
    name          TEXT NOT NULL,            -- 引擎类型（opencode/codearts）
    display_name  TEXT DEFAULT '',
    version       TEXT DEFAULT '',
    cli_path      TEXT DEFAULT '',
    status        TEXT DEFAULT 'offline',
    last_seen_at  TEXT DEFAULT NULL,
    scope         TEXT DEFAULT 'global',
    owner_id      TEXT DEFAULT '',
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (credential_id, name, scope, owner_id)
)
```

设计：
- **不新增列**。前端展示字段均由 API 响应按 `credential_id` JOIN `runtime_credentials` **派生**（见 §7）：
  - `instance_name`（唯一引擎名）：`{name}-{device_suffix}`，无 device_name 回退 `{name}-cred{credential_id}`（§4.2）
  - `worker_name` = 凭据名 `runtime_credentials.name`
  - `scope` / `owner` = 所属凭据的 scope / owner（引擎随 worker 一致）
- 可选：将 `instance_name` 落 `display_name`（register 时写入），便于排序/检索。

### 6.3 既有绑定表——无需改

| 表 | 说明 |
|----|------|
| `devops_agent_session` / `chat_agent_session` | `runtime_id` 已指向 `agent_runtimes.id`（引擎实例），绑定语义不变 |
| `agent_tasks` | `runtime_id` 路由不变；"按凭据聚合查任务" = `WHERE runtime_id IN (该凭据的 agent_runtimes.id)`，现有 `idx_agent_tasks_runtime(runtime_id)` 可覆盖，无需新索引 |

### 6.4 迁移与索引清单

| # | 项 | 位置 |
|---|----|------|
| 1 | `runtime_credentials` 加 `UNIQUE(name)`（先清重：同名多行仅留最小 id） | `db_setup.py` DDL + 迁移 |
| 2 | `runtime_credentials` 加 `owner_name` 列（幂等 `_ensure_column`） | `db_setup.py` 迁移 |
| 3 | `runtime_credentials.scope` 默认值 `'global'`→`'user'`（存量 global 行语义保留） | `db_setup.py` DDL |
| 4 | 唯一索引 `uq_runtime_credentials_name(name)`（老库） | `db_setup.py` 迁移 |
| 5 | （可选）`runtime_credentials(owner_name)` 索引，按 owner 过滤 | `db_setup.py` 迁移 |

## 7. 建议的 API 微调（实施时）

现状 `GET /api/settings/agent-runtimes` 已返回 `{runtimes, devices}`，两子页**无需新增接口**即可落地（除"按凭据查任务"外）：

| 子页 | 数据 | 是否需改 |
|------|------|----------|
| Engines | `runtimes` | **每行补 `instance_name`（唯一引擎名）、`worker_name`/`worker_ip`、`scope`、`owner`**（按 `credential_id` 关联） |
| Workers | `devices` | 补 `worker_ip`（`last_ip`）、`scope`、`owner`（标签化输出） |

`runtimes` 每行已含：`id / name / version / healthy / installed / credential_id / status / last_seen_at / health`。新增派生字段：
- `instance_name`：`{name}-{device_suffix}`（服务端统一派生，规则见 §3.2）
- `worker_name`：`runtime_credentials.device_name`
- `worker_ip`：`runtime_credentials.last_ip`
- `scope` / `owner`：随凭据（worker）一致（引擎不独立归属）

`devices` 每项补充：`scope`（全局/仓库/个人）、`owner`（user_name，=生成凭据的用户）。

**可见性过滤（按当前登录用户）**：`runtimes` / `devices` 响应仅返回当前用户可见项——全局 + 个人（owner=当前用户）+ 我所在仓库的仓库级（**仓库所有成员可见**，不只 owner）。凭据创建（`POST /api/settings/runtime-credentials`）为仓库 scope 时需携带目标 `repo_key`（落 `owner_id`/新 `repo_key` 列），成员关系校验沿用 `managed_repos` / 协作者体系。

凭据接口：`POST /api/settings/runtime-credentials` 入参补 `scope`（user/repo/global，默认 user）；`owner` 由服务端取**当前登录用户**注入（前端不传、不可选）→ 落 `runtime_credentials`（现有 `scope`/`owner_id` 列可复用或加 `owner_name`）。

补充接口（"查看任务"用）：`GET /api/runtime/credentials/{cid}/tasks` —— 按凭据聚合其所有 `agent_runtimes.id` 的 `agent_tasks`（替代现按单个 runtime_id 查询）。

Agent 配置保存：`agents.js` 提交 `engine name`（唯一实例名）→ 服务端按 `instance_name` 解析 `agent_runtimes.id` 写入绑定（`session_manager.py` / `agents.py` 路由解析处支持按实例名而非仅按类型）。

---

## 8. 实现范围（原型 → 落地）

| # | 事项 | 位置 |
|---|------|------|
| 1 | `runtimes` 视图顶部加 Tab（Engines / Workers），默认 Engines，拆 `renderEngines` / `renderWorkers` | `src/api/static/js/pages/runtimes.js` |
| 2 | `renderEngines`：按 scope 分三张表（全局/仓库/个人），各表列（engine name / type / version / worker name / scope / owner / status） | 同上 |
| 3 | `renderWorkers`：按 scope 分三张表，各表列（worker name / ip / scope / owner / 心跳 / 凭据[active+吊销] / 承载引擎），合入原凭据管理 | 同上 |
| 4 | 生成凭据表单：名称（**全局唯一，可后续改名**）+ 范围（个人/仓库/全局，默认个人）；**owner 自动=当前登录用户（不可选）**，全局暂不限 owner | `src/api/static/js/pages/runtimes.js` |
| 5 | `agent-runtimes` 响应为 `runtimes` 补 `instance_name`/`worker_name`/`worker_ip`/`scope`/`owner`；`devices` 补 `worker_ip`/`scope`/`owner` | `src/api/routes/settings.py` |
| 6 | 凭据创建接口入参补 `owner`/`scope`（落 `runtime_credentials`） | `src/api/routes/settings.py` |
| 7 | "查看任务"改按凭据聚合：`GET /api/runtime/credentials/{cid}/tasks`（或前端按各 runtime_id 合并） | `src/api/routes/runtime.py` + `runtimes.js` |
| 8 | vod/issue 页 Backend Engine 下拉直接选 `engine name`；保存提交实例名 → 服务端解析 `agent_runtimes.id` 写入绑定 | `src/api/static/js/pages/agents.js` + `session_manager.py` / `agents.py` |
| 9 | **agent 类型→scope 过滤**：DevOps Agent 下拉仅显示仓库/全局引擎；Chat Agent 仅显示个人引擎；服务端保存二次校验 scope 匹配 | `src/api/static/js/pages/agents.js` + `session_manager.py` / `agents.py` |
| 10 | 测试：两表格正确、`instance_name` 派生、scope/owner 派生、按凭据查任务、下拉选实例名绑定、凭据表单 owner/scope、**agent 类型 scope 过滤** | `tests/`（前端走 UI 场景） |

## 9. 验收

1. Runtime Agent 页顶部出现 Engines / Workers 两个 Tab，**默认 Engines**。
2. Engines 按 scope 分三张表（**全局最上 / 仓库居中 / 个人最下**），每行一唯一引擎实例：engine name / engine type / version / worker name / scope / owner / status。
3. 同一 engine type 多实例并存（opencode 多行，跨 scope 分表）；engine 的 scope/owner 与其 worker 一致。
4. Workers 按 scope 分三张表（全局/仓库/个人），每行：worker name / ip address / scope / owner / 心跳 / 凭据（active + 吊销，合入原凭据管理区）。
5. **生成凭据**表单含 名称 + 范围（默认个人）；**归属用户自动=当前登录用户（不可选）**，全局暂不限 owner；worker 注册后凭据即标识其归属用户与范围。
6. 吊销凭据后：凭据标 revoked、其引擎行 offline、该 worker 从在线分组消失。
7. vod/issue 页 Backend Engine 下拉**直接选 engine name**（唯一实例），离线实例置灰；保存后绑定到该实例 `runtime_id`，历史绑定不失效。
8. **agent 类型→scope 过滤**：DevOps Agent（issue/vod/coding/qa/sre）下拉仅出现**仓库/全局**引擎；Chat Agent 下拉仅出现**个人**引擎；服务端保存拒绝 scope 不匹配的选择。

---

## 10. 测试用例设计

> 服务端逻辑可单测（`tests/`）；前端 Tab/表格走 UI 场景。与 §8/§9 一一对应。

### 10.1 服务端单测（`tests/test_runtime_protocol.py` / `tests/test_runtime.py`）

**S1 instance_name / scope / owner 派生**（`GET /api/settings/agent-runtimes`）
- `test_runtimes_derive_instance_name_scope_owner`：注册凭据 A（name=`worker-1`，owner=当前用户，scope=user）→ `runtimes` 该行 `instance_name == "opencode-worker-1"`、`scope=="user"`、`owner==当前用户`、`worker_name=="worker-1"`。
- `test_instance_name_fallback_cred_id`：凭据无 `device_name` → `instance_name == "{type}-cred{credential_id}"`。
- `test_instance_name_global`：scope=global 的凭据 → 引擎行 scope/owner 与凭据一致（owner=生成用户，非强制 grape_bot）。

**S2 凭据名称全局唯一 + 可改名**
- `test_credential_name_global_unique`：创建两个同名凭据 → 第二个被拒（409/400）。
- `test_credential_rename_updates_views`：改名凭据 → `devices`/`runtimes` 的 `worker_name`/`instance_name` 同步新名。

**S3 凭据 owner = 当前登录用户（不可选）**
- `test_create_credential_owner_is_current_user`：以用户 U 调 `POST /api/settings/runtime-credentials`（body 仅 name+scope）→ 落库 owner=U；body 传 owner 被忽略/拒绝。

**S4 worker name = 凭据名**
- `test_devices_worker_name_is_credential_name`：凭据 name=`worker-123249`、`device_name=grape-server` → `devices[].worker_name=="worker-123249"`（非 device_name）。

**S5 按凭据聚合查任务**
- `test_credential_tasks_aggregates_runtimes`：凭据含 2 个引擎（2 个 runtime_id），各入队任务 → `GET /api/runtime/credentials/{cid}/tasks` 返回两任务；单个 runtime 查询接口仍可用。

**S6 scope 分组数据**
- `test_runtimes_grouped_by_scope`：混合 global/repo/user 三 scope 的实例 → 响应可按 scope 过滤/分组（三组各含正确实例）。

**S7 agent 类型 → engine scope 过滤**（§5.1）
- `test_devops_agent_only_repo_global`：DevOps agent（issue 等）的候选引擎仅含 scope∈{repo,global}；个人引擎不出现。
- `test_chat_agent_only_user_scope`：Chat agent 的候选引擎仅含 scope=user；仓库/全局引擎不出现。
- `test_save_rejects_scope_mismatch`：DevOps agent 保存选个人引擎 → 拒绝（400）；Chat agent 保存选全局引擎 → 拒绝。

**S8 仓库级 worker/引擎成员可见性**（§4.1 / §7）
- `test_repo_scope_visible_to_all_members`：成员 B 与 owner A 同仓库 → 双方 `devices`/`runtimes` 均可见 A、B 名下仓库级 worker/引擎（不只 owner）。
- `test_personal_not_visible_to_others`：A 的个人 worker 对非 owner 用户不可见（不出现在其列表中）。
- `test_repo_scope_hidden_for_non_member`：非该仓库成员用户看不到该仓库的仓库级 worker/引擎。

### 10.2 前端 / UI 场景（`tests/ui_scenario_test.py` 或手工）

- **T1 双 Tab**：Runtime Agent 页默认 Engines；切 Workers 后凭据生成/吊销/查看任务可用。
- **T2 三表分组**：Engines 与 Workers 各按 scope 显示全局/仓库/个人三张表，空分组不渲染；行与后端数据一致。
- **T3 生成凭据**：表单无"归属用户"选择（显示当前登录用户）；名称重名被拒；生成后 token 仅展示一次。
- **T4 下拉选实例名**：vod/issue Configure 的 Backend Engine 下拉列出各唯一 engine name，离线置灰；保存后绑定对应 `runtime_id`，重新打开回显选中项。
- **T5 吊销联动**：吊销凭据 → Workers/Engines 对应 worker/引擎下线、离线分组出现。
