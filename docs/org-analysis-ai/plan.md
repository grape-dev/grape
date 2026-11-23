# 组织仓库 AI 分析 + 自动刷新 + 下派联动方案

> 状态：草案（2026-11-19）
> 目标：把当前“确定性文档摘要”的仓库地图升级为“运行时 AI 分析”，并把分析结果、定时刷新与下派流水线串成闭环。

## 1. 结论

四个需求都可以在当前架构内实现，没有硬阻塞：

1. 关联仓库后自动 AI 分析 → 复用现有绑定钩子（`_bind_one` 已经会调度 `analyze_repo`），把确定性分析替换/扩展为“一次性 AI 分析任务”，运行时用现有 `pick_engine` 的随机选择能力。
2. 组织仓库每天 0 点自动分析 → 复用现有 Autopilot 定时任务（`autopilot_tasks` + scheduler），在关联仓库时幂等 ensure 一个受管任务。
3. 分析输出统一进 `grape-org` 内容分支 → 扩展现有 `sync_org_context_to_repo` 的 git 同步能力；`main` 保持空仓，不写任何 Grape 内容。
4. 下派时直接基于输出文档分析 → 在组织流水线的 `org_dispatch` 节点里读取分析分支文档，用当前引擎做 AI 匹配，再走现有 `org dispatches create` 显式 targets 契约；后端保留确定性召回作为兜底。

关键原则：

- **AI 只在“分析/匹配”层使用**，下派、权限、幂等、审计仍走现有确定性后端契约。
- **DB 是分析结果的权威源**，git 分支是便携/可审阅的产物快照。
- **不跨域直查/直写**：新增能力通过 owner 域 `public` 原语暴露（mgmt / runtime 边界）。

## 2. 总体数据流

```
用户关联仓库
  └─ org_repos._bind_one
       ├─ ensure_org_analysis_automation(org)      # 需求 2：幂等创建每日 0 点任务
       └─ request_repo_analysis(org, repo, trigger=link)
             └─ runtime.enqueue_one_shot_task(skill=org-repo-analyzer)
                   ├─ pick_analysis_engine(): 健康运行时中 random.choice
                   ├─ agent 在目标仓执行（只读）
                   └─ POST /orgs/{id}/repo-map/{repo}/ai-result
                         ├─ 校验 JSON schema → 写 grape_t_org_repo_map（覆盖旧结果）
                         ├─ 审计（engine / runtime / source_commit / result_hash）
                         └─ sync_org_analysis_to_repo(org)  # 需求 3：推 grape-org 分支

每天 00:00（Autopilot 任务）
  └─ org-repo-analysis-runner skill
       └─ grape org analysis run-all --org_id ...
             └─ 无关联仓库 → 直接退出；有 → 逐个 request_repo_analysis(trigger=schedule)

组织下派流水线
  org_intake → org_dispatch
                 ├─ 读取 grape-org 分支的分析文档
                 ├─ 引擎（AI）生成 targets + 理由 + acceptance
                 └─ org dispatches create（显式 targets，plan 保留理由，可审计）
```

## 3. 需求 1：关联仓库后自动 AI 分析

### 3.1 触发点

现有绑定链路已经具备钩子：

- `src/domains/mgmt/routes/org_repos.py`
  - `_bind_one()` 末尾已经调用 `_schedule_repo_map_analysis(org_id, repo_key)`（当前是确定性 `analyze_repo`）。
  - 批量绑定 `bind_repos_batch` 逐个走 `_bind_one`。
  - `transfer_repo` 转移组织时也应触发目标组织分析。
- `src/domains/mgmt/org_provision.py`
  - `manual_bind_org_repo` / `_bind_org_repo`（组织仓库 provisioning 路径）需要 ensure 自动化任务。

把 `_schedule_repo_map_analysis` 改为调用新的 `request_repo_analysis(...)`，保留旧确定性分析作为降级路径（开关控制）。

### 3.2 执行方式：一次性 AI 分析任务

不建议把单仓分析硬塞进 Autopilot 的既有执行流（其 prompt 输出格式是 findings/issue 口径，且任务 instruction 是全局的）。推荐：

- 在 runtime 域新增 public 原语：
  ```python
  enqueue_one_shot_skill_task(
      repo_key: str,
      skill_name: str,
      prompt: str,
      params: dict,
      agent_type: str = "qa",
      engine_ids: list[int] | None = None,
      scene_id: str = "",
  ) -> dict   # {task_id, engine_id, session_id}
  ```
  内部复用现有 `_create_direct_task` / `enqueue_task` 路径（含 workdir 物化、skill 下发、token 注入、任务日志）。
- mgmt 侧 `src/domains/mgmt/org_analysis.py`（新增）负责：
  - 参数组装：`org_id`、`repo_key`、`analysis_run_id`、输出分支名、schema 版本。
  - 去重/合并：同一 repo 已有 queued/running 任务时，不重复派发（可标记 `pending_rerun`，当前任务完成后自动再跑一次）。
  - 记录 `analysis_run_id` → 与回调结果对账。

### 3.3 随机可用运行时

现有 `src/domains/runtime/engine/engine_select.py::pick_engine` 已经支持：

- 健康度/标签/作用域过滤；
- `rule != "order"` 时 `random.choice(cand)` 随机选择健康引擎。

新增 `pick_analysis_engine(org_id, repo_key)`（runtime 域）统一口径：

1. 候选 = 组织分析场景/组织场景中 qa/analysis 节点的 `engine_runtime_ids`（健康过滤后）；
2. 为空时按产品口径回落到组织 owner 可见的全局健康运行时池；
3. 仍为空 → 返回 `engine_unavailable`，任务失败并保留旧分析结果，等待下一次触发（不空转、不静默成功）。

> 待确认：候选池是否允许“全组织可见的健康运行时”，还是只允许组织场景绑定的运行时。建议先只允许组织场景池，避免把用户个人机器拉进组织级批量任务。

### 3.4 AI 技能与输出

新增 QA 型技能：`.grape/skills/qa/org-repo-analyzer/`

- 输入：`ORG_ID`、`REPO_KEY`、`ANALYSIS_RUN_ID`、schema 版本（通过 prompt/env 注入）。
- 行为：只读；读取 README、依赖清单、目录结构、project-info、org-context；不修改文件、不建 PR、不发 Issue。
- 输出：严格 JSON（代码块），字段：
  ```json
  {
    "schema_version": 1,
    "repo_key": "gitcode:owner/repo",
    "source_commit": "abc123",
    "purpose": "一句话定位",
    "domains": ["..."],
    "keywords": ["..."],
    "interfaces": ["HTTP /api/...", "CLI ..."],
    "depends_on": ["owner/repo"],
    "not_owns": ["..."],
    "capabilities": [{"name": "...", "description": "...", "paths": ["..."]}],
    "confidence": 0.0,
    "evidence": ["README.md", "src/..."],
    "risks": ["..."]
  }
  ```
- 结果通过新 API 回传（worker session token），不让 agent 直接 push git：
  `POST /api/orgs/{org_id}/repo-map/{repo_key}/ai-result`
  - 校验：org 成员/owner、repo 属于该 org、`analysis_run_id` 未过期、JSON schema、大小上限。
  - 落库：写 `grape_t_org_repo_map` 的 AI 字段（覆盖旧结果），记录 engine/runtime/source_commit/result_hash/analyzed_at。
  - 异步：触发 `sync_org_analysis_to_repo(org_id)`。

建议保留现有的手动确定性分析入口（`POST /repo-map/{repo_key}/analyze`），新增 AI 异步入口（见 API 清单），便于灰度与降级。

## 4. 需求 2：组织仓库每日 0 点自动分析

### 4.1 复用 Autopilot 定时任务

`autopilot_tasks` 已支持：`repo_key`、`cron`、`instruction`、`skill_name`、`engine_runtime_ids/uids`、`work_branch`、`custom_fields`、`enabled`。调度器 `src/domains/runtime/engine/scheduler.py` 每轮扫描 enabled 任务。

自动创建的受管任务：

- `repo_key`：组织仓库 `org.org_repo_key`
- `name`：`__org_analysis__:<org_slug>`（稳定标识）
- `cron`：`0 0 * * *`
- `skill_name`：`org-repo-analysis-runner`
- `instruction`：
  ```text
  调用 grape org analysis run-all --org_id <ORG_ID> --trigger schedule。
  该命令会检查组织关联仓库；没有关联仓库时直接退出，不执行任何分析。
  ```
- `custom_fields`：空（org_id/mode 由任务指令与本次运行参数提供；受管身份按 `skill_name` 识别。不再写 `GRAPE_*` 系统保留变量）
- `created_by`：组织 owner（确保 skill/凭据可见性）
- `enabled=1`

### 4.2 幂等 ensure 时机

新增 `ensure_org_analysis_automation(org_id, actor_id)`（mgmt 域）：

- 只对团队组织、且存在 `org_repo_key` 的组织生效；
- 查询 `autopilot_tasks WHERE repo_key=? AND name=? AND skill_name=?`；
- 不存在 → 创建；存在 → 校正 cron/enabled/skill/instruction（不覆盖用户改过的其他字段，除非带 `__org_analysis__` 标记）；
- 失败只告警，不阻断绑定请求。

调用点：

- `org_repos.py::_bind_one`（单条/批量都覆盖）
- `org_repos.py::transfer_repo`（目标组织）
- `org_provision.py::manual_bind_org_repo` / `_bind_org_repo`（组织仓库创建/替换）
- 可选：`orgs.py` 创建组织后 provisioning 完成时

### 4.3 空组织不执行

两层保护：

1. runner skill 先调用 `grape org repos list --org_id ...`（或 CLI 等价命令）；过滤掉组织自动仓库后为空 → 直接输出 `SKIPPED: no linked repos`，exit 0。
2. `run-all` 后端接口同样在无关联仓库时返回 `{skipped: true, reason: "no_repos"}`，不创建任何分析任务。

## 5. 需求 3：分析输出放独立分支

### 5.0 分支命名与组织仓库默认分支

统一规则：**Grape 托管的分支一律以 `grape-` 开头**。

| 用途 | 分支名 | 说明 |
|---|---|---|
| 新建组织自动仓库的默认分支 | `main` | 保持空仓，留给组织自己开发；Grape 不写任何内容 |
| Grape 托管内容分支 | `grape-org` | 唯一写入目标：组织上下文/知识/需求目录/仓库地图/AI 结果都放这里 |
| 既有组织仓库 | 一次性迁移 | 旧 `grape-repo-knowledge` / `grape-repo-analysis` 只读合并进 `grape-org`，不再继续推送 |

实现要点：

1. `org_provision._create_org_repo()` 使用 `default_branch="main"` + `auto_init=False`；若平台建出完全空仓，同步时补一个**空提交**建立 `main`（不写 README/文件）。
2. `sync_org_context_to_repo()` 固定写 `grape-org`；清理旧上下文文件时保留 `org-context/repo-map/`（地图由分析同步独占维护）。
3. `sync_org_analysis_to_repo()` 也写 `grape-org`，只覆盖 `org-context/repo-map/`，保留同分支上的上下文/知识文件。
4. 两条同步共用同一工作副本与组织级内容锁，避免并发推同一分支产生 non-fast-forward。
5. 迁移策略：首次写入 `grape-org` 时，如果该分支不存在，则一次性读取旧 `grape-repo-knowledge`/`grape-repo-analysis` 文件并集作为初始内容；之后不再写旧分支。
6. 分支保护：`main` 与 `grape-org` 纳入组织仓库保护策略；`grape-org` 允许后端受控推送。

### 5.1 分支形态

`grape-org` 是唯一 Grape 内容分支，组织上下文、知识、需求目录与仓库地图共存。

- 内容：
  ```
  README.md
  org-context/repo-map/index.json          # 机器索引（repo_key / confidence / commit / analyzed_at）
  org-context/repo-map/<provider>/<owner>/<repo>.md
  org-context/analysis/<run_id>.json        # 可选：结构化结果存档
  ```
- 不包含组织业务代码；`main` 保持空仓，留给组织自己开发。

> `org-context/repo-map/` 与 `org-context/rules.md|profile.md|repos/<...>/context.md` 同分支共存，互不覆盖。

### 5.2 写入方式

新增 `sync_org_analysis_to_repo(org_id)`（mgmt 域，可复用 `org_context_repo.py` 的工具函数）：

- 独立工作副本：`workspace/<org_repo>/grape-content-sync`，避免污染代码 agent 的 bare/worktree。
- 首次：`git clone --depth 1` 后创建/切换 `grape-org`，写文件，push；存量仓从旧内容分支一次性迁移。
- 后续：`fetch origin grape-org --depth 1` → `checkout -B grape-org FETCH_HEAD` → 只覆盖对应目录 → commit → push `HEAD:refs/heads/grape-org`。
- commit message：`chore(grape-org): refresh AI repo map (YYYY-MM-DD HH:MM)`。
- 无变化不提交。
- 失败只告警，DB 仍是权威源；下次分析/定时任务会重试。

### 5.3 主分支现有 repo-map 的处理

当前 `_render_files()` 会把 `org-context/repo-map/` 一起推到 main。建议：

- 阶段 1：保留 main 的既有文件，但停止更新（新增开关 `ORG_CONTEXT_SYNC_REPO_MAP=0`）；
- 阶段 2：一次性清理 main 上的 `org-context/repo-map/`（单独提交，便于回滚）；
- 所有消费者改读 `grape-org` 分支。

## 6. 需求 4：下派流水线直接使用分析文档

### 6.1 推荐做法：AI 在流水线节点里做匹配

组织流水线节点（`configs/pipelines.json`）：

```
org_intake (issue / org-intake-cli)
  → org_dispatch (coding / org-dispatch-cli)
      → org_aggregate (qa / org-aggregate-cli)
```

扩展 `org-dispatch-cli`（或新增 `org-dispatch-ai` 技能）：

1. 读取 `grape-org` 分支：
   - `org-context/repo-map/index.json`
   - 各仓 `repo-map/*.md`
   （通过现有 `git-repo contents --ref grape-org` 或 `VcsApiClient.get_file(ref=...)`）
2. 把「组织需求正文 + 仓库地图摘要」交给当前运行的引擎，要求输出：
   - `targets`（repo_key 列表）
   - 每个 target 的匹配理由
   - 建议的 acceptance / 风险
3. 调用现有 `grape org dispatches create`，**显式传 targets**：
   - `context.summary/constraints/decisions/knowledge_refs/acceptance`
   - `description` 中保留 AI 匹配理由，便于审计
4. 失败/低置信度：
   - 回落到后端确定性 `recall_targets`（基于 AI 写入 DB 的 purpose/domains/keywords）
   - 仍无目标 → 明确返回 `NO_CONFIDENT_TARGET`，不静默下派全部仓

这样：
- 后端 `create_dispatch` 契约不变（显式 targets 已有校验：必须属于该组织）；
- AI 只负责“选仓 + 写理由”，不直接写 Issue/不改代码；
- 每一步都有 plan/context 可追溯。

### 6.2 后端确定性召回同步增强

即使不改流水线，`org_repo_map.recall_targets` 也会因为 AI 写入了更好的 `purpose/domains/keywords/interfaces` 而自动受益。建议：

- AI 结果落库时保留结构化字段（不要只存 Markdown）；
- 召回评分继续用现有 token/权重逻辑，避免引入后端 LLM 依赖；
- 调整置信度阈值（AI 分析的 confidence 通常高于 0.25 的确定性基线）。

## 7. 数据模型

### 7.1 `grape_t_org_repo_map` 扩展列（mgmt）

```sql
ALTER TABLE grape_t_org_repo_map ADD COLUMN ai_json TEXT DEFAULT '';
ALTER TABLE grape_t_org_repo_map ADD COLUMN ai_status TEXT DEFAULT '';        -- queued/running/succeeded/failed
ALTER TABLE grape_t_org_repo_map ADD COLUMN ai_error TEXT DEFAULT '';
ALTER TABLE grape_t_org_repo_map ADD COLUMN ai_engine_id INTEGER DEFAULT 0;
ALTER TABLE grape_t_org_repo_map ADD COLUMN ai_analyzed_at TEXT DEFAULT '';
ALTER TABLE grape_t_org_repo_map ADD COLUMN ai_source_commit TEXT DEFAULT '';
ALTER TABLE grape_t_org_repo_map ADD COLUMN ai_result_hash TEXT DEFAULT '';
```

### 7.2 分析运行台账（mgmt，可选但推荐）

`grape_t_org_repo_analysis_run`：

```
id (uuid/text PK)
org_id
repo_key
trigger        -- link / schedule / manual
status         -- queued/running/succeeded/failed/skipped
task_id        -- runtime agent_tasks.id
engine_id
requested_by
started_at / finished_at
error
result_hash
created_at
```

用途：UI 进度、去重、审计、失败重试；不依赖跨域 join runtime events。

> 新表必须登记 `configs/table_ownership.json`（mgmt），并走 `register_migration`。

## 8. API / CLI / 技能清单

### 8.1 API（mgmt）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/orgs/{id}/repo-map/{repo_key}/ai-analyze` | 手动触发单仓 AI 分析（202 + run_id） |
| POST | `/api/orgs/{id}/repo-map/ai-analyze-all` | 手动触发全量（无关联仓库返回 skipped） |
| POST | `/api/orgs/{id}/repo-map/{repo_key}/ai-result` | worker 回调保存 AI 结果（session token） |
| GET | `/api/orgs/{id}/repo-analysis/runs` | 运行台账（分页/状态过滤） |
| POST | `/api/orgs/{id}/analysis-automation/ensure` | 幂等 ensure 每日任务（管理入口/运维） |

### 8.2 CLI（`grape`）

- `grape org analysis run-one --org_id ... --repo_key ... --trigger link`
- `grape org analysis run-all --org_id ... --trigger schedule`
- `grape org repo-map save-ai --org_id ... --repo_key ... --json ...`（worker 回调，或直接走 HTTP）

命令由 operationId 自动生成，需确认 `grape commands` 暴露。

### 8.3 技能

- 新增 `.grape/skills/qa/org-repo-analyzer/`：单仓 AI 分析（只读）。
- 新增 `.grape/skills/org/org-repo-analysis-runner/`：每日任务 runner，只负责调用 `run-all` 并处理空组织。
- 扩展 `.grape/skills/org/org-dispatch-cli/`（或新增 `org-dispatch-ai`）：读取分析分支 + AI targets + create dispatch。

## 9. 改动清单（文件级）

**新增**

- `src/domains/mgmt/org_analysis.py`（触发/去重/ensure/结果保存/分支同步入口）
- `src/domains/mgmt/routes/org_analysis.py`（API）
- `src/domains/runtime/public` 中的 `enqueue_one_shot_skill_task` / `pick_analysis_engine` 原语
- `src/domains/mgmt/org_context_repo.py::sync_org_analysis_to_repo`
- `.grape/skills/qa/org-repo-analyzer/**`
- `.grape/skills/org/org-repo-analysis-runner/**`
- `docs/org-analysis-ai/plan.md`（本文）

**修改**

- `src/domains/mgmt/schema_ddl.py` / `src/api/db_setup.py`：新增列/表 + 迁移
- `configs/table_ownership.json`：新表归属
- `src/domains/mgmt/org_repo_map.py`：AI 结果写入、Markdown 渲染、召回可读 AI 字段
- `src/domains/mgmt/routes/org_repos.py`：`_bind_one` / transfer 钩子；AI 分析 API
- `src/domains/mgmt/org_provision.py` / `routes/orgs.py`：组织仓库绑定后 ensure 自动化
- `src/domains/runtime/engine/scheduler.py`（如需）：受管任务标识/执行参数（若不走一次性任务）
- `src/domains/mgmt/org_dispatch.py` / `configs/pipelines.json` / `org-dispatch-cli`：AI 选仓
- 前端：组织仓库地图页（AI 状态/重新分析/查看分支提交）、自动化列表（受管任务标识）

## 10. 并发、幂等、失败与安全

- **去重**：`(org_id, repo_key, status in queued/running)` 唯一；重复触发合并为一次，完成后可 `pending_rerun`。
- **覆盖**：成功结果覆盖旧 AI 字段；失败保留旧结果与旧 Markdown，只更新 `ai_status/ai_error`。
- **超时/重试**：单仓任务超时（默认 10min）→ failed；每日任务与下一次链接会重试。
- **无引擎**：明确 failed（`engine_unavailable`），不空跑、不静默；每日任务不因单仓失败中断其他仓。
- **分支 push 失败**：DB 仍成功；记录 `output_commit` 为空/旧值，后续同步重试；不把分析判失败。
- **安全**：
  - 技能只读；禁止执行仓库内脚本；
  - 结果 JSON schema 校验 + 大小上限（建议 256KB）；
  - Markdown 渲染转义；不把仓库内容当指令（prompt injection 声明）；
  - 审计 actor/engine/prompt 版本/source_commit/result_hash；
  - 回调接口只允许 worker session token + repo/org 匹配。

## 11. 分阶段实施

### P0（基础闭环，1–2 天）

- AI 技能 + 结果回调 API + DB 字段
- `org_analysis.py` 单仓触发 + 随机引擎选择
- `sync_org_analysis_to_repo` + `grape-org` 分支
- 手动 API 在 org 123 的 3 个测试仓验证

### P1（自动化，1 天）

- `_bind_one` / transfer / manual org repo 钩子
- `ensure_org_analysis_automation`（每日 0 点任务）
- 空组织跳过
- 重复触发去重

### P2（下派联动，1–2 天）

- `org-dispatch-cli` 读取 `grape-org` 分支 + AI 选仓
- 显式 targets + 理由写回 plan/context
- 确定性 `recall_targets` 兜底与阈值调整

### P3（体验与治理）

- UI：AI 分析状态、最近引擎/时间、分支提交链接、手动刷新
- 运行台账/失败重试/告警
- 文档、回归测试（含 org 123 端到端）

## 12. 风险与待确认

1. **引擎池口径**：分析允许哪些运行时？建议只允许组织场景绑定的健康运行时；是否允许全部可见运行时需要确认。
2. **每日任务形态**：受管 Autopilot 任务（用户可见、可手动触发）是否符合预期？还是希望后台隐藏任务？
3. **分支策略**：新建组织仓库保留空 `main`，所有 Grape 内容写 `grape-org`；存量旧内容分支一次性迁移后停止写入。
4. **人工编辑**：AI 覆盖是否会覆盖人工维护的 `REPO_MAP.md`？建议 AI 只写 `org-context/repo-map/`，人工文件优先/只读。
5. **成本**：链接即分析可能频繁触发；建议 30–60s 去抖 + 同仓合并；是否只对“有代码变更”的仓库重跑（按 source_commit 判断）。
6. **下派 AI 的确定性**：AI 选仓结果需要保留理由并可人工覆盖；是否允许 AI 在低置信度时退回“人工选择 targets”。
7. **多平台 token**：分析目标是 GitHub/GitCode 仓，任务使用目标仓 token；组织仓库回调使用 org repo token，需确认 worker 侧凭据注入范围。

## 13. 验收标准（建议）

- 关联任意仓库后 2 分钟内出现一条 AI 分析运行记录，成功后在 `grape-org` 分支看到对应 Markdown，旧结果被覆盖。
- 组织仓库自动化列表存在每日 0 点任务；无关联仓库时执行结果明确为 skipped。
- 手动/定时/链接三种触发均幂等，不产生重复运行。
- 组织下派时，dispatch plan 中有 targets 与 AI 匹配理由；低置信度不静默下派全部仓库。
- 失败场景（无引擎、分析超时、分支 push 失败）不破坏旧结果，可在台账中看到明确原因。
## 14. 当前实现状态（2026-11-19）

已落地（本地 `feat/org-repo-map-ai` 分支，未推送）：

- 分支命名：新建组织仓库默认分支 `main`（空仓）；Grape 内容统一写 `grape-org`（上下文/知识/地图共存，单工作副本串行同步）。
- AI 分析编排：`src/domains/mgmt/org_analysis.py` + `routes/org_analysis.py`（targets/context/docs/status/run/ensure/ai-result）。
- 运行时执行：Autopilot 任务 + 单次运行参数（`autopilot_executions.params_json`），任务级 `engine_runtime_ids` 健康候选随机选引擎。
- 技能：`.grape/skills/qa/org-repo-analyzer/`（只读、结构化 JSON、HTTP 回传）。
- 关联钩子：组织绑定 / 批量绑定 / 转移组织 / 手动组织仓库 / 托管进组织，均 ensure 每日 0 点任务并触发单仓 AI 分析。
- 每日任务：`0 0 * * *`，空组织由 skill 输出 `SKIPPED: no linked repos`。
- 下派联动：`GET /repo-analysis/docs` 供 `org-dispatch-cli` 读取；skill 已加入 AI 选仓与确定性召回兜底步骤。

待完成：

- 前端组织仓库地图页展示 AI 状态/最近引擎/手动刷新/分支提交链接；
- 三库拓扑下 mgmt 触发 Autopilot 的跨进程路径（当前单库本地可用；拆库应走 worker/event 进程）；
- 分支同步的集成测试（真实 git 远端）与端到端引擎跑通；
- `openapi.json` 的生成物更新（本次未提交，避免格式化噪音）。
