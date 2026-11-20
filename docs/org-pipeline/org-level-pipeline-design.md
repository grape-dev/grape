# 组织级仓库与组织流水线设计（Org Hub / Org Pipeline）

> 状态：设计稿 v1（2026-11-11）
> 目标：回答“基于现有 Grape 是否可行 + 怎么改”，并给出可直接进入评审/排期的技术方案。
> 代码基线：`arch-optimization-chenyan`（HEAD `73d2b3e07`）
> 关联模块：`src/domains/mgmt/routes/orgs.py`、`src/domains/mgmt/user/org_db.py`、
> `src/domains/mgmt/routes/repos.py`、`src/domains/mgmt/routes/scenes.py`、
> `src/api/pipeline_config.py`、`src/api/pipeline_executor.py`、
> `src/domains/runtime/pipeline/event_pipeline.py`、`src/domains/runtime/engine/service.py`、
> `src/domains/runtime/engine/scheduler.py`、`src/api/knowledge.py`、`src/api/issues_center.py`

---

## 0. 结论

**可行。** 需求不是要重写 Grape，而是在现有「组织 + 仓库 + 场景 + 事件/Agent + 知识」四层地基上加一层**组织级编排**。

三条关键结论：

1. **组织模型与租户隔离已就绪**：`organization` / `org_member` 已存在，且 13 张核心业务表已统一有 `org_id` 列（`src/api/db_setup.py::_ORG_ID_TABLES`），组织场景、组织知识、组织需求可以直接按 `org_id` 隔离，不需要新造租户体系。
2. **“组织流水线节点可以是 2 个或 3 个”天然成立**：场景节点本来就是 `nodes_json` 里的有向图（`next` 指向下一节点，`done` 结束），节点数量没有硬编码为 5；现在的 5 个（vod/issue/coding/qa/sre）只是默认官方模板。**但**节点 `id` 必须属于可执行 Agent 角色集合，所以组织流水线要支持“下派/审批”这类平台节点，必须扩展节点类型（见 §9）。
3. **跨仓下派不能复用现有 A2A 交棒**：`_cross_pipeline_handoff` 是**同仓跨场景**（`repo_key` 不变），跨仓库必须“在目标仓库新建子 Issue + 绑定目标场景 + 触发目标场景首节点”。这是本方案的核心新增服务（见 §11）。

主要新增工作量集中在一件事上：**一个服务端组织流水线编排器**。其余都是小改和复用。

---

## 1. 需求 → 设计目标

| 编号 | 原始需求 | 设计目标（可验收） |
|---|---|---|
| R1 | 创建组织后自动创建组织仓库，并自动创建组织流水线 | 团队组织创建成功后，异步 + 幂等地完成：建 VCS 仓库 → 托管（webhook/bot）→ 注册 `managed_repos(org_id, repo_kind='org')` → 创建组织场景（默认 3 节点）。失败可重试，**不阻塞组织创建**。 |
| R2 | 组织可以关联不同的其他仓库 | 组织维护“参与协同的仓库列表”，每个仓库可配默认场景、能力标签、是否参与下派；MVP 只允许**同组织**仓库。 |
| R3 | 在组织流水线中提需求，下派到各仓库的 grape 场景执行 | 组织需求是组织仓库里的真实 Issue；编排器按节点图推进，下派节点在目标仓库创建子 Issue、绑定目标仓库场景、触发其现有流水线；组织侧统一跟踪与汇总。 |
| R4 | 组织仓库存储组织级上下文/知识库，下派时携带 | 组织上下文/知识以组织仓库为权威载体；下派时生成**上下文快照**，注入子任务（`agent_tasks.knowledge_files`）与子 Issue 描述。 |
| R5 | 组织流水线重新定义节点，可以是 2/3 个 | 组织流水线是独立节点图：默认 3 节点（受理 → 下派 → 验收），支持 2 节点预置；节点种类 = `agent` / `dispatch` / `gate`。 |

### 非目标（本期不做）

- 不做跨组织下派与跨组织知识共享（预留字段，二期）。
- 不替换仓库级流水线与现有 5 节点模板。
- 不新增 Agent 引擎类型；组织 Agent 节点复用 `vod/issue/coding/qa/sre`。
- 不做组织级计费、配额结算。
- 不做“组织仓库”的代码化 CI（组织仓库不承载业务代码）。

---

## 2. 现有地基与硬约束（先看清边界）

### 2.1 可复用能力

| 能力 | 现有实现 | 复用方式 |
|---|---|---|
| 组织与成员 | `organization`、`org_member`、`org_invitation`；`org_db.py`；`orgs.py` | 组织归属、角色鉴权、成员管理 |
| 组织隔离 | `_ORG_ID_TABLES` 13 张表已有 `org_id` | 组织场景/知识/需求/任务天然可按 `org_id` 过滤 |
| 仓库托管 | `managed_repos`（含 `org_id`、`dev_scene`、`host_status`）；`repos.py::create_new_repo` / `host_repo` | 组织仓库走同一套托管链路 |
| 仓库缓存 | `user_repos`（用户可见仓库缓存） | 关联仓库选择器的数据源 |
| 场景/流水线 | `devops_pipeline_scene`（`scene_id + repo_key` 唯一，`nodes_json`、`linked_repos`、`org_id`） | 组织流水线复用同表，加 `scene_scope` 区分 |
| 流水线读取 | `pipeline_config.get_pipeline_config(scene_id, repo_key)`：DB 场景 → 市场模板 → `pipelines.json` → default | 组织场景按 `repo_key=组织仓库` 读取，**读取链路零改动** |
| 节点执行 | `pipeline_executor.dispatch_node`：`emit / waterfall / serial / parallel` 四种模式；`_enqueue_pipeline_task` | Agent 节点直接复用 |
| 事件与流水线 | `event_pipeline.run_pipeline` + `append_event` + A2A | 子仓执行完全复用 |
| 需求（Issue） | `issue` 表（`stage/owner_agent/scene_id/parent_issue_id/carried_context/org_id`）+ `issue_state` | 组织需求与子 Issue 都落在真实 Issue 上 |
| 建单 | `issues_center.create_task_via_issue` + `_post_gitcode_issue` / `_post_github_issue` | 子 Issue 走 `create_task_via_issue`（内部身份），组织需求走底层建单函数 |
| 知识 | `agent_shared_knowledge`（含 `org_id`）+ `scene_knowledge_ref` + `knowledge.py` + `service.collect_knowledge_files` | 组织知识=组织仓库上的仓库级知识，注入链路复用 |
| 后台循环 | `scheduler.py::scheduler_loop`（60s）+ `run_with_distributed_lock` | 组织编排器同款范式，避免多 worker 重复执行 |
| 前端 | `src/api/static/js/pages/organizations.js` 等静态页 | 在组织详情里扩 Tab |

### 2.2 四个必须正面处理的硬约束

**约束 A：节点 `id` 必须是已知 Agent 角色。**
`scenes.py::_validate_scene_node_roles` 要求节点 `id ∈ KNOWN_AGENT_ROLES = {coding, issue, qa, vod, chat, sre}`（`engine_select.py`）。`tag` 只是展示名，可为中文。
→ 组织流水线的 `dispatch` / `gate` 节点**不能**靠新增 Agent 角色实现，必须扩展“节点类型（kind）”，并在校验里按 kind 放行。

**约束 B：A2A 目标角色白名单是硬编码的。**
`webhook.py::a2a_endpoint` 只接受 `("vod","issue","coding","qa","sre")`。
→ 组织平台的 `dispatch/gate` 节点**永远不经过 A2A**；组织 Agent 节点只用这 5 个角色，因此不冲突。

**约束 C：事件总线是进程内的。**
`event_bus.py` 明确是 in-process，且 mgmt/worker/runtime 多进程 + `--workers N`。
→ 编排器**不能**依赖事件总线唤醒，必须 DB 轮询 + 分布式锁（复用 `scheduler_loop` 范式）。

**约束 D：VCS 建仓只支持个人命名空间。**
`repo_manager.create_repo` 调用 GitCode `POST /api/v5/user/repos`，只能建在 token 所属用户下；GitCode 没有 Grape 意义的“子组织”命名空间。
→ 组织仓库实际建在**组织 owner 的 VCS 账号**下，命名 `grape-org-<slug>`；这是必须向用户说明的限制（§20 Q1）。

---

## 3. 设计决策（含被否决方案）

### D1：组织流水线复用 `devops_pipeline_scene`，`scene_scope='org'`，`repo_key=组织仓库`

不新建 `org_pipeline` 表，原因是场景编辑器、节点校验、引擎绑定、前端详情页都能复用。

组织场景存法：

- `repo_key = 组织仓库 repo_key`（**不是空串**）
- `org_id = 组织 id`
- `scene_scope = 'org'`
- `scene_id = 'org-<slug>-default'`（或 uuid，全局唯一即可）

为什么把 `repo_key` 设为组织仓库而不是空串：

- `get_pipeline_config(scene_id, repo_key)`、`_query_scene_by_id`、`enqueue_task(knowledge 打包)` 全部按 `repo_key` 定位，**零改动即可读到组织场景**；
- 组织知识、project-info、工作区目录也都以 `repo_key` 为根，天然复用；
- `UNIQUE(scene_id, repo_key)` 仍然成立，不需要重建表。

代价：组织场景会出现在组织仓库的场景列表里 —— 这是符合语义的（组织仓库只有这一条流水线），前端只要在“组织仓库”视角展示即可。

> 被否决：`repo_key=''` + `org_id` 查组织场景。需要改 `get_pipeline_config`、`_query_scene_by_id`、所有 `repo_key` 读取点，改动面大且易漏。

### D2：组织流水线由服务端编排器驱动，Agent 只负责“单个节点”

- `dispatch` 节点 = 平台事务（批量建子 Issue、绑场景、触发、幂等），不能放提示词里。
- `gate` 节点 = 人工审批，等待时间不可控。
- 验收节点要“等所有子任务终态”，Agent 自己等会长期占用引擎。

所以节点图的推进权在 `OrgPipelineRunner`，Agent 节点执行完只产出结果，由 Runner 解析并推进。

> 被否决：让 Agent 用 A2A 自动交棒到下一个组织节点。不可控（Agent 可能忘交棒/重复交棒），且跨仓建单/等待无法在 Agent 内可靠实现。

### D3：组织需求 = 组织仓库里的真实 Issue

好处：复用事件、任务、对话、评论、@、外链、审计、任务中心。

新增标记（避免被普通流水线接管）：

```sql
ALTER TABLE issue ADD COLUMN is_org_demand INTEGER NOT NULL DEFAULT 0;
ALTER TABLE issue ADD COLUMN org_demand_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE issue ADD COLUMN org_dispatch_item_id INTEGER NOT NULL DEFAULT 0;  -- 子 Issue 用
```

组织需求的 `issue_state.scene_id` 写组织场景 id，`stage` 写当前 Agent 节点角色（便于任务中心展示）；真实“当前组织节点”以 `org_demand.current_node_id` 为准。

**必须加一道守卫**：`event_pipeline` 在路由 `issue_opened` 前检查 `issue.is_org_demand=1`，命中则跳过默认 5 节点路由，只记录事件并交给编排器。否则组织需求会被普通 `vod` 节点抢跑。

> 被否决：不做真实 Issue，只建 `org_demand` 行。失去评论/外链/任务中心/审计，用户直觉差。

### D4：下派 = 在目标仓库建子 Issue + 绑目标场景 + 触发目标场景首节点

跨仓下派不是“移动 Issue”，而是在目标仓库**新建**子 Issue：

1. 用目标仓库的 bot token（内部身份）建真实 VCS Issue；
2. `create_task_via_issue` 写 `issue.scene_id = 目标场景`；
3. 由它同步触发目标仓库 `issue_opened` → 目标场景首节点开跑；
4. 子 Issue 带 `org_demand_id / org_dispatch_item_id`，组织侧可反查；
5. 子仓完全按自己的节点图执行，组织侧只观察终态。

> 被否决：直接调用目标仓库 A2A 伪造成 `issue_number`。目标仓库没有该 Issue 会触发 A2A 的 `managed_repos` 与 Issue 存在性校验，且没有 Issue 记录无法跟踪。

### D5：组织上下文/知识 = 组织仓库上的“仓库级知识”，下派时快照注入

- 权威载体：组织仓库工作区 `org-context/`（`context.md`、`standards/*.md`、`knowledge/*.md`、`repos.json`）。
- 索引：`agent_shared_knowledge`（`repo_key=组织仓库`、`scope='project'`、`org_id=组织 id`）。**不新增 scope**，避免改 `knowledge_path` / `read_knowledge` / `knowledge_storage_key` 三处白名单。
- 快照：下派时把命中的文件内容 + hash 存 `org_context_snapshot`，子任务按快照注入，保证同一次下派上下文一致。
- 注入点：`enqueue_task` 调 `collect_knowledge_files` 后，若该任务带 `org_demand_id`，再合并组织仓库知识（前缀 `org-context/`），受 `_is_credential_file` 过滤 + 总大小上限（默认 256KB）。

> 被否决：新增 `scope='org'`。`knowledge_path` 只认 `project/issue/issue_agent`，新增要改存储路径、OBS key、读写合并三处，收益仅为命名语义。

### D6：节点模型 = `agent | dispatch | gate`，默认 3 节点、支持 2 节点

节点数量没有硬编码为 5，`nodes_json` 本来就是图。组织流水线只是**解释执行**这套图，并新增两种平台节点。

默认 3 节点：`intake(agent/vod) → dispatch(system) → accept(agent/qa)`
2 节点预置：
- `dispatch → accept`（需求已足够明确，不需要组织澄清）
- `intake → dispatch`（下派即完成，子仓自行闭环）

为什么默认 3：组织级需求的价值在“统一拆解 + 统一验收”；2 节点用于轻量批量协同（如“同步改 3 个仓库的依赖版本”）。

### D7：MVP 只允许同组织仓库

`managed_repos.org_id` 是权威归属。下派目标必须：已托管 && `org_id` 相同 && 在 `org_repo_link` 且 `dispatch_enabled=1`。

跨组织二期：新增组织互信授权，子 Issue 归属仍记目标仓库所在组织，禁止改归属。`org_repo_link` 预留 `owner_org_id` 字段但不启用。

### D8：组织仓库单列类型，不混入普通仓库列表

`managed_repos` 新增 `repo_kind`（`code` / `org`）：
- `code`（默认）：现有业务仓库；
- `org`：组织中枢仓库，**不出现在**“我的仓库/项目列表”，只从组织入口进入；
- 事件/流水线仍按普通仓库跑（组织需求 Issue 就在这里）。

### D9：功能开关与灰度

- 环境变量 `GRAPE_ORG_PIPELINE=1` 才启用自动建仓与编排器；
- 已有组织不自动补建，提供 `POST /api/orgs/{id}/hub/ensure` 手动触发；
- 关闭开关时组织功能退化为“只有成员管理”，不影响任何现有仓库。

---

## 4. 总体架构

```text
                         ┌──────────────────────────────────────────┐
                         │  Grape 组织（organization / org_member）  │
                         └───────────────┬──────────────────────────┘
                                         │ 1:1
                 ┌───────────────────────▼──────────────────────────┐
                 │ 组织仓库（managed_repos.repo_kind='org'）          │
                 │ org-context/  ← 组织上下文、标准、知识库            │
                 │ Issue #N      ← 组织需求（真实 Issue）              │
                 └───────────────────────┬──────────────────────────┘
                                         │ 关联 1:N
                 ┌───────────────────────▼──────────────────────────┐
                 │ 组织流水线（devops_pipeline_scene.scene_scope='org'）│
                 │ intake(agent/vod) → dispatch(system) → accept(qa) │
                 │ 或 2 节点：dispatch → accept / intake → dispatch   │
                 └───────────────────────┬──────────────────────────┘
                                         │
                 ┌───────────────────────▼──────────────────────────┐
                 │ OrgPipelineRunner（worker 进程，10s tick，分布式锁）│
                 │ 节点推进 / 等待 / 重试 / 汇总 / 知识沉淀            │
                 └──────┬────────────────────────────────┬──────────┘
        下派            │                                │ 汇总
                        ▼                                ▼
        ┌──────────────────────────────┐      ┌──────────────────────┐
        │ OrgDispatchService           │      │ 组织验收 / 知识回写    │
        │ 建子 Issue + 绑场景 + 触发    │      └──────────────────────┘
        └──────────────┬───────────────┘
                       │ 每个目标仓库独立跑自己的场景（5 节点/自定义）
        ┌──────────────┼──────────────┬───────────────┐
        ▼              ▼              ▼               ▼
   ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐
   │ 仓库 A   │    │ 仓库 B   │    │ 仓库 C   │    │ 组织仓库 │
   │ 场景 A   │    │ 场景 B   │    │ 场景 C   │    │（锚点）  │
   └─────────┘    └─────────┘    └─────────┘    └─────────┘
```

边界：

- 组织流水线只做**组织级编排**，不编码业务逻辑；
- 子仓流水线由目标仓库场景决定，组织不硬编码节点；
- 组织仓库是锚点，不承载业务代码；
- 平台节点（dispatch/gate）不占用任何引擎。

---

## 5. 数据模型

> 统一在 `src/api/db_setup.py` 注册：`_NEW_TABLES.extend([...])` + `_ensure_column(...)` 幂等迁移；
> 字段类型沿用现有 SQLite DDL 风格，MySQL 侧由翻译层处理。

### 5.1 新表

```sql
-- 组织中枢：每个团队组织一行，记录组织仓库/组织流水线的供给状态
CREATE TABLE IF NOT EXISTS org_hub (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER NOT NULL UNIQUE,
    repo_key      TEXT NOT NULL DEFAULT '',    -- 组织仓库（gitcode:owner/grape-org-slug）
    scene_id      TEXT NOT NULL DEFAULT '',    -- 组织流水线场景 id
    status        TEXT NOT NULL DEFAULT 'pending',
                  -- pending | provisioning | ready | pending_credentials | failed
    stage         TEXT NOT NULL DEFAULT '',    -- repo_created | hosted | scene_ready
    error         TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT DEFAULT (datetime('now')),
    settings      TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
)

-- 组织关联仓库
CREATE TABLE IF NOT EXISTS org_repo_link (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id            INTEGER NOT NULL,
    repo_key          TEXT NOT NULL,
    alias             TEXT DEFAULT '',
    dispatch_enabled  INTEGER NOT NULL DEFAULT 1,
    default_scene_id  TEXT DEFAULT '',         -- 下派默认场景（空=按仓库默认）
    capabilities      TEXT DEFAULT '[]',       -- JSON: ["backend","web"]，供 plan 节点匹配
    priority          INTEGER NOT NULL DEFAULT 5,
    created_by        TEXT DEFAULT '',
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (org_id, repo_key)
)

-- 组织需求（锚点 = 组织仓库真实 Issue）
CREATE TABLE IF NOT EXISTS org_demand (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id              INTEGER NOT NULL,
    org_repo_key        TEXT NOT NULL,
    issue_number        INTEGER NOT NULL DEFAULT 0,
    title               TEXT NOT NULL DEFAULT '',
    description         MEDIUMTEXT DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'queued',
                        -- queued | running | pending_approval | succeeded
                        -- | partial_failed | failed | cancelled
    current_node_id     TEXT DEFAULT '',
    scene_id            TEXT DEFAULT '',
    plan_json           MEDIUMTEXT DEFAULT '{}',
    targets_json        MEDIUMTEXT DEFAULT '[]',  -- 用户指定目标 [{repo_key, scene_id}]
    context_snapshot_id INTEGER NOT NULL DEFAULT 0,
    created_by_user_id  INTEGER NOT NULL DEFAULT 0,
    created_by          TEXT DEFAULT '',
    started_at          TEXT DEFAULT NULL,
    finished_at         TEXT DEFAULT NULL,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now')),
    UNIQUE (org_repo_key, issue_number)
)

-- 组织流水线节点运行记录（幂等 + 重试 + 审计）
CREATE TABLE IF NOT EXISTS org_pipeline_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    demand_id   INTEGER NOT NULL,
    node_id     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'agent',   -- agent | dispatch | gate
    status      TEXT NOT NULL DEFAULT 'pending',
                -- pending | running | waiting | succeeded | failed | skipped
    attempt     INTEGER NOT NULL DEFAULT 0,
    task_id     TEXT DEFAULT '',                 -- agent 节点：agent_tasks.id
    batch_id    INTEGER NOT NULL DEFAULT 0,      -- dispatch 节点：org_dispatch_batch.id
    input_json  MEDIUMTEXT DEFAULT '{}',
    output_json MEDIUMTEXT DEFAULT '{}',
    error       TEXT DEFAULT '',
    started_at  TEXT DEFAULT NULL,
    finished_at TEXT DEFAULT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (demand_id, node_id, attempt)
)

-- 下派批次
CREATE TABLE IF NOT EXISTS org_dispatch_batch (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    demand_id    INTEGER NOT NULL,
    node_run_id  INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',  -- running | succeeded | partial_failed | failed
    total        INTEGER NOT NULL DEFAULT 0,
    succeeded    INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
)

-- 下派明细（一条 = 一个目标仓库的子 Issue）
CREATE TABLE IF NOT EXISTS org_dispatch_item (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id           INTEGER NOT NULL,
    demand_id          INTEGER NOT NULL,
    org_id             INTEGER NOT NULL,
    target_repo_key    TEXT NOT NULL,
    target_scene_id    TEXT DEFAULT '',
    child_issue_number INTEGER NOT NULL DEFAULT 0,
    child_task_id      TEXT DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'pending',
                       -- pending | created | running | succeeded | failed | skipped
    done_when          TEXT NOT NULL DEFAULT 'issue_done',
    result_summary     TEXT DEFAULT '',
    error              TEXT DEFAULT '',
    created_at         TEXT DEFAULT (datetime('now')),
    updated_at         TEXT DEFAULT (datetime('now')),
    UNIQUE (batch_id, target_repo_key)
)

-- 组织上下文快照
CREATE TABLE IF NOT EXISTS org_context_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER NOT NULL,
    demand_id     INTEGER NOT NULL DEFAULT 0,
    files_json    MEDIUMTEXT DEFAULT '{}',      -- {rel: {hash, size, content|obs_key}}
    content_hash  TEXT DEFAULT '',
    total_bytes   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
)
```

### 5.2 既有表扩展

```sql
ALTER TABLE managed_repos            ADD COLUMN repo_kind TEXT NOT NULL DEFAULT 'code';   -- code | org
ALTER TABLE devops_pipeline_scene     ADD COLUMN scene_scope TEXT NOT NULL DEFAULT 'repo'; -- repo | org
ALTER TABLE issue                    ADD COLUMN is_org_demand INTEGER NOT NULL DEFAULT 0;
ALTER TABLE issue                    ADD COLUMN org_demand_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE issue                    ADD COLUMN org_dispatch_item_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_tasks              ADD COLUMN org_demand_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_tasks              ADD COLUMN org_node_run_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_tasks              ADD COLUMN org_context_snapshot TEXT DEFAULT '{}';
```

已有可直接用、无需新增的列：`managed_repos.org_id`、`devops_pipeline_scene.org_id`、`agent_shared_knowledge.org_id`、`issue.org_id/scene_id/carried_context`、`agent_tasks.org_id`。

### 5.3 迁移与兼容

- 全部 `_ensure_column(..., default)`，存量数据语义不变（`repo_kind='code'`、`scene_scope='repo'`、`is_org_demand=0`）。
- 不建外键（与现有风格一致），一致性由服务层保证。
- 回滚：停 Runner → 关开关 → 新表可直接 DROP（无既有表依赖），新增列保留无副作用。

---

## 6. 组织仓库与组织流水线自动创建

### 6.1 触发

```python
# src/domains/mgmt/routes/orgs.py::create_organization
org = create_org(...)
if not org.get("is_personal"):
    _ensure_org_hub_async(org["id"])    # asyncio.create_task，失败只记日志
```

- 快路径：mgmt 进程内 `asyncio.create_task(ensure_org_hub(org_id))`；
- 兜底：worker 进程的 `OrgPipelineRunner` 每轮扫描 `org_hub.status ∈ {pending, failed, pending_credentials}` 且 `next_retry_at <= now`，重试；
- 两条路径都走同一把分布式锁 `org_hub:{org_id}`，保证幂等。

### 6.2 供给流程（`ensure_org_hub`）

```text
1. 取 org + owner；非团队组织（is_personal=1）直接 ready + 跳过
2. 加锁 org_hub:{org_id}（ttl 300s）；非 ready 状态才继续
3. 解析 owner 的 VCS 凭证：provider = gitcode/github，token = get_user_token_with_refresh(owner_username)
   ├─ 无 token → status='pending_credentials'，error='组织 owner 未绑定 GitCode'
   └─ 有 → 继续
4. 建仓：name = grape-org-<slug>（冲突则 -2/-3），private=True，auto_init=True
   → 记录 stage='repo_created'
5. 托管：setup_webhook + bot token；add_managed_repo(..., org_id=<org>, repo_kind='org')
   → ensure_default_agent_sessions(repo_key)
   → 记录 stage='hosted'
6. 创建组织流水线：create_custom_scene(scene_id='org-<slug>-default', repo_key=<org repo>,
   nodes=<默认 3 节点>, scene_scope='org', org_id=<org>)
   → managed_repos.dev_scene = scene_id
   → 记录 stage='scene_ready'
7. org_hub.status='ready'；organization.settings.hub = {repo_key, scene_id, ...}
8. 释放锁；任一步异常 → status='failed'，error=可读原因，next_retry_at=now+退避
```

关键实现点：

- `repo_manager.create_repo` 当前写死 `auto_init=False`，需加参数 `auto_init: bool = False`；组织仓库传 `True`，否则空仓库无默认分支，部分 clone/工作区逻辑会异常。
- 托管复用 `repos.py::create_new_repo` 中已有的 webhook/bot 逻辑，建议抽出 `host_repo_for_org()` 复用，避免复制粘贴。
- 组织仓库命名不可与用户已有仓库冲突，`check_repo_name` 已有接口可先探测。

### 6.3 失败与用户可见性

| 状态 | 含义 | 用户动作 |
|---|---|---|
| `provisioning` | 正在建仓/托管 | 等待（前端轮询） |
| `pending_credentials` | 组织 owner 未绑定 VCS | 提示 owner 去绑定，或手动指定已有仓库 |
| `failed` | 建仓/托管/建场景失败 | 展示 error + “重试”按钮 |
| `ready` | 可用 | 进入组织流水线/需求 |

重试接口：`POST /api/orgs/{org_id}/hub/ensure`（org admin）。

### 6.4 手动指定组织仓库（兜底）

`PUT /api/orgs/{org_id}/hub`，body `{repo_key}`：把一个已托管的同组织仓库设为组织仓库（`repo_kind='org'`），并创建组织场景。用于：
- owner 无 VCS token；
- 组织已有“文档/规范仓库”，不想再建新仓。

校验：仓库已托管、`managed_repos.org_id == org_id`、非其他组织的 hub。

---

## 7. 组织关联仓库

### 7.1 语义

- `org_repo_link` 是**组织级登记表**，回答“哪些仓库参与本组织协同”。
- 与场景的 `linked_repos` 区分：后者是“这一个场景执行时可能跨仓 A2A 的关联仓库”（鉴权信任域用，`webhook._authorize_a2a`），不是组织级台账。本方案不合并二者。

### 7.2 规则

- 只能登记同组织已托管仓库（`managed_repos.org_id == org_id`）；
- 组织仓库本身不可登记为下派目标；
- `dispatch_enabled=0` 时保留展示但不参与 `all_members` 策略；
- 删除关联：软删除（`is_active`）或硬删除？→ **软删除**（保留历史下派明细可追溯），新增 `is_active INTEGER DEFAULT 1`。

### 7.3 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/orgs/{org_id}/repos` | 关联仓库列表（含仓库名、默认场景、可用引擎数、最近下派数） |
| POST | `/api/orgs/{org_id}/repos` | 添加 `{repo_key, alias, default_scene_id, capabilities}` |
| PATCH | `/api/orgs/{org_id}/repos/{link_id}` | 改默认场景/开关/标签 |
| DELETE | `/api/orgs/{org_id}/repos/{link_id}` | 软删除 |
| GET | `/api/orgs/{org_id}/repos/candidates` | 可选仓库（同组织已托管、未登记、非 hub） |

---

## 8. 组织流水线节点模型

### 8.1 节点 JSON

在现有 `{id, tag, skill, skill_id, mode, next, handoff, skip, enabled, engine_runtime_ids}` 上扩展：

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | string | `agent`（默认） / `dispatch` / `gate` |
| `label` | string | 展示名，回退 `id` |
| `config` | object | kind 专属配置 |
| `timeout_sec` | int | 节点超时（agent 默认 3600，dispatch 86400，gate 172800） |

- `kind='agent'`：`id` 必须是已知角色（vod/issue/coding/qa/sre），`skill` 必填（建议），`handoff='server'`（禁止 Agent 自己交棒）。
- `kind='dispatch'`：`id` 任意（如 `dispatch`），不允许 `tag/skill`。
- `kind='gate'`：`id` 任意，不允许 `tag/skill`。

### 8.2 默认 3 节点

```json
[
  {
    "id": "intake", "kind": "agent", "label": "需求受理与拆解",
    "tag": "vod", "skill": "org-demand-intake", "skill_id": 0,
    "mode": "emit", "next": "dispatch", "handoff": "server", "enabled": true,
    "config": { "output_contract": "org_plan_v1", "max_targets": 8 }
  },
  {
    "id": "dispatch", "kind": "dispatch", "label": "跨仓下派",
    "next": "accept", "enabled": true,
    "config": {
      "strategy": "plan",
      "max_items": 8,
      "max_concurrency": 3,
      "scene_policy": "link_default",
      "on_item_failure": "continue",
      "child_done_when": "issue_done",
      "context": { "include_org_context": true, "include_org_knowledge": true, "max_context_bytes": 262144 },
      "retry": { "max_attempts": 2, "backoff_sec": 60 }
    }
  },
  {
    "id": "accept", "kind": "agent", "label": "组织验收与沉淀",
    "tag": "qa", "skill": "org-demand-accept",
    "mode": "emit", "next": "done", "handoff": "complete", "enabled": true,
    "config": { "wait_for_batch": true, "write_knowledge": true }
  }
]
```

### 8.3 2 节点预置

```json
// 轻量：直接下派 + 验收
[
  {"id":"dispatch","kind":"dispatch","label":"跨仓下派","next":"accept","config":{...}},
  {"id":"accept","kind":"agent","label":"组织验收","tag":"qa","skill":"org-demand-accept","next":"done","handoff":"complete"}
]
```

```json
// 只下派：子仓自行闭环
[
  {"id":"intake","kind":"agent","label":"需求受理","tag":"vod","skill":"org-demand-intake","next":"dispatch","handoff":"server"},
  {"id":"dispatch","kind":"dispatch","label":"跨仓下派","next":"done","config":{...}}
]
```

### 8.4 `dispatch.config` 字段

| 字段 | 取值 | 说明 |
|---|---|---|
| `strategy` | `plan` / `manual` / `all_members` | `plan`=解析 intake 输出；`manual`=需求里指定 targets；`all_members`=所有启用关联仓库 |
| `max_items` | int（默认 8，上限 20） | 防一次下派过多 |
| `max_concurrency` | int | 并发触发子流水线数（仍受引擎容量约束） |
| `scene_policy` | `link_default` / `explicit` / `smart` | 目标场景解析策略 |
| `on_item_failure` | `continue` / `abort` | 单仓失败是否终止整批 |
| `child_done_when` | `issue_done` / `pr_merged` / `task_terminal` | 子任务完成判定 |
| `context` | object | 注入策略 |

### 8.5 `gate.config`

```json
{ "approvers": ["owner", "admin"], "timeout_hours": 24, "on_timeout": "reject" }
```

审批接口：`POST /api/orgs/{org_id}/demands/{demand_id}/approve`，body `{approved, comment}`。

### 8.6 校验（新增 `validate_org_pipeline(nodes)`）

与仓库场景校验分离，避免污染现有逻辑：

- 节点数 2–5；`id` 唯一；
- `next` 必须存在或为 `done`；无环；所有节点从首节点可达；
- 恰好 1 个 `dispatch`；
- `gate` 只能在 `dispatch` 之前；
- `accept`（kind=agent 且在 dispatch 之后）可选；
- `kind=agent` 的 `id ∈ KNOWN_AGENT_ROLES`；
- `kind=dispatch/gate` 不得有 `tag/skill`；
- `scene_policy=explicit` 时 target 的 `scene_id` 必须存在于对应仓库；
- 失败返回 400 + `problems[]`，不落库。

**校验入口改造**：`scenes.py::_validate_scene_node_roles(nodes)` 增加参数 `scene_scope`，`org` 场景对 `dispatch/gate` 节点放行、对 agent 节点仍走角色白名单。

---

## 9. 组织需求与编排器

### 9.1 创建需求

```http
POST /api/orgs/{org_id}/demands
{
  "title": "统一升级 3 个仓库的 FastAPI 到 0.115",
  "description": "...",
  "targets": [{"repo_key": "gitcode:a/x"}, {"repo_key": "gitcode:a/y"}],
  "context": { "knowledge_ids": [12, 13] },
  "preset": "default"        // 或指定 scene_id
}
```

服务端：

```text
1. 鉴权：require_org_member；org_hub 必须 ready
2. 建组织仓库真实 Issue：_post_gitcode_issue / _post_github_issue（底层，不触发流水线）
3. ensure_issue_task(org_repo_key, issue_num, ...) 建档
4. 写 issue.is_org_demand=1，issue.scene_id=组织场景，issue.org_demand_id
5. 生成上下文快照 org_context_snapshot（§10）
6. 插 org_demand(status='queued', issue_number, scene_id, targets_json, snapshot_id)
7. 返回 {demand_id, issue_number, status}
```

> 用底层建单函数而不是 `create_task_via_issue`，是为了避免组织需求立刻被普通 `issue_opened → vod` 路由抢跑。即便有 webhook 回流，也由 §D3 的 `is_org_demand` 守卫拦住。

### 9.2 编排器

**位置**：worker 进程（`grape_worker_server.py` lifespan 启动），与 `scheduler_loop` 同进程。
**节奏**：10s tick；`run_with_distributed_lock("org_pipeline_runner", ttl=30)`。
**幂等**：所有状态迁移用 `UPDATE ... WHERE status=?` 的 CAS 语义，先抢状态再执行。

```python
async def org_runner_loop():
    while True:
        async with run_with_distributed_lock("org_pipeline_runner", ttl=30):
            await provision_pending_hubs()          # §6 兜底重试
            for demand in pick_runnable_demands(limit=20):   # status=running/queued
                try:
                    await advance_demand(demand)
                except Exception as e:
                    mark_demand_error(demand, e)
            await sync_dispatch_items()              # 子单终态回传
        await asyncio.sleep(10)
```

`advance_demand`：

```python
cfg = get_pipeline_config(demand.scene_id, repo_key=demand.org_repo_key)
node = get_node(cfg, demand.current_node_id or first_node)
kind = node.get("kind", "agent")

if kind == "agent":
    run = get_or_create_run(demand, node)       # UNIQUE(demand,node,attempt)
    if run.status == "pending":
        seq = append_event(repo_key=org_repo, event_type="org_node_dispatch",
                           issue_id=f"#{demand.issue_number}", agent_type=node.tag, scene_id=demand.scene_id)
        _enqueue_pipeline_task(repo_key=org_repo, seq=seq, agent_type=node.tag,
                               skill_name=node.skill, skill_id=node.skill_id,
                               evt=build_evt(demand, node), issue_id=f"#{demand.issue_number}",
                               org_demand_id=demand.id, org_node_run_id=run.id)
        run.status = "running"
    elif run.status == "running":
        task = get_task(run.task_id)
        if task.status in TERMINAL:            # completed/failed/...
            if task.status == "completed":
                run.output_json = parse_agent_output(task.result_text, node)
                run.status = "succeeded"
                advance_to_next(demand, node)
            else:
                retry_or_fail(run, task)
    return

if kind == "dispatch":
    batch = get_or_create_batch(demand, run)
    plan = resolve_plan(demand, node, run)      # plan/manual/all_members
    create_items_and_dispatch(batch, plan)      # §10
    run.status = "waiting"
    return

if kind == "gate":
    demand.status = "pending_approval"
    return
```

### 9.3 节点推进

- Runner 只按 `next` 推进；`next == 'done'` → `demand.status = succeeded/partial_failed`。
- `accept` 节点配置 `wait_for_batch=true`：进入该节点前先确认批次终态；未终态则本轮跳过。
- Agent 节点 `handoff='server'` 时，提示词追加约束：“完成后不要 A2A 交办，结果由平台解析”。同时在 `a2a_endpoint` 对 `is_org_demand=1` 的 Issue 拒绝非平台发起的 A2A（防 Agent 自行跳节点）。

### 9.4 状态机

```text
queued ──start──▶ running ──agent ok──▶ running(下一节点) ──done──▶ succeeded
                    │                                        └────▶ partial_failed
                    ├──gate──▶ pending_approval ──approve──▶ running
                    │                          └─reject──▶ cancelled
                    └──节点重试耗尽──▶ failed
任意 running/cancelled ◀──用户取消── running
```

---

## 10. 跨仓下派

### 10.1 下派计划契约（`org_plan_v1`）

intake Agent 输出必须是结构化 JSON（放在结果末尾代码块，服务端解析，解析失败则整节点失败重试）：

```json
{
  "summary": "统一升级 FastAPI 依赖",
  "items": [
    { "repo_key": "gitcode:a/x", "scene_id": "custom-xxx", "task": "升级 pyproject 中 fastapi 到 0.115，跑测试", "done_when": "pr_merged" },
    { "repo_key": "gitcode:a/y", "task": "同步 lock 文件", "done_when": "issue_done" }
  ],
  "notes": "..."
}
```

服务端校验：
- 每个 `repo_key` 必须命中 `org_repo_link`（`dispatch_enabled=1`）且 `managed_repos.org_id == org_id`；
- `max_items` 上限；
- `scene_id` 必须存在于目标仓库（否则按 `scene_policy` 降级并记录 warning）；
- 去重（同一 plan 重复解析不重复建单）。

### 10.2 场景解析优先级（`scene_policy`）

1. item 显式 `scene_id`（`explicit` 策略下缺失即失败）；
2. `org_repo_link.default_scene_id`；
3. `managed_repos.dev_scene`；
4. 目标仓库唯一启用场景 → 用它；
5. 以上皆无 → 该 item 失败，错误“目标仓库未配置场景”，**不静默回退 default**。

> 组织场景绝不能作为子仓场景，必须显式排除 `scene_scope='org'`。

### 10.3 子 Issue 创建

逐 item：

```python
owner, name = split_owner_repo(target_repo_key)
token = get_gitcode_token(repo_key=target_repo_key) or get_github_token(...)
internal_user = {"id": demand.created_by_user_id, "username": owner,
                 "token": token, "auth_type": "internal"}
desc = render_dispatch_description(item, snapshot, demand)   # 组织上下文摘要 + 任务 + 回链
result = await create_task_via_issue(
    TaskCreateBody(repo_key=target_repo_key, title=f"[组织需求 #{demand.issue_number}] {item.task[:60]}",
                   description=desc, scene_id=item.scene_id),
    user=internal_user)
```

随后：

```sql
UPDATE issue SET org_demand_id=?, org_dispatch_item_id=?, carried_context=?
 WHERE repo_key=? AND issue_number=?;
UPDATE org_dispatch_item SET child_issue_number=?, child_task_id=?, status='created';
```

- `create_task_via_issue` 会同步触发目标仓库 `issue_opened` → 目标场景首节点开跑（复用现有全部能力）。
- 子 Issue 标题带组织需求号，便于人肉追溯；描述末尾带组织需求链接与快照摘要。
- 并发：`max_concurrency` 用信号量控制；失败不阻断其他 item（`continue`）。

### 10.4 子单终态回传

`sync_dispatch_items()` 每轮：

| `done_when` | 判定 |
|---|---|
| `task_terminal` | 子 Issue 最新 `agent_tasks.status ∈ {completed, failed, timeout_failed, cancelled}` |
| `issue_done` | `issue.status='completed'` 或 `issue_state.stage='done'` |
| `pr_merged` | 存在 `pull_request_merged` 事件（或 `issue.status=completed` 且 PR 已合并） |

状态写回 `org_dispatch_item`，并汇总 `org_dispatch_batch`（`total/succeeded/failed`）。全部终态后：

- `on_item_failure=continue`：批次 `partial_failed`，demand 允许进入 `accept`（验收 Agent 会看到失败清单）；
- `abort`：批次 `failed`，demand `failed`。

### 10.5 为什么不用 A2A 直连

现有 `_cross_pipeline_handoff` 校验 `get_pipeline_config(target_scene_id, repo_key=repo_key)`，`repo_key` 不变；`a2a_endpoint` 还会校验目标仓库 `managed_repos` 与 Issue 存在性。跨仓“凭空”A2A 会同时踩到这两处，且目标仓库没有 Issue 台账，后续无法跟踪。所以采用“建子 Issue”的路径。

---

## 11. 组织上下文与知识库

### 11.1 五层上下文

| 层 | 内容 | 来源/存放 | 下派时 |
|---|---|---|---|
| L0 组织硬规则 | 安全、合规、跨仓接口、强制技术标准 | 组织仓库 `org-context/rules.md` | 总是注入，量很小，不可被仓库规则覆盖 |
| L1 组织上下文 | 组织简介、架构原则、规范、术语、仓库目录 | 组织仓库 `org-context/` | 按需求标签/目标仓库选取 |
| L2 仓库上下文 | `AGENTS.md`、project-info、仓库知识、场景、代码 | 目标仓库自身 | 只注入目标仓库，不注入其他仓库 |
| L3 任务上下文 | 需求摘要、验收标准、下派计划、子任务 brief | 组织需求 + 平台快照 | 每次下派生成，随子任务下发 |
| L4 执行上下文 | 日志、工具结果、对话、测试输出 | 子仓/平台存储 | 不回传正文，父层只收摘要和链接 |

组织上下文和仓库上下文是叠加关系：

- 组织上下文回答“为什么做、跨仓约束是什么”；
- 仓库上下文回答“这个仓库怎么改、怎么测、怎么发布”；
- 任务上下文回答“这次具体做什么、验收标准是什么”；
- 执行上下文只服务本次执行，不回灌组织仓库。

### 11.2 组织仓库目录

```text
org-context/
├── rules.md              # L0 组织硬规则（权威，优先级最高）
├── profile.md            # L1 组织简介
├── architecture.md       # L1 组织架构与原则
├── standards/            # L1 组织规范
├── repos/                # L2 仓库目录（由关联记录自动生成，放摘要/索引）
│   └── <provider>/<owner>/<repo>/context.md
├── knowledge/            # 组织知识（active）
├── demands/              # L3 需求上下文
│   └── <需求号>/
│       ├── brief.md
│       ├── dispatch-plan.md
│       └── acceptance.md
└── index.json            # 机器可读索引（路径/大小/哈希）
```

边界：

- `org-context/` 是组织级权威载体，`agent_shared_knowledge` 只做索引；
- 仓库级上下文仍放在各目标仓库的 `project-info/` / `.knowledge/`；
- `repos/` 只放目录条目和摘要，不复制完整仓库代码、project-info 正文；
- 执行日志、完整对话、构建产物、实时任务状态不写入组织仓库。

### 11.3 下派快照

下派时为一次需求/批次生成快照，包含：

- L0 `rules.md`（总是）；
- L1 `profile.md` / `architecture.md` / 相关 `standards/`；
- 目标仓库的 `repos/<provider>/<owner>/<repo>/context.md`；
- L3 `demands/<需求号>/brief.md` / `dispatch-plan.md` / `acceptance.md`；
- 与需求标签匹配的 Top-K 组织知识。

不包含：

- 其他仓库上下文；
- 全量组织知识库；
- 全量历史需求；
- 原始日志、工具输出、完整对话；
- 过期/废弃文档。

预算建议：

| 层 | 预算 |
|---|---|
| L0 组织硬规则 | 8KB |
| L1 组织上下文 | 32KB |
| L2 仓库上下文 | 64KB |
| L3 任务上下文 | 32KB |
| 组织知识 Top-K | 32KB |

快照正文放平台快照目录/对象存储，`org_context_snapshot` 只存：

- `snapshot_ref` / `storage_path`；
- `files_json` manifest（路径、大小、哈希、来源）；
- `content_hash` / `total_bytes` / 版本时间。

同一批次的所有子任务共用同一份快照，保证组织口径一致。

### 11.4 子任务注入

- 平台把快照物化到子任务工作区 `.org-context/`；
- 子任务不需要 clone 组织仓库，也不需要组织仓库 token；
- 目标仓库自己的 `AGENTS.md`、`project-info/`、`.knowledge/` 继续由现有链路注入；
- Prompt 声明：**组织硬规则 > 本次任务明确要求 > 目录规则 > 仓库规则 > 组织通用规范 > 组织知识**；
- 除组织硬规则外，越具体越优先；发现冲突先修文档，不靠模型猜。

### 11.5 治理、生命周期与防幻觉

```text
草稿 → 审核 → 生效 → 废弃 → 归档
```

- 一个主题只有一个权威文档；
- 组织上下文变更需要管理员评审；
- 仓库目录由关联记录自动生成；
- Agent 自动沉淀的知识先进入待审核（draft），不直接 active；
- 知识沉淀必须有证据：PR、测试结果、验收结论；
- 过期/废弃内容不参与检索和注入；
- 区分事实、规范、决策、经验、推测；Agent 必须引用来源。

### 11.6 执行记忆提炼（Task Memory）

组织上下文不能只记录「任务是否完成」。Agent 通过 `grape tasks complete --json`
已产出 `actions / conclusion / artifacts / next_step / issue_summary`，服务端在
聚合收口时按目标仓库提炼为高信号摘要：

- 同一子 Issue 按角色保留最新一条终态任务（`issue / coding / qa / sre`）：
  - `issue` 回答需求评级、范围与拆分依据；
  - `coding` 回答改了什么、产物/分支/PR；
  - `qa` 回答验证证据、审查结论与风险；
  - `sre` 回答部署结果与线上影响。
- 每张卡片固定输出：结论、关键动作、验证证据、产物、风险与后续；
  `Agent feedback` 只进入「跨仓可复用经验」，不混入关键动作。
- 字段按上限裁剪（结论 1200 字、单条 360 字、单角色最多 8 条），
  避免把执行日志塞回上下文；上下文是有限注意力预算。
- `org_context.decision` 写入批次结论 + 目标执行摘要 + 跨仓可复用经验；
  有真实执行信息时额外沉淀一条 `agent_shared_knowledge(kind=retrospective)`
  「下派 #N 经验沉淀」，供后续检索/注入。
- 项目级 `get_issue_summary()`：仅**团队组织关联仓库**（`managed_repos.org_id`
  指向 `is_personal=0` 的 organization）优先读取 `agent_tasks.result_text`
  的结构化产出；普通仓库/个人仓库保持原有 `events.result` 摘要逻辑不变。

### 11.7 容量与保留策略（防上下文爆炸）

组织运行越久，下派批次与经验条目会持续增长。上下文不能按「全部历史」注入或镜像，
按热/温/冷三层收敛：

| 层 | 载体 | 行为 |
|---|---|---|
| 热 | 最近 N 条 active decision / retrospective knowledge | 进入 UI、组织仓库镜像、注入候选 |
| 温 | `dispatch-archive` decision | 旧批次压缩成一行结论 + 去重可复用经验 |
| 冷 | deprecated decision / archived knowledge | 保留在 DB 可查，不进入默认注入/镜像 |

- 触发：每次下派结论落库后调用 `compact_org_memory()`，幂等；
- 默认保留：`ORG_CONTEXT_ACTIVE_DECISIONS=30`、
  `ORG_CONTEXT_ACTIVE_RETROSPECTIVES=30`；
- 归档窗口：`ORG_CONTEXT_ARCHIVE_ENTRIES=200`、
  `ORG_CONTEXT_ARCHIVE_MAX_BYTES=32768`；
- 单条 conclusion：`ORG_DECISION_MAX_BYTES=65536`；
- 仓库镜像硬上限：`ORG_CONTEXT_REPO_MAX_FILES=400`、
  `ORG_CONTEXT_REPO_MAX_BYTES=2MB`，超限写 `org-context/TRUNCATED.md`；
- 列表 API：`limit` 默认 200、上限 500，禁止一次拉全量；
- 项目级 prompt 摘要：`max_issues=10` + 每 Issue 3 个角色 + 字段上限，天然有界；
- `upsert` 走 key/name 精确查询，不再随历史增长做全量扫描；
- 组织仓库 `demands/` 只生成正在执行或有 active decision 的需求；清理后的历史需求不再生成；
- 仓库目录摘要对 codegraph 未索引的 project-info 给占位说明，不写入大段
  “No relevant code found” 噪声。

### 11.8 当前实现状态（2026-11-16）

- 已落地：组织仓库 `org-context/` 分层镜像（rules/profile/architecture/standards/
  repos/knowledge/demands）、`org_context_snapshot` 表、快照生成与 CLI 读取接口；
- 已落地：Agent 自动 retrospective 先写 draft，避免未审核知识直接生效；
- 已落地：下派子 Issue 记录 snapshot_id，`collect_knowledge_files` 合并
  `org-context/**`，worker/direct 物化到 `.org-context/`；
- 已落地：子 Issue 同时给出 `grape org dispatches context` 按需读取命令；
- 待补：accept 节点 `## 组织知识沉淀` 解析回写；
- 待补：组织知识标签/相似度 Top-K 检索（当前按稳定顺序 + 预算截断）。
