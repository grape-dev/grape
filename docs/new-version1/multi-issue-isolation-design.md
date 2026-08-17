# 多 Issue 并行 + 全局共享设计（Issue-level Session/Workdir Isolation + Shared Project Info）

> 版本：v0.2（设计稿，M0/M1/M2 已实施）
> 日期：2026-08-09
> 状态：待评审
> 前置：`agent-runtime-event-full-design.md` / `concurrency-design.md` / `vod-skill-upgrade-plan.md` / `skill-delivery-remote.md`
> 说明：本文只做**需求分析与设计思路输出**。覆盖三个关联需求：
> 1. 不同 Agent 部署在不同虚机时，project-info 如何全局共享；
> 2. 每个 DevOps Agent 从「repo 级会话/工作目录隔离」升级为「Issue 级会话/工作目录隔离」，支持多 Issue 并行；
> 3. 明确上下文分层：Agent 从 Grape 服务端拿到 Issue 级 event.json 后，**repo 全局信息就只剩 project-info**。
> 变更记录：
> - v0.2（2026-08-09）：**M0/M1/M2 已实施**（project-info 随任务交付、issue_agent_session、claim 串行粒度切换）；§3.4.1 按用户拍板细化 M3 git 基建（bare mirror + per-Issue worktree + worker 懒建立）；§3.4.2 明确 sweeper 只等 `stage=done`；§四 增前端 B（Pipeline 复用 + 执行徽标）；§六/§七 同步。

---

## 〇、需求与结论摘要（TL;DR）

| # | 需求 | 现状 | 设计结论 |
|---|------|------|---------|
| R1 | project-info 全局共享（跨 VM/Agent） | 各 Agent 在各执行机本地 `data/workspaces/{owner}/{repo}/project-info/` 各自生成，`data/` 被 gitignore，多机不共享、口径不一 | **服务端权威存储 + 生成职责上收 + 随任务打包交付**（复用 skill_files 机制），Agent 不再本地生成 |
| R2 | Issue 级 session + workdir 隔离 | session 按 `(repo_key, agent_type)` 共享（`devops_agent_session` + `runtime_session_id`）；cwd = `WORKSPACE_DIR/{owner}/{repo}/{agent_type}-agent` 全 Issue 共享；D9 串行粒度 = repo+agent → 同 repo 多 Issue 无法并行 | **配置层/执行层拆分**：配置仍 repo 级，新增 Issue 级执行会话 `issue_agent_session`；workdir 改为 `{owner}/{repo}/{agent_type}/issues/{n}/`；D9 串行粒度降到 Issue 级，同 Issue 串行、跨 Issue 并行 |
| R3 | 上下文分层：repo 全局信息只剩 project-info | 本地模式下历史文件（`WORKSPACE_DIR/history/`）与 project-info 均以**本地绝对路径**注入，远端 worker 不可读 | **三级上下文**：repo 全局（仅 project-info）+ Issue 级（event.json / issue_state / 评论）+ 事件瞬态（payload）；一切本地路径注入改为「随任务打包」或「文本内联」 |

**核心矛盾点**：本地模式（`direct`，`queue_local` 已废除）下 Agent 与 Bot 同机，可以直读服务端 `data/workspaces/`；远端模式（`queue_remote`）下 worker 与 Bot 异机，`cwd` 只是字符串，worker 上并不存在服务端的数据文件。因此**一切 repo/Issue 级知识（project-info、event.json、历史）都必须走"服务端构造 → 随任务交付 / 文本内联"，而不是依赖 worker 本地文件系统**。

---

## 一、现状基线核对（2026-08-09 代码实测）

### 1.1 会话模型（repo 级）

| 项 | 现状 | 位置 |
|---|---|---|
| Agent 会话表 | `devops_agent_session`，**唯一键 `(repo_key, agent_type)`**，含 runtime_id / skill / repo_gitcode_token / IM 配置 / enable | `src/api/db_setup.py:45` |
| 会话获取 | `get_or_create_agent_session(repo_key, agent_type)` 按 `(repo_key, agent_type)` 查/建 | `src/api/session_store.py:84` |
| 会话续接指针 | `runtime_session_id`（= opencode `--session` 值）也按 `(repo_key, agent_type)` 存储/更新 | `src/api/event_pipeline.py:621` / `src/api/session_store.py:142` |
| 串行语义（D9） | `agent_tasks.agent_session_id` 为同一 `devops_agent_session.id` → **同 repo + 同 agent 的所有 Issue 严格串行**（同一时刻最多 1 个活跃 task） | `src/runtime/service.py:155-187`（claim 的 `NOT EXISTS` 活跃 session） |

### 1.2 工作目录（repo 级）

| 场景 | cwd | 位置 |
|---|---|---|
| 事件管线（direct） | `WORKSPACE_DIR/{repository}/{agent_type}-agent` | `src/api/event_pipeline.py:518` |
| 事件管线（queue_*） | 同上，作为 `agent_tasks.cwd` | `src/api/event_pipeline.py:448` |
| 飞书 Chat | `WORKSPACE_DIR/feishu/{repository}_{sess}` | `src/im/handler_async.py:451` |
| VOD | `WORKSPACE_DIR/vod/{repo_key 清洗}` | `src/im/vod_handler_async.py:201` |
| Chat（WebUI） | `WORKSPACE_DIR/chat/{user_id}/{conv_id}` | `src/api/routes/chat.py:8` |

> 现状 cwd 目录**内容为空**：仓库代码并未落地（coding 技能的 `git checkout -b` / `git push` 依赖的 checkout 机制缺失，属本设计配套缺口）；opencode 的会话状态文件（`--session` + `.opencode/` 等）落在 cwd 下。

### 1.3 知识文件（均服务端本地文件，`data/` 整体 gitignore）

| 文件 | 路径 | 归属 |
|---|---|---|
| event.json（Issue 档案） | `WORKSPACE_DIR/{repo_key}/issues/{n}/event.json` | Issue 级 |
| project-info | `WORKSPACE_DIR/{owner}/{repo}/project-info/{project-graph,project-capabilities}.md + .metadata.json` | **repo 全局** |
| 会话历史 | `WORKSPACE_DIR/history/{account}/{repo_name}/session_history*.json` | repo 级（按 账号+仓库 分目录） |

> **会话历史的来源与用途**（避免误解）：它是**飞书 Chat 的"记忆文件"，非事件管线产物**。写入方为 `src/im/handler_async.py:474-486`——每次飞书消息经 Agent 处理后把一条记录（type/success/exit_code/actor/account/skill/stdout/oc_session_id）append 进历史，再 `dump_history_to_file()`（`src/session_manager.py:249`）落盘为 JSON 分片（最新 10 条在 `session_history.json`，更早的在 `session_history_N.json`）。读取方为 `_build_history_file_guide`（`src/prompt_builder.py:55-69`）——构建后续 prompt 时把文件**绝对路径**列给 Agent，指示用 read tool 读取，作为"过去发生了什么"的 repo 级历史上下文。

### 1.4 知识注入方式（现状：本地绝对路径为主）

| 注入项 | 方式 | 位置 |
|---|---|---|
| 历史文件指引 | prompt 写入 `WORKSPACE_DIR/history/...` **绝对路径** | `src/prompt_builder.py:60` |
| project-info | VOD：服务端读文件全文内联进 prompt | `src/im/vod_handler_async.py:118-127` |
| project-info | issue-advance：**技能内让 Agent 用 read tool 直读本地路径** | `skills/issue-advance/SKILL.md:93` |
| event.json 富上下文 | `build_issue_context()` 服务端内联 | `src/api/event_archive.py:228` |
| 技能文件 | `agent_tasks.skill_files` 随任务打包 + `{{SKILL_DIR}}` 占位符替换 | `src/runtime/service.py:111-152` / `skill-delivery-remote.md` |
| A2A 地址 | `{{SERVER_URL}}` 占位符 | `src/runtime/service.py:316` |

### 1.5 关键结论（现状三个断点）

1. **project-info 无全局权威**：谁执行谁生成，多 VM 各自一份，且远端 worker 生成的写回不了服务端 → 口径漂移、重复成本。
2. **repo 级隔离 = 无法多 Issue 并行**：`devops_agent_session` 一把串行锁 + 共享 cwd（含 opencode 会话状态），同 repo 不同 Issue 互相阻塞/污染。
3. **本地路径注入在远端失效**：`history/`、`project-info/` 绝对路径在 worker 上不存在（见 `skill-delivery-remote.md` §1.2 已列举同类问题，本项目录扩展到 project-info/history）。

---

## 二、需求 1：project-info 全局共享（跨 VM / 跨 Agent）

### 2.1 现状问题

- **每机自建**：issue-advance 与 VOD 均按 `codegraph-project-analysis.md` 在**执行机本地**生成 project-info；多 VM 部署后每台 worker 一份，内容可能不一致。
- **写回无门**：远端 worker 生成的文档落在 worker 的 `data/workspaces/`，服务端与其它 worker 不可见。
- **重复生成**：同一 repo 不同 Agent 不同 Issue 反复触发 codegraph 分析（代价高：`codegraph init` 1-3 分钟）。
- **口径依赖生成者**：谁生成、用什么分支、什么时间点生成，都不可控。

### 2.2 设计目标

1. **单一权威**：project-info 只有一份，存在服务端（全 Grape 集群可见）。
2. **一次生成、全局复用**：避免每个 Agent/每台 worker 重复分析。
3. **随任务可达**：远端 worker 无服务端文件系统也能拿到（打包或 API）。
4. **可更新**：以 commit sha / 7 天为节拍，服务端统一刷新，无需各 Agent 各自判断。

### 2.3 方案对比

| 方案 | 机制 | 优点 | 缺点 |
|---|---|---|---|
| A 服务端文件系统权威 + 随任务打包 | 服务端保存唯一副本；`enqueue_task` 时打包进 `project_info_files` 列（与 skill_files 同机制），worker 落地 | 复用成熟机制；零新增接口；每任务拿最新快照 | 每次任务重复传输（可用 content_hash 缓存抵消）；生成仍需有人触发 |
| B 服务端文件系统权威 + API 拉取 | `GET /api/repos/{key}/project-info`，Agent 用 curl 拉取 | 灵活、按需、不占任务包 | 新增接口 + 认证；Agent 多一次外部调用；与「禁止探索 Grape 内部」边界需澄清 |
| C DB 表存文档 | `project_info` 表存全文 + 元数据 | 结构化管理、审计 | 大文本进 SQLite 不划算；迁移/备份复杂 |
| **D 混合（推荐）** | **A 为主**：服务端权威文件 + enqueue 打包注入 prompt 侧 + 可选 B 接口供按需 | 三 scope 统一；prompt 内联最省 Agent 动作；保留 API 兜底 | 需要新增列 + 生成职责上收 |

### 2.4 推荐方案 D 设计

```
[服务端] 权威 project-info
  data/workspaces/{owner}/{repo}/project-info/{project-graph,project-capabilities}.md + .metadata.json
        │
        ├─(1) 生成/刷新：由「服务端指定 runtime」统一执行（见 2.6）
        ├─(2) enqueue 时：读文件 → 注入 agent_tasks 新列 project_info_files
        │      worker 落地 {cwd}/.project-info/ + prompt 内联摘要
        └─(3) 可选：GET /api/repos/{key}/project-info 供按需拉取（M2+）
```

**执行侧（worker `_run_task` 前置，扩展 skill-delivery-remote.md §3.5）：**

```python
# 1) 落地 project-info（内容随任务，无服务端文件依赖）
files = json.loads(task.get("project_info_files") or "{}")   # {"project-graph.md": "...", "project-capabilities.md": "..."}
for rel, content in files.items():
    dest = os.path.join(cwd, ".project-info", rel)
    _materialize(content, dest)                                # 复用 content_hash 缓存
# 2) prompt 已含服务端内联摘要（见 §四），落地副本供 Agent read tool 使用
```

### 2.5 数据模型

`agent_tasks` 新增列（与 skill_files 同构）：

```sql
ALTER TABLE agent_tasks ADD COLUMN project_info_files TEXT DEFAULT '{}';  -- JSON: {rel_path: content}
ALTER TABLE agent_tasks ADD COLUMN project_info_meta  TEXT DEFAULT '{}';  -- JSON: {last_commit_sha, branches, last_updated}
```

- `project_info_meta` 供 prompt 声明「基线时效/降级标注」，Agent 无需自行探测。
- 无 project-info（未生成或生成失败）→ `project_info_files='{}'`，prompt 提示「无知识基线，允许降级分析」。

### 2.6 生成与刷新策略（职责上收）

- **生成主体**：不再由各执行机上的 issue-advance / VOD 各自生成，改为**服务端统一调度**：
  - 首选：服务端新增「project-info builder」轻量 Agent（指定一个固定 runtime，按 repo 排程），或
  - 复用现有 `repo-onboarding`/`issue-advance` 技能，但**只在指定 runtime 上跑、结果写回服务端**。
- **触发条件**（沿用 `codegraph-project-analysis.md` §增量更新策略）：
  - 文档不存在 → 生成；
  - main 分支 HEAD commit 变化 → 重建；
  - 距上次更新 > 7 天 → 重建；
  - 否则复用（enqueue 时读取即可，不做每任务校验）。
- **降级**：codegraph 不可用 → README + GitCode API 基础信息，`.metadata.json` 标注 `[degraded]`，随 `project_info_meta` 传递。
- **并发写保护**：`event_archive.py` 已有 `_lock_for(repo_key)` 模式，project-info 写入复用同一把 repo 锁 + 原子替换。

### 2.7 对技能的影响（待技能文件同步改）

| 技能 | 现状 | 改动 |
|---|---|---|
| `skills/issue-advance/SKILL.md:93` | read tool 直读 `data/workspaces/{owner}/{repo}/project-info/` | 改读 `{cwd}/.project-info/`（或 prompt 内联摘要），**不再自行生成** |
| `skills/vod/SKILL.md:37` | 同上 | 同上；`codegraph-project-analysis.md` 引用改为「仅在服务端 builder 场景使用」 |
| `references/codegraph-project-analysis.md` | 各 Agent 本地执行 | 迁移为「服务端 builder 执行手册」，或保留为降级参考 |

---

## 三、需求 2：Issue 级 session 与 workdir 隔离

### 3.1 现状问题（为什么 repo 级不够）

1. **串行瓶颈**：D9 按 `agent_session_id`（= repo+agent）串行，同 repo 多 Issue 只能排队 → 无法并行，与多 runtime worker 的扩容目标冲突。
2. **上下文污染**：`runtime_session_id` 是 repo+agent 共享的 opencode 会话续接指针，A Issue 的对话历史会被 B Issue 延续读取 → 上下文错乱。
3. **工作目录冲突**：cwd 共享，opencode 会话状态（`--session` 相关文件）落同一目录；coding 技能的 `git checkout -b feat/issue-{n}` / `git add -A` 在同一 checkout 上执行 → 分支/暂存区互相踩踏。
4. **生命周期对不上**：Issue 级状态（`issue_state`）已经是 per-Issue，而执行会话还是 per-repo → 语义不匹配。

### 3.2 隔离粒度

| 维度 | 现状 | 目标 |
|---|---|---|
| 会话上下文（runtime_session_id） | `(repo, agent)` 共享 | `(repo, agent, issue)` 独立 |
| 工作目录（cwd） | `{owner}/{repo}/{agent_type}-agent` | `{owner}/{repo}/{agent_type}/issues/{issue_n}/` |
| 串行锁（D9） | `agent_session_id`（repo+agent） | `issue_session_id`（repo+agent+issue） |
| 配置（runtime/skill/token/IM/enable） | repo+agent | **不变**（配置不属于某个 Issue，仍 repo+agent） |

> 核心原则：**配置与执行分离**。`devops_agent_session` 是"这台 repo 的这个 Agent 配了什么"；新增执行会话是"这个 Issue 的这一段处理上下文"。这样既保留管理端 repo 级配置，又让执行按 Issue 并行。

### 3.3 数据模型（配置层 / 执行层拆分）

**执行层新增表 `issue_agent_session`**（per Issue 独立会话续接指针）：

```sql
CREATE TABLE IF NOT EXISTS issue_agent_session (
    id                TEXT PRIMARY KEY,          -- uuid，作为 agent_tasks.issue_session_id 外键
    repo_key          TEXT NOT NULL,             -- 冗余，便于检索
    agent_type        TEXT NOT NULL,             -- 冗余
    issue_id          TEXT NOT NULL,             -- 如 "#5"（对应 event.json 的 issue_num）
    runtime_session_id TEXT DEFAULT '',          -- opencode --session 续接指针（Issue 级）
    status            TEXT DEFAULT 'idle',       -- idle / running / failed
    last_active_at    TEXT DEFAULT (datetime('now')),
    created_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (repo_key, agent_type, issue_id)      -- 一个 Issue 在同一 Agent 下唯一会话
)
```

**配置层 `devops_agent_session` 保持 repo+agent 不动**：runtime_id / skill / skill_id / repo_gitcode_token / IM / enable 继续承载管理端配置；`runtime_session_id` 列**废弃使用**（数据迁移到 issue_agent_session 或在读侧回退），保留列兼容。

**`agent_tasks` 新增/调整**：

```sql
ALTER TABLE agent_tasks ADD COLUMN issue_session_id TEXT DEFAULT '';  -- 串行粒度切换为 Issue 级
-- 语义：agent_session_id（repo+agent 配置，快照）保留；串行判断改用 issue_session_id
```

**串行语义（D9 变更）**：claim 的 `NOT EXISTS` 子查询从"同 `agent_session_id`"改为"同 `issue_session_id`"：

```sql
-- 同 Issue 严格串行；不同 Issue（不同 issue_session_id）并行
WHERE issue_session_id = ?
  AND NOT EXISTS (
    SELECT 1 FROM agent_tasks a2
    WHERE a2.issue_session_id = agent_tasks.issue_session_id
      AND a2.status IN ('dispatched','running'))
```

> 兼容回退：`issue_session_id=''`（旧任务/未关联 Issue 的 push 事件）时，退化为按 `agent_session_id` 串行（现状语义），保证迁移期安全。

### 3.4 工作目录布局

```
data/workspaces/{owner}/{repo}/
├── project-info/                    # repo 全局（服务端权威，见 §二）
├── issues/{issue_n}/event.json      # Issue 档案（现有，不变）
├── codebase/                        # ★ repo 只读代码基线（共享，见 3.4.1）
└── {agent_type}/
    └── issues/{issue_n}/            # ★ Issue 级工作目录（新）
        ├── .skills/                 # 技能包落地（现有逻辑，随 cwd 下移）
        ├── .project-info/           # project-info 落地（§2.4）
        └── ...                      # opencode 会话状态 / git 工作副本（如需）
```

**3.4.1 共享只读代码基线（coding/qa/sre，M3，用户已拍板：git 命令工作模型 X）**

设计采用 **「共享 bare mirror 基线 + Issue 级 git worktree」**（多 Issue 并行互不踩踏，且只用一份对象库）：

```
{WORKSPACE_DIR}/{owner}/{repo}/
├── codebase/                      # ★ 共享只读基线（bare mirror，1 份/仓库/机器）
└── coding/
    └── issues/
        ├── 5/                     # worktree：分支 feat/issue-5，独立工作树+暂存区
        ├── 6/                     # worktree：分支 feat/issue-6
        └── 7/
```

| 项 | 决策 |
|---|---|
| 基线形态 | **bare mirror**（`git clone --bare --mirror`）：只读、省空间、无"主 checkout 分支被 worktree 占用"冲突 |
| 建立方式 | **各 worker 懒建立**（每机一份缓存，`fetch --prune` 刷新）——代码是只读缓存而非权威数据，不需要像 project-info 那样服务端唯一权威 |
| worktree | `git worktree add {issue_dir} -b feat/issue-{n} <base_head>`；issue 级 cwd 即 worktree 工作树，`new-dev-flow.md` 的 `git checkout -b/commit/push` 直接可用 |
| git 鉴权 | clone 与 push 均用 `git -c http.extraheader="Authorization: Basic ..."` **subprocess 注入**，不写 config、不进日志、不入 ps 参数；token 用完即弃、随目录清理零残留 |
| 跨平台复用 | host 由 `parse_repo_key` 的 provider 映射（gitcode→`gitcode.com`、github→`github.com`），未来扩 GitHub 只加映射，结构不变 |
| 接入点 | `service._run_task`（本地 worker）与 `agent.py::_execute_task`（远端边端）在 `stream_runtime` 前 `ensure_repo_base + ensure_issue_worktree`（仅 Issue 级任务且 agent_type ∈ coding/qa/sre）；失败仅 `log.warning` 不阻断（agent 仍可走 API 只读） |
| 无新任务列 | 复用现有 `repo_key` + `repo_gitcode_token` |

**3.4.2 清理策略（sweeper，M3）**

- **触发条件：仅 `issue_state.stage='done'`（终态）**，绝不清理 vod/issue/coding/qa/sre 进行中的 workdir（承载该 Issue 会话状态）。
- 动作：`git worktree remove --force` + 删 `{agent_type}/issues/{issue_n}/` 目录。
- 保留：`event.json` 档案（在 `WORKSPACE_DIR/{repo_key}/issues/` 独立路径，天然保留）；`issue_agent_session` DB 行（审计用，很小）。
- 周期：服务端 lifespan 后台循环，间隔 ~15min，对齐 runtime GC；对"超 7 天未活跃的僵尸 workdir"兜底清理。
- 跳过：对应 `issue_agent_session` 存在 dispatched/running 任务的会话不清理。

### 3.5 执行流改动

```
触发点（event_pipeline._enqueue_pipeline_task）：
  1. 配置会话：get_or_create_agent_session(repo, agent)   → devops_agent_session.id（配置快照，沿用）
  2. 执行会话：get_or_create_issue_session(repo, agent, issue_id)
     → issue_agent_session.id + runtime_session_id（Issue 级续接）
  3. cwd = WORKSPACE_DIR/{owner}/{repo}/{agent_type}/issues/{issue_n}/（makedirs）
  4. enqueue_task(..., agent_session_id=配置id, issue_session_id=执行id, cwd=..., runtime_session_id=Issue级)
  5. 完成时：update_issue_session(issue_session_id, new_runtime_session_id)  ← 续接指针写回 Issue 级
```

**并发效果**：`issue #5` 与 `issue #6` 在同一个 worker 上可并行（不同 `issue_session_id`）；同 Issue 的评论/更新仍串行（同 `issue_session_id`）。配合 `RUNTIME_MAX_CONCURRENT_TASKS=20` 执行槽，水平扩展成立。

### 3.6 对现有代码的影响面（实现期清单）

| 文件 | 改动 |
|---|---|
| `src/api/db_setup.py` | 新增 `issue_agent_session` 表 + `agent_tasks.issue_session_id` 列 |
| `src/api/session_store.py` | 新增 `get_or_create_issue_session` / `update_issue_session` / `list_issue_sessions` |
| `src/api/event_pipeline.py` | `_enqueue_pipeline_task`/`_execute_agent`：cwd 改 Issue 级 + issue_session 接入 |
| `src/runtime/service.py` | `enqueue_task` 增 `issue_session_id` 参数；claim 串行子查询切换 |
| `src/im/vod_handler_async.py` / `handler_async.py` | 对应 workdir 调整（vod/chat 非 Issue 场景可保持现状或跟随） |
| `src/prompt_builder.py` | 历史指引/上下文注入改造（见 §四） |

---

## 四、需求 3：上下文分层（repo 全局信息 = 仅 project-info）

### 4.1 分层模型

Agent 处理一个事件所需上下文分三级，**只有 repo 级是全局共享，其余全部 per-Issue/per-事件**：

| 层级 | 内容 | 来源 | 注入方式 |
|---|---|---|---|
| **repo 全局** | project-info（graph + capabilities + meta） | 服务端权威存储 | 随任务打包 `.project-info/` + prompt 内联摘要（§二） |
| **Issue 级** | event.json（`build_issue_context`）、`issue_state` 阶段/归属、Issue 评论、关联 PR | 服务端 DB / event.json | prompt 文本内联（服务端构建时） |
| **事件瞬态** | 本次事件 payload、actor、branch、summary | 事件本身 | prompt 文本内联 |

> 前提确认（用户给定）：Agent 能**从 Grape 服务端拿到 Issue 的 event.json**（`build_issue_context` 已内联；`GET /api/events/detail/{seq}` 已暴露档案摘要）。因此 Issue 级上下文无需本地文件，**repo 全局需要共享的就只剩 project-info** —— 这也是 §二 必须服务端化 project-info 的原因。

### 4.2 prompt 注入改造（`src/prompt_builder.py`）

| 注入项 | 现状 | 改造后 |
|---|---|---|
| 历史文件指引（`WORKSPACE_DIR/history/...`） | 飞书 Chat 会话记忆的**本地绝对路径**（`_build_history_file_guide`，非事件管线产物） | **移除本地路径**；Chat 历史改由对话上下文/event.json 档案承载（远端不可读本地） |
| project-info | VOD 特例内联；issue-advance read tool 直读 | **统一内联摘要**（`project-graph` + `project-capabilities`，`max_chars` 截断）+ `.project-info/` 落地副本供 read tool |
| event.json 富上下文 | `build_issue_context()` | 保留（已内联，无文件依赖） |
| `issue_state` 阶段 | VOD 特例内联 | 统一注入「当前 Issue 处于 X 阶段，归属 Y」 |

**Skill 文件路径占位符**：延续 `skill-delivery-remote.md` 占位符约定，新增 `{{PROJECT_INFO_DIR}}`（worker 替换为 `{cwd}/.project-info`），prompt 中一切 server-local 路径不再出现。

### 4.3 硬约束与安全边界

- **远端模式铁律**：Agent 不得假定 `data/workspaces/`、`history/` 等服务端路径在本机存在；一切知识来自 prompt 内联 / 随任务落地副本 / GitCode API。
- 维持「严禁探索 Grape 内部数据」：`.project-info/` 与 `.skills/` 是任务交付的**白名单目录**，其余 `data/` 下内容仍禁止。
- token 脱敏逻辑不变（写入前统一 `[REDACTED]`）。

### 4.4 前端 B：Pipeline 页复用 + 执行徽标（M3，用户已拍板）

> **结论：不新建"Issue 并行视图"页面**——Pipeline 页已是 per-Issue 视图（每行一 Issue、每 Issue 独立五阶段卡片、点击切换），多个 Issue 同时 In Progress 即直观体现并行。Runtime 页**不加** Issue 级业务视图（Runtime 是业务无关的执行底座，跨 repo/用户共享，不掺 Issue 状态）。

只做最小增强（复用 Pipeline 页）：

- **后端** `GET /api/pipeline/{owner}/{repo}`：按该 repo 查 `agent_tasks` status ∈ (dispatched, running)，**按 issue_id 派生** `executing: bool`（不维护 `issue_agent_session.status` 双份记账，避免状态漂移）。
- **前端** `pipeline.js`：历史表每行 + 激活卡片加"⚡ 执行中 / 空闲"徽标（复用现有 tg 样式）。

---

## 五、数据模型与 API 变更汇总

### 5.1 表/列变更

| 变更 | 内容 |
|---|---|
| 新增表 | `issue_agent_session`（§3.3） |
| `agent_tasks` + 列 | `issue_session_id`（串行粒度）、`project_info_files`、`project_info_meta` |
| `devops_agent_session` | 保留 repo+agent 配置；`runtime_session_id` 废弃使用（读侧回退/迁移） |
| `issue_state` | 不变（已是 Issue 级，唯一权威） |

### 5.2 新增/变更 API

| 端点 | 用途 | 里程碑 |
|---|---|---|
| `GET /api/repos/{key}/project-info` | 按需拉取权威 project-info（已实施） | M0 |
| `GET /api/pipeline/{owner}/{repo}` 扩展 | 返回体每个 Issue 附 `executing: bool`（前端 B，§4.4） | M3 |

### 5.3 依赖关系图（改造后）

```
event_pipeline（触发点三连）
  ├─ append_event → events 表
  ├─ init_event_json → {owner}/{repo}/issues/{n}/event.json
  └─ enqueue_task
       ├─ agent_session_id      → devops_agent_session.id（repo+agent 配置快照）
       ├─ issue_session_id      → issue_agent_session.id（repo+agent+issue，串行锁）
       ├─ runtime_session_id    → issue_agent_session.runtime_session_id（Issue 级续接）
       ├─ cwd                   → {owner}/{repo}/{agent_type}/issues/{issue_n}/
       ├─ project_info_files    → .project-info/ 落地（repo 全局共享）
       └─ skill_files           → .skills/ 落地（现有）
```

---

## 六、实施计划与里程碑

| 阶段 | 内容 | 验证 |
|---|---|---|
| **M0 project-info 服务端化** ✅ | `agent_tasks` 加 `project_info_files`/`project_info_meta`；服务端统一生成/刷新；enqueue 打包注入；worker 落地 `.project-info/`；prompt 内联摘要 + `{{PROJECT_INFO_DIR}}` 占位符；issue-advance/vod 技能改读注入 | 远端任务无服务端文件也能拿到基线；同一 repo 不重复生成 |
| **M1 Issue 级会话 + workdir** ✅ | `issue_agent_session` 表 + `agent_tasks.issue_session_id`；cwd 改 Issue 级；`update_issue_session` 写回 | 两 Issue 同 worker 并行；同 Issue 串行 |
| **M2 串行粒度切换** ✅ | claim 串行子查询按 `issue_session_id`（空值回退现状）；测试并发 claim | 并发多 Issue 无重复 dispatched、无互相阻塞 |
| **M3 配套**（进行中） | ① `src/runtime/repogit.py`：bare mirror 基线 + per-Issue worktree（§3.4.1）；② `src/runtime/cleanup.py` sweeper（§3.4.2）；③ 前端 B：Pipeline `executing` 徽标（§4.4）；`GET project-info` API 已完成 | coding 多 Issue 分支互不踩踏；done 后 workdir 清理 |
| **M4 迁移与回归** | 存量 `runtime_session_id` 读侧回退（已实施）；全量 pytest（已实施）；端到端（部署阶段） | 零回归 |

---

## 七、测试用例设计

> 约定：测试沿用现有 pytest 体系，`DEVOPS_DB_PATH` 指向独立临时库（见 `tests/conftest.py` / `test_session_manager.py:283` 的做法）；事件档案/工作目录用 `patch("src.config.WORKSPACE_DIR", tmp_path)` 隔离（见 `test_repos_remove_cleanup.py:143`）。新增文件与既有文件扩展并行，避免全量回归单点。

### 7.1 M0 project-info 服务端化 —— 新增 `tests/test_project_info.py`

| 用例 | 步骤 | 断言 |
|---|---|---|
| enqueue 打包权威基线 | 服务端 `data/workspaces/{owner}/{repo}/project-info/` 有 2 个 md → `enqueue_task` | `agent_tasks.project_info_files` 含 `project-graph.md`/`project-capabilities.md` 全文；`project_info_meta` 含 last_commit_sha |
| 无基线降级 | 目录不存在 → enqueue | `project_info_files='{}'`；prompt 内联摘要为「无知识基线，允许降级」 |
| 基线缺失部分文件 | 仅 project-graph.md 存在 | 打包只含存在的文件，不报错 |
| worker 落地 + 占位符 | `_run_task` 执行前 | `{cwd}/.project-info/project-graph.md` 生成；prompt 中 `{{PROJECT_INFO_DIR}}` 替换为 `{cwd}/.project-info` |
| 缓存命中 | 同 content_hash 第二次任务 | 不重复写盘（走缓存） |
| 内联摘要截断 | 基线 > max_chars | 摘要截断且含「已截断」标注；落地副本为全文 |
| 生成职责上收 | 同 repo 连续两任务、基线未过期 | 两次 enqueue 读同一份，不触发重新生成 |

### 7.2 M1/M2 Issue 级会话 + 串行粒度 —— 新增 `tests/test_issue_session.py` + 扩展 `tests/test_runtime.py`

`tests/test_issue_session.py`：

| 用例 | 断言 |
|---|---|
| 建表/幂等 | 重复建表不报错；`UNIQUE(repo_key, agent_type, issue_id)` 生效 |
| get_or_create 复用 | 同 `(repo, agent, issue)` 二次调用返回同一 id；不同 issue 返回不同 id |
| 续接指针隔离 | `update_issue_session(A, "ses_A")` 不影响 B 的 runtime_session_id |
| 空指针重建 | 记录 `runtime_session_id=''` 时新任务自动续新会话 |

`tests/test_runtime.py` 扩展：

| 用例 | 断言 |
|---|---|
| 同 Issue 串行 | `issue_session_id=X` 已有 running → claim 不返回 X 的 queued 任务 |
| 跨 Issue 并行 | X、Y 两个不同 `issue_session_id` 的 queued 任务 → 均可被 claim（无 `NOT EXISTS` 阻塞） |
| 空值回退 | `issue_session_id=''` 任务 → 按 `agent_session_id` 串行（现状语义） |
| 并发抢单 | N 线程并发 claim 不同 Issue → 无重复 dispatched、无 `database is locked` |
| 终态幂等 | 同任务二次 complete/fail → 不重复 finalize（回归） |

### 7.3 触发点接入 —— 扩展 `tests/test_pipeline.py`

| 用例 | 断言 |
|---|---|
| cwd 为 Issue 级 | 触发 `issue #5` 后 `agent_tasks.cwd = {WORKSPACE_DIR}/{owner}/{repo}/{agent_type}/issues/5/` 且已 makedirs |
| 三连接入 | append_event → init_event_json → enqueue 均带 `issue_session_id` |
| `#0` 事件 | 无关联 Issue → 不建 issue_agent_session，`issue_session_id=''`，走回退串行 |

### 7.4 M3 配套 —— 新增 `tests/test_repogit.py` + `tests/test_cleanup.py` + 扩展 `tests/test_pipeline.py`

`tests/test_repogit.py`（git 基建，用本地临时 git 仓库做 fixture）：

| 用例 | 断言 |
|---|---|
| clone URL 构建 | `gitcode:owner/repo` → `https://gitcode.com/owner/repo.git`；`github:owner/repo` → `https://github.com/owner/repo.git`（X 跨平台复用） |
| bare mirror clone + fetch 幂等 | 重复 `ensure_repo_base` 不重复克隆（第二次走 `fetch --prune`） |
| worktree 隔离 | Issue #5/#6 各 `ensure_issue_worktree` → 分支互不干扰、`git worktree list` 两条记录、各自 commit 独立 |
| worktree remove | `cleanup_issue_worktree` 后 worktree 列表无该条目、目录删除 |
| token 不落盘 | clone/push 后：`.git/config` 无 token、仓库内无 `.git-credentials`、捕获日志无 token |

`tests/test_cleanup.py`（sweeper）：

| 用例 | 断言 |
|---|---|
| done 才清理 | `issue_state.stage='done'` → workdir 被清；`stage='coding'` → 不清理 |
| event.json 保留 | 清理后 `{repo_key}/issues/{n}/event.json` 仍存在 |
| running 跳过 | 对应 `issue_agent_session` 有 running 任务 → 不清理 |
| 僵尸兜底 | workdir 超 7 天未活跃 → 清理 |

`tests/test_pipeline.py` 扩展：

| 用例 | 断言 |
|---|---|
| executing 派生 | 该 repo 有 `agent_tasks` status∈(dispatched,running) 的 Issue → pipeline 返回 `executing=true`；无 → false |
| 不依赖 session.status | 仅由 agent_tasks 派生，不读 `issue_agent_session.status` |

### 7.5 迁移与回归 —— 扩展 `tests/test_session_manager.py` / `test_m2_execution_mode.py`

| 用例 | 断言 |
|---|---|
| 存量列读侧回退 | 旧 `devops_agent_session.runtime_session_id` 有值 → 首任务迁移到 issue_agent_session 或按现状回退，不丢上下文 |
| 全量回归 | `python -m pytest tests/ -v` 全绿（覆盖事件管线 / chat / VOD / A2A / runtime 协议，S7） |

### 7.6 端到端（手工 / 脚本，对应验收 §九）

| ID | 场景 | 对应用例 |
|---|---|---|
| S1 | 两 worker 同 repo 两 Issue → 都能拿到同一 project-info | 7.1 缓存/生成职责 + 手工双 worker |
| S2 | 同 Agent 两 Issue 并行、各自续接 | 7.2 跨 Issue 并行 + 7.3 cwd |
| S3 | 同 Issue 评论串行 | 7.2 同 Issue 串行 |
| S4 | 远端 worker 直读 `.project-info/` | 7.1 worker 落地 + 手工远端 |

---

## 八、风险与兜底

| 风险 | 兜底 |
|---|---|
| 多 VM project-info 口径漂移 | 服务端唯一权威 + 生成职责上收；worker 不写不生成 |
| 远端拿不到基线导致分析降级 | `project_info_meta` 标注缺失/degraded，prompt 明确允许降级（README + API） |
| 同 repo 大量 Issue 并行造成 worker 负载 | `RUNTIME_MAX_CONCURRENT_TASKS` 执行槽天然节流（已有） |
| 旧任务 `issue_session_id=''` 语义变化 | 空值回退按 `agent_session_id` 串行（现状语义） |
| opencode 会话续接指针迁移出错 | `issue_agent_session` 空指针自动重建（新 Issue 会话）；保留 devops_agent_session 旧列读侧回退 |
| cwd 目录爆炸 | Issue 关闭/done 后 sweeper 清理，对齐 runtime GC |
| coding git worktree 与共享 checkout 冲突 | 只读基线 + per-Issue worktree 隔离；`git worktree list` 审计 |
| 技能文件遗漏路径改造 | 技能硬约束同步检查（`tests/test_vod_skill.py` / issue-advance 用例） |

---

## 九、验收要点

| ID | 场景 | 预期 |
|---|---|---|
| S1 | 两 worker 机器，同 repo 触发两个 Issue 事件 | 均在各自 worker 处理；都能拿到同一份 project-info（内容一致） |
| S2 | 同一 repo 同 Agent 的 Issue #5 / #6 并发 | 同时 running，互不阻塞；各自 `runtime_session_id` 独立续接 |
| S3 | Issue #5 处理中又来 #5 评论 | 串行（同 `issue_session_id` 排队） |
| S4 | 远端 worker 执行 issue-advance | read tool 可读 `{cwd}/.project-info/project-graph.md`，无需服务端路径 |
| S5 | 代码基线更新（commit 变化） | 服务端刷新 project-info，下次任务 prompt 内联新基线 |
| S6 | 无基线（生成失败） | prompt 明确标注降级，任务照常执行 |
| S7 | 全量回归 | 现有事件管线 / chat / VOD / A2A 行为不回退 |

---

## 十、关联文档

- `agent-runtime-event-full-design.md`：控制面/内容面、event.json、任务生命周期（前置）
- `concurrency-design.md`：SQLite WAL、claim 原子性、执行槽（串行粒度改动的并发基础）
- `vod-skill-upgrade-plan.md`：`issue_state` 生命周期、知识基线共用（D1 的延伸落地方案）
- `skill-delivery-remote.md`：技能/凭据随任务交付（project-info 打包复用其机制与占位符约定）
- `agent-runtime-event-tech-impl.md`：`enqueue_task` / `claim` / `_run_task` 实现基线
