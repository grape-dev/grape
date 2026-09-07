# Issue #49 优化方案：GitCode Webhook 事件处理（action 过滤 + 分 Issue 阶段模型）

> Issue：https://gitcode.com/grape-dev/grape/issues/49
> 类型：优化（token 浪费 / 无意义处理）
> 状态：**已确认 Q1-Q3（2026-09-06）**，待 Spec/coding 实施
> 作者：VOD Agent 代码追踪 + 开发者定界确认 + 本地代码复核
> 日期：2026-09-06
> 方向：**① 对 Webhook 事件做可配置的 action 级过滤（减无意义 agent 触发与 token 消耗）；② 构建分 Issue 阶段的事件分派模型（事件按 stage 路由到对应 Agent）**。

## 一、问题概述

GitCode 的 Webhook 事件只要属于 issue/PR（无论 action 是 `open`/`update`/`close`/`reopen`/`assign`/`label`/`edit`/`merge`），Grape 都会进入矩阵路由并触发 agent 处理。其中大量 action（责任人变更、贴标签、编辑正文、reopen 等）**对 agent 流水线无实质意义**，导致无意义处理和 token 浪费。

## 二、现状代码路径（复核确认）

### 2.1 事件归一化（`src/vcs/gitcode.py:normalize()`）

| Webhook | action | 归入事件类型 |
|---------|--------|--------------|
| Issue Hook | `open`/`create` | `issue_opened` |
| Issue Hook | 其他（update/close/reopen/assign/label/edit） | `issue_updated` |
| Note Hook | noteable=Issue | `issue_comment` ① |
| Note Hook | noteable=MergeRequest | `note_hook_pr` ① |
| Merge Request Hook | `merge` | `pull_request_merged` |
| Merge Request Hook | 非 merge（open/update/close/reopen） | `pull_request_opened` |

> ① **comment/PR 评论 = 独立事件类型（Note Hook），无 `action` 字段** → 不在 L1 action 过滤范围内（no_rule 放行），仅受 L2 阶段路由约束；评论作为需求澄清/变更入口必须保留触发。

**关键**：GitCode 的 Issue Hook 对 `assign`/`label`/`edit`/`reopen`/`close` 的 action **全部归一化为 `issue_updated`**，action 细节只保留在 `payload.object_attributes.action` / `payload.changes`。

### 2.2 处理触发（`src/api/event_pipeline.py:run_pipeline()`）

现有**唯一的 action 级过滤**：

```python
# 2.1 issue_updated 纯标签变更（changes.labels 有变化、其余内容未动）→ 跳过（L730-741）
if event_type == "issue_updated":
    _changes = payload.get("changes", {}) or {}
    _content_keys = [k for k in ("title", "description") if k in _changes]
    _label_changed = "labels" in _changes
    if _label_changed and not _content_keys:
        → skip (label_only_change)
```

除此之外：
- **`issue_updated` 其他所有 action 都继续走阶段矩阵**（`_resolve_issue_stage_agent`，L419）→ 有 issue_state 时路由到对应 Agent 并执行。
- **`action=close` 不丢弃**：虽触发 `close_issue_tasks()` 副作用（关闭任务），但事件本身继续路由（VOD 分析 L751-786 实为 3.x 任务关闭段，事件路由在 L745 之后仍进行）。
- **PR 的 `update`/`close`/`reopen`** 同理进 `pull_request_opened`/`note_hook_pr` 矩阵路由。

### 2.3 路由集合

- `ISSUE_EVENT_TYPES = {issue_opened, issue_updated, issue_comment, issue_labeled}`（routing.py:9）
- `PR_EVENT_TYPES = {pull_request_opened, pull_request_updated, pull_request_merged, note_hook_pr}`（routing.py:10）

## 三、定界确认（开发者已确认 Q1-Q3，2026-09-06）

### Q1：Issue 事件过滤范围（已确认，按真实 payload 修正）

> 此表针对 **Issue Hook**（`issue_opened`/`issue_updated`）的 action 过滤。`issue_comment` 来自 **Note Hook**（`object_kind: note`），是独立事件类型，见下方「comment 事件」。
> ⚠️ **2026-09-07 修正**：GitCode 真实 payload 中 `assign`/`label`/`edit` 均以 **`action='update'` + `changes` 表达**，不存在 `action='assign'/'label'/'edit'`。下表按实际动作语义过滤，实现时通过 `action + changes` 组合判定（§4.1）。

| 实际动作（payload 表达） | 是否触发 Grape | 理由 |
|-------------------------|---------------|------|
| `open`（action='open'） | ✅ 保留 | 新需求审视（issue_opened → VOD） |
| `close`（action='close'） | ⚠️ 保留副作用 | **仅关闭任务（`close_issue_tasks`），无需 agent 执行** |
| 责任人变更（update + changes.assignees） | ❌ 过滤 | 无流水线意义 |
| 标签变更（update + changes.labels） | ❌ 过滤 | 已有 label-only 跳过，进一步全量过滤 |
| 正文编辑（update + changes.description） | ✅ **保留** | **需求补充/变更需处理**；仅 description 实质变更才有效 |
| 标题编辑（update + changes.title） | ✅ 保留 | 需求标题调整视为有效 |
| `reopen`（action='reopen'） | ❌ 过滤 | 无流水线意义 |

**comment 事件（独立于上表的 action）**：

| 事件类型 | 是否触发 Grape | 说明 |
|----------|---------------|------|
| `issue_comment`（Note Hook） | ✅ **保留** | **评论是需求澄清/变更的重要入口**：用户/开发者对 Issue 的评论必须触发对应 Agent（L2 按 stage 路由到 vod/issue）。不做 action 过滤（Note Hook 无 action 字段） |
| `note_hook_pr`（PR 评论） | ✅ **保留** | PR 讨论/QA 评论需触发审查（L2 按 stage 路由）。不做 action 过滤 |

> **comment 的过滤只发生在既有层面**：① self-comment（bot 回声，`<!-- grape-auto-comment -->` 标记）跳过；② label_only 之类不适用注释场景。comment 本身 **不在 `_EVENT_ACTION_RULE` 过滤**（无 action 键 → no_rule 放行），仅受 L2 阶段路由约束。

> **edit 的细粒度**：`update` 时 `changes` 有 `description` → 有效（重新审视需求）；仅 assignees/labels/updated_at → 过滤；同时有 description 与 assignees → 内容变更优先放行。

### Q2：PR 事件是否同步过滤（已确认）

| action | 是否触发 | 说明 |
|--------|---------|------|
| `open` | ✅ 保留 | 新 PR 审查 |
| `merge` | ✅ 保留 | 合并触发 QA→SRE 流水线 |
| `update` | **按操作者细分** | **机器人自己操作的 update → 不需要处理（过滤）**；**用户操作且涉及 PR 代码变更（如新 commit）→ 需要处理**（重新审查） |
| `close` | ❌ 过滤 | 无流水线意义 |

> **update 细粒度**：通过 payload 的 push 来源/提交数判断是否"有新的 commit 由用户推送"；机器人 force-push/同步操作不计。

### Q3：过滤规则形态（已确认 → **采用可配置**）

**选定 B. 可配置**：用 DB 表维护「事件 action / 操作者 / changes 条件」白名单，便于后续调整，不硬编码。

### 补充确认：self-comment 概念

- 用户确认：**`self-comment` 表示这批 webhook 事件是由机器人（grape_bot）自己发起/触发的**（如 bot 打标签、bot 评论、bot 合并 PR 带来的回声）。
- 现状：`run_pipeline` L2.0（L690-714）已对 actor∈bot_usernames 的**所有事件**跳过（含 issue_opened 的特殊豁免）。即 bot 自己动作引起的 webhook 回声，目前基本不进流水线。
- **与本次过滤的关系**：self-comment（bot 回声）与 action 过滤是**两个不同维度**——self-comment 是"谁发起的"（bot→跳过），action 过滤是"什么动作"（assign/edit/reopen→跳过）。设计上：**self-comment 判定在前（bot 回声直接弃），action/changes 过滤在后（用户操作但无意义动作）**。

## 四、优化方案（组合 L1+L2，落地实现）

### 4.1 核心：run_pipeline 新增「事件准入过滤 + 按操作者细分」两层前置（2.2a）

在现有 `self-comment`（bot 回声，L690-714）检测之后、路由判定之前，插入统一过滤：

```python
# 2.2a 无意义 action 过滤（Issue #49，按真实 payload：action + changes 组合判定）
_EVENT_ACTION_RULE = load_event_action_rule()   # 从 DB event_action_filter 读取（Q3=可配置）
# 结构：{event_type: {action: {allow, actor, require_changes, block_changes, handle_side_effect}}}
#  - require_changes: 命中这些 changes 键才放行（如 description 编辑）
#  - block_changes:   命中这些 changes 键即过滤（如 assignees/labels 变更）
_skip_reason = ""
if event_type in _EVENT_ACTION_RULE:
    try:
        _oa = payload.get("object_attributes", {}) or {}
        _act = str(_oa.get("action", "") or "").lower()
        _rule = _EVENT_ACTION_RULE[event_type].get(_act)
        if _rule is not None:
            _changes = (payload.get("changes") or {}) if isinstance(payload, dict) else {}
            _act_ok = bool(_rule.get("allow", False))
            _blocked = _rule.get("block_changes") or []
            # 1) block_changes 命中即拦（assignees/labels 变更）
            if _blocked and any(k in _changes for k in _blocked):
                _skip_reason = f"block_changes:{_act}"
            # 2) require_changes：需命中指定键才放行
            elif _rule.get("require_changes"):
                _req = _rule["require_changes"]
                if not (isinstance(_changes, dict) and all(k in _changes for k in _req)):
                    _skip_reason = f"changes_not_met:{_act}"
            # 3) 未跳过但 allow=0
            if not _skip_reason and not _act_ok:
                _skip_reason = f"action_not_allowed:{_act}"
    except Exception:
        pass
if _skip_reason:
    log.info(f"Pipeline skip: {_skip_reason} (issue #49 filter)")
    from src.api.event_store import append_skipped_event
    append_skipped_event(repo_key, event_type, _skip_reason)
    return result
```

**默认规则（按真实 payload 审计后重写，写入 DB `event_action_filter`）**：

> **审计结论（2026-09-07 生产样本，53 条 issue_updated）**：
> - `issue_updated.action` 实际只出现 `update`/`close`/`reopen`（**没有 `assign`/`label`/`edit`/`open` 这类 action**）
> - `assign` 变更 = `update` + `changes.assignees`；`label` 变更 = `update` + `changes.labels`；内容编辑 = `update` + `changes.description`/`title`
> - `issue_comment`/`note_hook_pr` 无 `action`（靠 note + noteable_type）→ 规则表外
> - 因此 **判定需基于 action + changes 组合，不复依赖假想的 action 名**

| event_type | action | allow | 判定依据 | 说明 |
|------------|--------|-------|----------|------|
| `issue_updated` | `update` | ✅ | `require_changes: [description]`，`block_changes: [assignees, labels]` | 内容编辑放行；**assign/label 变更过滤**（若同时有 description 变更则放行） |
| `issue_updated` | `update` | ✅ | `require_changes: [title]`，`block_changes: [assignees, labels]` | 标题编辑（可选独立行） |
| `issue_updated` | `close` | ⚠️ 副作用-only | — | 任务关闭副作用，不进入 agent |
| `issue_updated` | `reopen` | ❌ | — | 无流水线意义 |
| `pull_request_opened` | `open` | ✅ | — | 新 PR 审查 |
| `pull_request_opened` | `update` | ⚠️ 按操作者 | actor=user → allow；bot → skip | 新 commit 审查 |
| `pull_request_opened` | `close`/`reopen` | ❌ | — | 无流水线意义 |
| `pull_request_merged` | `merge` | ✅ | — | 合并触发 QA→SRE |

> **assign/label 过滤实现**：真实 action 是 `update`，故用 **`block_changes: [assignees]` / `block_changes: [labels]`** 判定，不再依赖 `action=assign`。若 changes 同时含 description，说明"改内容顺带换人"，允许放行（取内容变更优先）。
> **comment/note_hook_pr**：无 action → 规则表外（no_rule 放行），仅 L2 阶段路由 + self-comment 拦截。

- **位置**：紧挨现有 2.1 `label-only` 检测，保持过滤逻辑集中。
- **close 语义（Q1 已确认）**：仅关闭任务副作用（`close_issue_tasks`），**不路由到任何 agent 执行**——事件虽被"准入"（走副作用路径），但直接短路，不触发流水线 agent。
- **可配置化（Q3 已确认）**：规则存 DB 表 `event_action_filter`（event_type / action / allow / actor / require_changes / priority / handle_side_effect），读取方式与现有 `devops_pipeline_scene` 类似；配置变更无需重启热生效。

### 4.2 忽略事件也要有据可查

- 被过滤的事件写入 `event_skipped`（reason 带 action），前端 events 页可看到"因 action 过滤被跳过"，便于回溯。
- 目标：**大幅度减少 `issue_updated` 的空转事件落库与 agent 触发**。

### 4.3 与既有过滤的关系（不重复）

- 保留现有 `label_only_change`（L730-741）作为"纯标签变更"兜底；
- 新增 action 过滤器更普适（assign/edit/reopen 全覆盖）；
- 二者不冲突（label-only 理论上被新过滤器覆盖，可保留双保险或复核后去重）。

## 五、分 Issue 阶段 Webhook 事件处理模型（本次补充的核心框架）

> 需求：不仅是"过滤无意义 action"，而是**把"webhook 事件到达 → 按 Issue 所处阶段 → 分派到对应 Agent"这套逻辑显式化、模型化**，让每个事件都有清晰、可推导的处理归属。

### 5.1 现有机制（已存在的雏形，复核确认）

**阶段状态机**（`issue_state.stage`，顺序 `vod → issue → coding → qa → sre → done`）：

| 阶段 | owner_agent | 该阶段的职责 | 推进方式 |
|------|-------------|--------------|----------|
| `vod` | vod | 需求收敛 / 场景选择 | VOD 审视后 A2A 交办 |
| `issue` | issue | Spec 设计 / 打标签 / 分配 / A2A | Issue Agent Spec 后 A2A 交办 |
| `coding` | coding | 代码实现 / PR | Coding Agent 完成后 A2A QA |
| `qa` | qa | 代码审查 / 合并 | QA 审查后 A2A SRE |
| `sre` | sre | 部署 / 发布 | SRE 完成后 → done |
| `done` | — | 已结束 | 不再分发 |

**现有路由函数 `_resolve_issue_stage_agent(repo_key, event_type, payload, default)`**（event_pipeline.py:419-477）已经是"按阶段分派"的核心但**逻辑分散、不完整**：

```
Issue 类事件（issue_opened/updated/comment/labeled）：
  issued stage=vod     → vod
  issued stage=issue+  → issue（coding/qa/sre 不直接处理 Issue 类事件）
  issued stage=done    → 不分发
PR 类事件（opened/updated/merged/note_hook_pr）：
  issued stage=vod/issue → issue（审视）
  issued stage=coding + PR updated + QA有历史 → qa（复审）
  issued stage=coding/qa/sre → 对应执行 Agent
  issued stage=done    → 不分发
```

**阶段推进**：A2A 交办时 `set_issue_stage(repo_key, issue_id, agent_name, ...)`（webhook.py:416-419），并带**回退检测**（`_target_idx < _cur_idx` 时取消下游任务，webhook.py:344-411）。

### 5.2 现有模型的问题

1. **路由逻辑嵌入在 `event_pipeline.run_pipeline` 内联**（L745-785 + `_resolve_issue_stage_agent`），**action 过滤（§四）与阶段路由是两段独立代码**，模型未显式化。
2. `issue_updated` 的**无意义 action（assign/edit/reopen）**在进入阶段路由前没有被过滤 → 即使阶段路由正确，也大量空转。
3. 阶段路由**只认"stage"，不认识"action 是否该触发该 stage 的 agent"**——例如 stage=issue 时收到 `assign` 变更也会路由到 Issue Agent，但 Assign 本不该触发 Spec 重做。
4. 无统一的事件处理模型文档/表驱动，阶段归属靠 if/else 内联，扩展（新增 stage/事件类型）需改代码。

### 5.3 目标模型（事件处理 = 三层流水线）

把 webhook 事件处理归纳为**确定性三层通道**，每个事件按顺序经过：

```
Webhook 到达
  │
  ├─ L1 事件准入过滤（§四，可配置 action+changes 判定）：无意义变化 / 纯 metadata 变更 → event_skipped，
  │     不进入 L2/L3（assign/label 变更（update+assignees/labels）与 reopen 丢弃；
  │     close 走副作用短路；description/title 编辑、open 放行；PR update 按操作者：user+新 commit 有效，bot 丢弃）
  │
  ├─ L2 阶段归属分派（分阶段模型，本节核心）：
  │     读 issue_state.stage → 查「阶段 × 事件类型 → Agent」映射表 → 确定目标 Agent
  │     （映射不存在/无记录 → 按 default 兜底）
  │
  └─ L3 触发执行：目标 Agent 启动（沿用现有 enqueue/dispatch）
```

**阶段分派映射表（显式化，替代内联 if/else）**：

| 事件类型 | stage=vod | stage=issue | stage=coding | stage=qa | stage=sre | stage=done |
|----------|-----------|-------------|--------------|----------|-----------|------------|
| `issue_opened` | **vod**（审视/建阶段） | —（新建→vod） | — | — | — | 不分发 |
| `issue_updated`(open/edit·已过L1) | vod | issue | issue | issue | issue | 不分发 |
| `issue_updated`(close) | ⚡副作用-only² | ⚡副作用-only² | ⚡副作用-only² | ⚡副作用-only² | ⚡副作用-only² | 不分发 |
| `issue_comment` | vod（澄清对话） | issue | issue³ | issue³ | issue³ | 不分发 |
| `pull_request_opened` | issue | issue | coding | qa | sre | 不分发 |
| `pull_request_updated` | issue | issue | coding/qa⁴ | qa | sre | 不分发 |
| `pull_request_merged` | issue | issue | coding | qa | sre | 不分发 |
| `note_hook_pr` | issue | issue | coding | qa | sre | 不分发 |

> ² **close = 副作用-only，不进入任何 Agent 执行**：仅处理任务关闭（`close_issue_tasks`），管道直接短路，不触发流水线。各阶段均不分派。
> ³ **Issue 类事件（comment/labeled；updated 的 open/edit 动作）在非 done 阶段均归 `issue`**——coding/qa/sre 阶段仍可能产生需求补充/变更的评论/编辑，需 Issue Agent 审视，不可丢弃（Q1 意图，回归修复 2026-09-07）。
> ⁴ PR updated + stage=coding + QA 有历史 → qa 复审；否则 coding。

### 5.4 落地方式（与 §四 结合）

1. **L1 = §4.1 的命中规则过滤器**（从 DB `event_action_filter` 读取，Q3 可配置），先行过滤无意义 action；`handle_side_effect` 命中（如 close）→ 执行副作用后**直接短路返回，不进入 L2/L3**。
2. **L2 = 将 `_resolve_issue_stage_agent` 重构为「映射表驱动」**：
   - 定义常量 `STAGE_EVENT_AGENT_MAP`（上表），函数查表返回目标 Agent，取代散落的 if/else；
   - 保留现有特例（issue→issue、PR updated+coding+QA 历史→qa）；
   - 无 stage / 无记录 → 走 default 兜底（现状语义）；
   - `done` → 一律不分发。
3. **L3 不变**：沿用现有 enqueue / broadcast dispatch。
4. **可观测**：L1/L2 决策写入 `event_skipped`（action 过滤，reason 带 action + actor）或 `events`（正常分发时带 stage 归属），前端可见"事件 → stage → agent"链路。

### 5.5 收益

- **事件不再盲目触发**：action 越过 L1 才算"有效事件"；有效事件再按 stage 精确分派。
- **归属可推导**：无需读代码即知"某 stage 下某事件该给谁"。
- **零盲目 A2A/执行**：`assign`/`label`/`reopen` 不触发任何 agent；无效 `edit`（无需求变更）不触发；bot 的 `update` 不回环；`issue` stage 的 Issue 事件只给 issue，不误触 coding/qa/sre。
- **扩展友好**：新增 stage/事件类型只需改映射表；过滤规则写入 DB 可热调整，无需重启。

## 六、改动清单

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/api/db_setup.py` | 新增 `event_action_filter` 表（event_type/action/allow/actor/require_changes/priority）+ 默认规则 seed | P0 |
| 2 | 新增 `src/api/event_rule.py` | `load_event_action_rule()` 读取 + 命中判定（含 actor/changes 细化）；带缓存 | P0 |
| 3 | `src/api/event_pipeline.py` | L1：2.2a 接入事件规则过滤器（调 `event_rule`） | P0 |
| 4 | `src/api/event_pipeline.py` `_resolve_issue_stage_agent` | L2：重构为 `STAGE_EVENT_AGENT_MAP` 表驱动（§5.4），保留特例/兜底 | P0 |
| 5 | `src/api/event_pipeline.py` run_pipeline | L1→L2 串联（先 action 过滤，再阶段分派） | P0 |
| 6 | `src/vcs/gitcode.py` normalize | （可选）保留 action/操作者到 evt 顶层字段，便于规则读取 | P2 |
| 7 | `src/api/routes/settings.py`（或新管理页） | event_action_filter 的查看/编辑 API（Q3 可配置的运维入口） | P1 |
| 8 | `tests/` | 规则命中 + 阶段分派单测：assign 跳过 / edit 无 desc 跳过 / PR update 按操作者 / open 按 stage 分派 / done 不分发 / PR updated 复审 | P1 |
| 9 | 文档 | 本方案 + Q1-Q3 结论 + self-comment 概念回写 | P1 |

## 七、验收标准

- [ ] L1：`issue_updated` 的 **assign（update+changes.assignees）/ label（update+changes.labels）/ reopen** 变化不再触发 agent（event_skipped 带 reason）
- [ ] **L1：`close` 仅执行任务关闭副作用（`close_issue_tasks`），不路由任何 agent 执行（各阶段均不分派）**
- [ ] L1：`update` 仅当 `changes.description`/`title` 变更时触发（需求补充/变更被保留）；纯 assignees/labels/updated_at 变更被过滤
- [ ] L1：PR `update` 按操作者——user 且有新 commit → 触发审查；bot 操作 → 过滤不回环
- [ ] L2：同一 `issue_comment`/`PR updated` 在不同 stage 下路由到对应 Agent（vod/issue/coding/qa/sre/done 各验证）
- [ ] L2：`issue` 阶段的 Issue 事件只给 issue，不误触 coding/qa/sre（现状语义保持）
- [ ] stage=done 的事件一律不分发
- [ ] 过滤规则存 DB `event_action_filter`，可配置热生效（改配置后无需重启即影响后续事件）
- [ ] self-comment（bot 回声）与 action 过滤两条前置正常共存：bot 事件先被 self-comment 拦截，用户事件再走 action/阶段规则
- [ ] 过滤/跳过得有据可查（event_skipped reason 含 action + actor）
- [ ] 现有 webhook/onboarding/任务相关测试全绿 + 新增用例通过

## 八、风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| filter 规则误配（如误关 open/edit） | 高 | `event_action_filter` 配置化 + 管理端可查看/回滚；默认 seed 保守（关键 action 不放行默认 min） |
| `edit` 不错判（需求补充被过滤 / metadata 编辑误触发） | 中 | 靠 `require_changes: [description]` 精确判定；单测覆盖两种场景 |
| PR `update` 按操作者判定依据（push 来源）不可靠 | 中 | 用 payload 的提交数/actor 判断；若 GitCode 不提供，退化为"有 commits 且有非 bot actor" |
| 阶段映射表重构引入回归（原 if/else 行为偏差） | 中 | 映射表逐格对照现有 `_resolve_issue_stage_agent` 行为；补充阶段×事件组合单测 |
| 规则表与既有 label-only 过滤重复 | 低 | 二选一或复核去重（label 无意义，统一由规则表覆盖） |
| 场景级特殊诉求（自定义场景依赖 issue_updated） | 低 | 规则表可加场景/仓库级覆盖（预留 scope 字段） |

## 九、处理流水线状态

- ✅ VOD Agent：已完成代码追踪（3 事件路径确认）并结构化总结
- ✅ **开发者已确认 Q1-Q3**（2026-09-06）：edit 保留（需求变更需处理）/ PR update 按操作者细分 / 采用可配置；并确认 self-comment = bot 自发起事件
- ✅ **2026-09-07 真实 payload 审计修正**：初版规则含假想的 `action=assign/label/edit`；实证 GitCode 统一用 `action=update` + `changes.assignees/labels/description/title` 表达 → 判定改为 **action + changes 组合**（图 §4.1 已重写）。生产时间线：hello-calculator-10 assign 变更（seq 2071，action=update）证实此前 assign 未被过滤的根因
- ⏳ coding 实施（按审计后规则：L1 用 block_changes 拦 assign/label，L2 阶段模型，配置入口）

## 附：关键代码位置

- 事件归一化（action→event_type）：`src/vcs/gitcode.py` `GitCodeBackend.normalize` L11-89
- 处理入口 + label-only 过滤：`src/api/event_pipeline.py` `run_pipeline` L717-743
- 阶段矩阵路由（L2 待重构点）：`src/api/event_pipeline.py` `_resolve_issue_stage_agent` L419-477；路由接入 `run_pipeline` L745-785
- 事件类型集合：`src/api/routing.py` L9-10（`ISSUE_EVENT_TYPES`/`PR_EVENT_TYPES`）
- 阶段状态读写：`src/api/session_store.py` `get_issue_stage`(L42) / `set_issue_stage`(L62)
- 阶段推进 + 回退检测：`src/api/routes/webhook.py` A2A 段 L321-421（`_stage_order`/`_target_idx < _cur_idx` 回退取消）
- 阶段顺序常量：`src/api/routes/pipeline.py` `AGENT_ORDER` L8
- 阶段矩阵设计依据：`docs/new-version1/vod-skill-upgrade-plan.md` §3.0.3（D8/D9）