# 事件路由：阶段矩阵 + 订阅机制现状与双路径优化方案（event-routing-subscription-design.md）

> 版本：v1.0
> 日期：2026-08-18
> 状态：待评审（未实施）
> 触发背景：Jason7602/hello-html2 PR Merged（#1745 `pull_request_merged`→qa、#1746 `issue_updated`→issue）同步关闭 Issue，SRE 配置了 `pull_request_merged` 订阅却未收到事件。
> 根因：PR 事件按 **issue_state 阶段矩阵**路由到 qa；SRE 的 `subscribe_events` 仅在**主路由为空**时兜底，未作为"主动关注"触发分发。

---

## 一、现状方案

### 1.1 路由分层（routing.py + event_pipeline.py）

```
webhook 事件 → normalize_event（归一化）
            → run_pipeline（event_pipeline.py:251）
               ├── ① 自循环过滤（actor 为 bot / issue_updated 纯标签）
               ├── ② 路由判定（event_pipeline.py:313-336）
               │     ├── issue_opened ──────────────► vod（D8，恒）
               │     ├── Issue 类事件 ──────────────► 阶段矩阵 _resolve_issue_stage_agent
               │     ├── PR 类事件 ─────────────────► 阶段矩阵（PR_DEFAULT_AGENT="" 作 _default）
               │     └── 非 Issue 事件 ─────────────► 静态映射 NON_ISSUE_EVENT_ROUTE
               │     └── 主路由为空时 ──────────────► 订阅兜底 _match_repo_subscription
               ├── ③ Agent 启用检查（is_agent_enabled）
               ├── ④ 会话/技能查找
               ├── ⑤ append_event（创建事件，status=running）
               └── ⑥ 执行分派（pick_engine → direct / queue_remote）
```

### 1.2 阶段矩阵（`_resolve_issue_stage_agent`，event_pipeline.py:136-180）

**判定**：有关联 issue_id 的事件按 `issue_state.stage` 归属：

```
PR 类事件（pull_request_opened/updated/merged, note_hook_pr）:
  stage = vod | issue  → "issue"（Issue Agent 审视）
  stage = coding       → "coding"
  stage = qa           → "qa"        ← #1745 落此
  stage = sre          → "sre"
  stage = done         → ""（不再分发）

Issue 类事件（issue_opened/updated/comment/labeled）:
  stage = vod → "vod"; 其余 → "issue"; done → ""
```

**关键**：PR 事件路由**完全由 issue_state 当前阶段决定**，与"谁订阅了该事件"无关。

### 1.3 订阅机制（`_match_repo_subscription`，event_pipeline.py:213-248）

- 读 `devops_agent_session.subscribe_events`（JSON 列表 `[{event_type, branch}]`）
- 匹配 `event_type` 且（branch 空=不限 / 等于事件分支）
- **仅在主路由结果为空时调用**（`if not agent_type:`，event_pipeline.py:321-330）
- 命中 → 返回订阅的 agent_type，作为兜底归属

### 1.4 现状缺陷

| 缺陷 | 说明 |
|------|------|
| **订阅只兜底，不触发** | SRE 订阅 `pull_request_merged`，但主路由（阶段矩阵）=qa 非空 → 订阅分支不执行 → SRE 收不到 |
| **阶段矩阵单归属** | 同一事件只能路由到 1 个 agent，无法表达"QA 收尾 + SRE 部署"的双关注 |
| **订阅语义弱化** | 注释（routing.py:4）"订阅声明不再作为路由依据"——订阅仅前端展示/兜底，与"主动关注某类事件"的直觉不符 |

---

## 二、优化方案：阶段矩阵 + 订阅双路径并行

### 2.1 目标语义

同一 webhook 事件到达时：
1. **阶段矩阵** → 归属主 Agent（qa），正常收尾（现有流程不变）
2. **订阅匹配** → 命中的订阅 Agent（sre）**额外**触发，独立建事件 + 独立执行

即：**两条路径都走，互不冲突**——qa 收尾合并、sre 响应订阅做发布/部署。

### 2.2 改动点（run_pipeline）

在阶段矩阵路由（event_pipeline.py:313-319）与订阅兜底（:321-330）之后，**新增"订阅并行分发"分支**：

```python
# ① 主路由（阶段矩阵）— 现有逻辑，结果 agent_type（如 qa）
agent_type = _resolve_issue_stage_agent(...)

# ② 订阅并行分发（新增）：主路由完成后，额外匹配订阅命中
if event_type in ("pull_request_merged",):          # 先限定 PR 合入事件
    _pr_branch = resolve_pr_target_branch(evt)
    _sub_agent = _match_repo_subscription(repo_key, event_type, _pr_branch)
    if _sub_agent and _sub_agent != agent_type:
        _dispatch_subscription(evt, repo_key, _sub_agent, issue_id, ...)  # 独立事件 + 分派
```

`_dispatch_subscription` 复用现有链路：`append_event`（agent_type=订阅 Agent）→ 启用检查 → 会话/技能 → 执行分派，**但不改 `issue_state.stage`**（阶段仍由主路径推进）。

### 2.3 关键设计决策

| 决策 | 方案 | 理由 |
|------|------|------|
| **触发范围** | 先仅 `pull_request_merged` | 用户当前关注点；避免订阅范围扩大引发意外双事件 |
| **事件生成** | 订阅 Agent 独立 event（新 seq）| 各自审计、各自状态，互不干扰 |
| **阶段不推进** | 订阅 Agent 不 `set_issue_stage` | 阶段状态机保持主路径单一推进，防混乱 |
| **去重** | `_sub_agent == agent_type` 时跳过 | 阶段矩阵已归该 Agent（如 stage=sre + sre 订阅），不重复 |
| **启用检查** | 订阅 Agent 也过 `is_agent_enabled` | enable=0 的 Agent 不触发 |
| **会话/目录** | 复用 issue 级会话与 workdir（`issues/{n}/{agent}`）| 与主路径一致的执行上下文 |
| **A2A 事件** | 不走双路径 | 仅 webhook 事件（PR/webhook）触发订阅分发 |

### 2.4 执行链路复用

订阅 Agent 分派复用现有 `_enqueue_pipeline_task` / `_execute_agent`（engine-selection v2 `pick_engine`），与主路径完全一致——无需新执行机制，仅多一次事件创建 + 分派调用。

### 2.5 验证

| 用例 | 步骤 | 断言 |
|------|------|------|
| PR Merged + SRE 订阅 | 配置 SRE `subscribe_events=[pull_request_merged]`，PR 合入 | 生成 2 个事件：主路径（qa）+ 订阅（sre）；sre 任务正常执行 |
| 阶段矩阵归 sre | stage=sre 时 PR Merged | 仅 1 个事件（sre），不重复（去重） |
| SRE 未订阅 | 无订阅配置 | 仅主路径事件（现状行为不变） |
| SRE 禁用 | enable=0 | 订阅命中但跳过（事件记录 skipped agent_disabled）|
| 阶段不推进 | 订阅 sre 执行后 | `issue_state.stage` 不变（仍由主路径推进）|

### 2.6 订阅感知 A2A 去重（方案 B，服务端 a2a_endpoint 判断）

#### 2.6.1 目标语义

QA 收到 `pull_request_merged` 后处理完 → **A2A 交办 SRE**。服务端 A2A 端点判断目标 SRE 的 `subscribe_events`**非空**（即 SRE 已配置任何订阅）→ **只推进 `issue_state.stage=sre`，不创建任务**。理由：SRE 配置了订阅，说明它自己会从 webhook 收到关注的事件，QA 的 A2A 交办是冗余的。

> **副作用（可接受）**：若将来有"非 webhook 驱动的 A2A 交办 SRE"（如 QA 要求 SRE 做某操作，无对应 webhook 事件），也会被误去重。当前业务中 SRE 的所有 A2A 交办均源自 webhook 事件链（PR Merged→QA→SRE），该副作用无实际影响。

#### 2.6.2 改动点（`src/api/routes/webhook.py` a2a_endpoint）

在 :182（stage 推进）之后、:184（创建 event/分派任务）之前，插入订阅检查：

```python
# 订阅感知 A2A 去重：若目标 SRE 已配置任何订阅 → 只推进 stage，不建任务
# （SRE 会从 webhook 订阅路径收到关注的事件，A2A 交办是冗余的）
if agent_name == "sre":
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT subscribe_events FROM devops_agent_session WHERE repo_key=? AND agent_type='sre'",
                (repo_key,))
            row = c.fetchone()
            if row:
                _raw = (row[0] or "").strip()
                if _raw and _raw != "[]" and _raw != "null":
                    log.info(f"A2A: sre subscribed (type={event_type}), skip task, only advance stage")
                    return {"status": "ok", "seq": 0, "agent": agent_name, "skipped": "sre_subscribed"}
    except Exception:
        pass
```

> 判断依据：`subscribe_events` 非空且非 `"[]"` / `"null"` → SRE 已配置订阅 → 只推进 stage（:175-182 已在前面执行），不创建 event/分派任务。无订阅配置时走原有 A2A 流程（正常创建任务）。

#### 2.6.3 验证（补充）

| 用例 | 步骤 | 断言 |
|------|------|------|
| A2A→SRE，SRE 有订阅 | QA 调 A2A 交办 SRE，SRE `subscribe_events` 非空 | stage 推进到 sre，**无新事件/任务**（seq=0, skipped=sre_subscribed） |
| A2A→SRE，SRE 无订阅 | QA 调 A2A 交办 SRE，SRE 订阅为空/`[]` | stage 推进 + 正常创建 a2a_event + 分派任务 |
| A2A→coding，coding 有订阅 | QA 调 A2A 交办 coding | **不受影响**（去重仅对 sre），正常创建事件/任务 |

---

## 三、备选方案（对比）

| 方案 | 语义 | 优劣 |
|------|------|------|
| **A 双路径并行（推荐）** | 阶段矩阵归属 + 订阅额外触发 | 两者兼顾；事件翻倍需评估；改动集中在 run_pipeline |
| B 订阅优先 | 订阅命中则替代阶段矩阵 | 简单；但可能跳过 qa 正常收尾 |
| C 维持现状 | 订阅仅兜底 | 不改；SRE 订阅形同虚设（当前缺陷） |

**推荐 A**：符合"阶段矩阵管流程归属 + 订阅管主动关注"的双语义，且对现有流程零破坏（主路径不变）。

---

## 四、结论

- **现状**：PR 事件按 issue_state 阶段矩阵单归属；SRE 订阅仅在主路由为空时兜底，无法"主动关注"。
- **优化**：`pull_request_merged` 走双路径——阶段矩阵归 qa 收尾，订阅命中额外触发 sre，独立事件、独立执行、不改阶段。
- **待确认**：① 触发范围是否仅限 `pull_request_merged`；② 订阅触发的 sre 具体职责（发布/部署/健康检查）；③ 是否需前端在订阅配置处提示"双路径"语义。

*本文为现状梳理与方案设计，待评审后实施。*
