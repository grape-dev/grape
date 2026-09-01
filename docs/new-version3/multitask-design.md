# Grape 多任务模式设计文档

> 版本：2.0 | 更新：2026-09-01 | 分支：multitask

## 1. 概述

多任务模式（Multi-Task Mode）是 Grape 平台针对复杂需求的拆分执行机制。当 Issue 需求涉及多个可独立交付的模块时，系统自动将父任务拆分为子任务，各子任务独立走流水线（VOD → Issue → Coding → QA），QA 通过后立即合入 PR，所有子任务 QA 完成后由父任务统一解决冲突 + 部署验证。

### 1.1 单任务 vs 多任务模式

| 维度 | 单任务模式 | 多任务模式 |
|------|-----------|-----------|
| 触发条件 | 默认 | 用户勾选「多任务模式」或 Issue body 含 `[multi-task-mode]` |
| 流水线 | VOD → Issue → Coding → QA → SRE → Done | 父任务：VOD → Issue → Done（不 A2A Coding）<br>子任务：VOD → Issue → Coding → QA → Done（不走 SRE） |
| 部署 | 单任务 SRE 部署 | 父任务统一 SRE 部署（所有子任务 QA 完成后） |
| PR 合入 | QA 通过后由 SRE 部署时处理 | 子任务 QA 通过后立即合入；冲突 PR 由父任务 cherry-pick 解决 |

### 1.2 设计目标

1. **并行加速**：多个子任务可并行执行 Coding + QA
2. **独立交付**：每个子任务有独立 Issue + PR + 测试
3. **自动合入**：QA 通过后立即合入 PR，减少人工干预
4. **冲突解决**：有冲突的 PR 由父任务统一 cherry-pick
5. **统一部署**：所有子任务完成后由父任务统一部署验证

## 2. 系统架构

### 2.1 整体流程图

```
用户创建 Issue（多任务模式）
        │
        ▼
   Webhook 触发
        │
        ▼
   VOD 审视需求
        │
        ▼
   Issue Agent 拆分子任务
   ├── 创建子 Issue #A（含 ## 父任务 链接）
   ├── 创建子 Issue #B
   ├── 创建子 Issue #C
   └── 更新父 Issue body（追加子任务清单）
        │
        ▼
   父任务 stage=done（不 A2A Coding）
        │
        ▼
   子 Issue webhook 自动触发
   ┌────────┬────────┬────────┐
   │        │        │        │
   ▼        ▼        ▼        ▼
 VOD#A   VOD#B   VOD#C    （并行）
   │        │        │
   ▼        ▼        ▼
 Issue#A Issue#B Issue#C   （精简 Spec，跳过分析）
   │        │        │
   ▼        ▼        ▼
Coding#A Coding#B Coding#C （并行实现）
   │        │        │
   ▼        ▼        ▼
  QA#A    QA#B    QA#C     （并行审查）
   │        │        │
   ▼        ▼        ▼
 合入PR#A 合入PR#B 合入PR#C （QA 通过后立即合入）
   │        │        │
   ▼        ▼        ▼
 stage=done stage=done stage=done
   │        │        │
   └────────┴────────┘
        │ 所有子任务 QA 完成
        ▼
   父任务统一处理
   ├── 1. 在父 Issue 发汇总报告
   ├── 2. Cherry-pick 有冲突的 PR
   └── 3. A2A 触发 SRE 统一部署验证
        │
        ▼
   父任务 stage=done
```

### 2.2 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| 任务创建 | `src/api/routes/tasks.py` | 前端创建任务时设置 `is_subtask_mode`，Issue body 追加 `[multi-task-mode]` 标记 |
| 会话状态 | `src/api/session_store.py` | `issue_state` 表存储 `is_subtask_mode` + `parent_issue_id` |
| 流水线分发 | `src/api/event_pipeline.py` | 父任务 Issue Agent done → 子任务 webhook 自动触发 |
| 子任务 QA 处理 | `src/api/event_pipeline.py` | QA 完成 → 合入 PR + 标记 done + 检查所有子任务完成 |
| 冲突解决 | `src/api/event_pipeline.py` | `_merge_conflict_prs_via_cherry_pick`：clone → cherry-pick → push |
| 父任务部署 | `src/api/event_pipeline.py` | `_trigger_parent_deploy`：A2A 触发 SRE |
| Issue Agent 技能 | `skills/issue/issue-multitask/SKILL.md` | 拆分逻辑、子 Issue 创建、禁止测试 Issue |
| 流水线配置 | `configs/pipelines.json` | 场景节点定义（vod → issue → coding → qa → sre → done） |

## 3. 数据模型

### 3.1 issue_state 表

```sql
CREATE TABLE issue_state (
    id              INTEGER PRIMARY KEY,
    repo_key        TEXT NOT NULL,
    issue_id        TEXT NOT NULL,          -- #1, #2, ...
    stage           TEXT DEFAULT 'vod',     -- vod/issue/coding/qa/sre/done
    owner_agent     TEXT DEFAULT '',
    a2a_seq         INTEGER,
    assignee        TEXT DEFAULT '',
    scene_id        TEXT DEFAULT '',
    scene_user_id   INTEGER DEFAULT 0,
    parent_issue_id TEXT DEFAULT '',         -- 父任务 Issue 号（#1），子任务才有
    is_subtask_mode INTEGER DEFAULT 0,       -- 1=多任务模式
    updated_at      TEXT,
    UNIQUE(repo_key, issue_id)
);
```

### 3.2 tasks 表关键字段

```sql
is_subtask_mode    INTEGER DEFAULT 0,   -- 1=多任务模式
parent_issue_number INTEGER DEFAULT 0,  -- 父任务 Issue 编号
```

### 3.3 状态流转规则

| Issue 类型 | stage 流转 | 说明 |
|-----------|-----------|------|
| 普通任务 | vod → issue → coding → qa → sre → done | 完整流水线 |
| 父任务 | vod → issue → done | Issue Agent 拆分后直接 done |
| 子任务 | vod → issue → coding → qa → done | QA 完成后直接 done（不走 SRE） |

## 4. 详细设计

### 4.1 任务创建

**入口**：`POST /api/tasks/create`（前端「新建任务」按钮）

**流程**：

1. 用户勾选「多任务模式」→ `is_subtask_mode=1`
2. 创建 GitCode Issue
3. **先设置 `issue_state.is_subtask_mode=1`**（在 task 创建之前，确保 task 创建时能读到正确值）
4. Issue body 追加 `[multi-task-mode]` 标记
5. `ensure_issue_task` 创建 task → 从 `issue_state` 同步 `is_subtask_mode` + `parent_issue_number`

**关键代码**（`src/api/routes/tasks.py`）：

```python
# 4) 多任务模式 → 先设置 issue_state 标记（在 ensure_issue_task 之前）
if body.is_subtask_mode:
    set_subtask_mode(repo_key, f"#{issue_num}", True)
    _patch_issue_body(api_token, owner, rname, issue_num, 
                      body.description + "\n\n---\n[multi-task-mode]...")

# 2) 按既有流程建档 — 在 set_subtask_mode 之后
task_id = ensure_issue_task(repo_key, issue_num, ...)
```

**时序保证**：`set_subtask_mode` 必须在 `ensure_issue_task` 之前执行，确保 `_get_issue_subtask_mode` 能从 `issue_state` 读到正确值。

### 4.2 父任务 Issue Agent 拆分

**触发条件**：VOD 审视后 A2A 交办 Issue Agent，prompt 注入 `is_subtask_mode: true`

**Issue Agent 行为**（`skills/issue/issue-multitask/SKILL.md`）：

1. 评估可拆分性（总预估 > 20 分钟？可独立交付？有依赖？）
2. 创建子 Issue（调用 GitCode API）
   - 每个 Issue body 包含 `## 父任务` + Issue 链接 + 目标 + 验收标准 + 依赖
   - **禁止创建测试 Issue**（硬约束）
3. 更新父 Issue body 追加子任务清单
4. 发布拆分说明评论

**服务端协同**（`src/api/event_pipeline.py`）：

```python
# 父任务 Issue Agent 完成后 → stage=done（不 A2A coding）
if new_status == "completed" and agent_type == "issue" and issue_id:
    if is_subtask_mode_enabled(repo_key, issue_id):
        set_issue_stage(repo_key, issue_id, "done", "issue")
```

### 4.3 子任务自动触发

子 Issue 创建后 webhook 自动触发 → VOD 审视 → Issue Agent（精简 Spec）→ Coding → QA

**子任务快速通道**：
- 跳过深度可开发性分析（父任务已确认）
- 跳过完整 Spec 生成 → 用精简 Spec（目标 + 验收标准 + 实现建议）
- 直接 A2A 交办 Coding

### 4.4 子任务 QA 完成处理

**触发条件**：`new_status == "completed" and agent_type == "qa"` 且 Issue 有 `parent_issue_id`

**处理流程**：

1. **标记 stage=done**（子任务不走 SRE，由父任务统一部署）
2. **立即合入该子任务的 PR**（`_merge_subtask_pr`）
   - 查询仓库 open PR，匹配 title/head branch 包含 issue 号
   - `mergeable=True` → 调用 GitCode merge API 合入
   - `mergeable=False` → 返回 `conflict`，待父任务 cherry-pick
   - 无匹配 PR → 返回 `no_pr`
3. **登记完成情况到父 Issue**（含 PR 合入结果）
   - `✅ PR 已自动合入` / `⚠️ PR 有冲突，待父任务 cherry-pick` / `ℹ️ 未找到关联 PR`
4. **检查所有子任务 QA 是否完成**
   - `all(stage in ("qa", "sre", "done"))` → 全部 QA 通过
   - 全部通过 → 触发 `_post_all_subtasks_done`

**PR 合入 API**：

```
PUT https://gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{pr_num}/merge
Body: {"merge_method": "merge"}
Header: Authorization: Bearer {bot_token}
```

### 4.5 所有子任务完成后统一处理

**触发条件**：`_post_all_subtasks_done`（所有子任务 stage >= qa）

**处理流程**：

#### 4.5.1 汇总报告

在父 Issue 发布评论，列出所有子任务完成状态：

```
### 全部子任务已完成

- ✅ #2 (done)
- ✅ #3 (done)
- ✅ #4 (done)
- ✅ #5 (done)
- ✅ #6 (done)

父任务 #1 的所有子任务已完成。
```

#### 4.5.2 Cherry-pick 有冲突的 PR

**函数**：`_merge_conflict_prs_via_cherry_pick`

**流程**：

1. 查询仓库所有 open PR，筛选 `mergeable=False` 且目标分支为 main/master 的
2. Clone 仓库（`--depth 50`，用 bot token 认证）
3. Checkout main（或 master）
4. 设置 git 身份（`user.email` + `user.name`，cherry-pick 需要）
5. 对每个冲突 PR：
   - `git fetch origin refs/merge-requests/{N}/head:pr-{N}`（GitCode 用 `refs/merge-requests/` 而非 GitHub 的 `pull/`）
   - `git cherry-pick --strategy=recursive -X theirs pr-{N}`（冲突时保留子任务实现）
   - 失败 → `git cherry-pick --abort`，跳过该 PR
6. `git push origin main`

**GitCode ref 格式**：

| 平台 | PR ref 格式 |
|------|------------|
| GitHub | `refs/pull/{N}/head` |
| GitCode | `refs/merge-requests/{N}/head` |

#### 4.5.3 A2A 触发父任务 SRE 部署

**函数**：`_trigger_parent_deploy`

**流程**：

1. 获取 server_base_url（优先本地直连 `127.0.0.1:38080`）
2. POST `/api/a2a`
   - `agent: "sre"`
   - `payload: {repository, issue_number, context: "父任务所有子任务已完成、PR 已合入，开始统一部署验证"}`
3. SRE Agent 收到 A2A 后执行部署（build → deploy → verify）

### 4.6 auto-advance 机制移除

**背景**：原 `auto-advance` 机制在任务完成后检查 pipeline 是否推进，没推进则自动补触发下一个 agent。导致：
- coding 失败后推进到 qa（空跑）
- agent 正常调了 A2A 但 stage 没更新 → 重复触发
- 大量重复 A2A 事件（#13 coding 5 次、qa 6 次）

**方案**：完全移除 `_auto_advance_pipeline`，改为：
1. 纯事件驱动（agent 自己调 A2A）
2. pipeline 卡住时由前端「重派」按钮人工触发
3. `detect_stale_pipelines` 检测 30 分钟无活动的 Issue

## 5. 验证用例

### V1: 多任务模式创建

| 步骤 | 操作 | 预期 |
|------|------|------|
| 1 | 前端勾选「多任务模式」创建任务 | `issue_state.is_subtask_mode=1` |
| 2 | 查 task 记录 | `tasks.is_subtask_mode=1` |
| 3 | 查 GitCode Issue body | 含 `[multi-task-mode]` 标记 |

### V2: 父任务拆分

| 步骤 | 操作 | 预期 |
|------|------|------|
| 1 | VOD 审视父任务 | A2A 交办 Issue Agent，payload 含 `is_subtask_mode: true` |
| 2 | Issue Agent 完成拆分 | 创建 N 个子 Issue，每个含 `## 父任务` 链接 |
| 3 | 父任务 stage | `done`（不 A2A Coding） |
| 4 | 子 Issue 创建 | webhook 自动触发 VOD |

### V3: 子任务 QA 完成 → PR 合入

| 步骤 | 操作 | 预期 |
|------|------|------|
| 1 | 子任务 QA 通过 | `stage=done` |
| 2 | PR mergeable=True | PR 自动合入，父 Issue 评论 `✅ PR 已自动合入` |
| 3 | PR mergeable=False | 不合入，父 Issue 评论 `⚠️ PR 有冲突` |

### V4: 所有子任务完成 → 父任务部署

| 步骤 | 操作 | 预期 |
|------|------|------|
| 1 | 最后一个子任务 QA 完成 | 触发 `_post_all_subtasks_done` |
| 2 | 父 Issue 汇总报告 | 评论列出所有子任务状态 |
| 3 | 冲突 PR cherry-pick | clone → cherry-pick → push main |
| 4 | A2A 触发 SRE | SRE Agent 开始统一部署 |

### V5: Cherry-pick 冲突解决

| 步骤 | 操作 | 预期 |
|------|------|------|
| 1 | PR 有冲突（mergeable=False） | 子任务 QA 完成时不合入 |
| 2 | 所有子任务完成后 | cherry-pick 到 main |
| 3 | cherry-pick 成功 | `push origin main`，PR commit 进入 main |
| 4 | cherry-pick 失败 | `--abort` 跳过，日志记录 |

### V6: auto-advance 移除验证

| 步骤 | 操作 | 预期 |
|------|------|------|
| 1 | Coding 完成 | 不自动推进 QA（由 agent 自己 A2A） |
| 2 | Agent 没调 A2A | pipeline 卡住，不自动补触发 |
| 3 | 前端重派 | 手动触发下一个 agent |

### V7: Stale pipeline 检测

| 步骤 | 操作 | 预期 |
|------|------|------|
| 1 | 调用 `GET /api/runtime/pipelines/stale` | 返回 30 分钟无活动的 Issue 列表 |
| 2 | Issue 有 running agent | 不算 stale |
| 3 | Issue 无活动 > 30 分钟 | 标记 stale |

## 6. 关键设计决策

| # | 决策 | 原因 |
|---|------|------|
| 1 | `set_subtask_mode` 在 `ensure_issue_task` 之前 | 确保 task 创建时能从 issue_state 读到正确的 is_subtask_mode |
| 2 | 子任务 QA 完成后 stage=done（不走 SRE） | 子任务不需要单独部署，由父任务统一部署 |
| 3 | 子任务 QA 完成后立即合入 PR | 减少冲突概率（越早合入，后续 PR 冲突越少） |
| 4 | 冲突 PR 用 cherry-pick 而非 merge | cherry-pick 可以逐个解决冲突，`-X theirs` 保留子任务实现 |
| 5 | GitCode 用 `refs/merge-requests/{N}/head` | GitCode 不支持 GitHub 的 `pull/{N}/head` 格式 |
| 6 | cherry-pick 后直接 push main | bot 有写权限，无需再走 PR 合入流程 |
| 7 | 移除 auto-advance | 避免空跑推进和重复 A2A 事件 |
| 8 | 父任务 Issue Agent 不 A2A Coding | 多任务模式下子 Issue webhook 自动触发流水线 |
| 9 | 禁止创建测试 Issue | Issue Agent 曾建测试 Issue 验证 API → 触发 webhook 浪费资源 |

## 7. 风险与回退

### 7.1 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| cherry-pick `-X theirs` 丢失冲突方的修改 | 子任务实现可能覆盖已有代码 | 保留子任务实现是预期行为（子任务 QA 已通过） |
| bot token 无写权限 | push 失败 | 需确保 bot 是仓库 Maintainer |
| 子任务 PR 没有匹配的 issue 号 | PR 不被合入 | `_merge_subtask_pr` 按 title/head branch 匹配 |
| 多个子任务同时 QA 完成 | 并发触发 `_post_all_subtasks_done` | 幂等：重复调用只发一次评论 |
| GitCode API 限流 | PR 查询/合入失败 | 日志记录，不影响流水线 |

### 7.2 回退

- 子任务 QA 完成后不合入 PR → 回退为所有子任务完成后统一合入
- cherry-pick 失败 → 手动在 GitCode 网页解决冲突
- 父任务 SRE 部署失败 → 前端重派 SRE

## 8. 文件索引

| 文件 | 说明 |
|------|------|
| `src/api/routes/tasks.py` | 任务创建、`_get_issue_subtask_mode`、`_get_issue_parent` |
| `src/api/session_store.py` | `set_subtask_mode`、`get_parent_issue`、`get_subtask_issues`、`is_subtask` |
| `src/api/event_pipeline.py` | 父任务 done、子任务 QA 处理、PR 合入、cherry-pick、父任务部署 |
| `src/runtime/service.py` | `complete_task`（exit_code 修复）、`detect_stale_pipelines` |
| `skills/issue/issue-multitask/SKILL.md` | Issue Agent 多任务拆分技能 |
| `configs/pipelines.json` | 流水线场景节点定义 |
| `start-worker.sh` | Worker daemon 保护（崩溃自动重启） |
