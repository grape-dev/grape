# Agent 事件订阅（仓库级可配置）设计方案

> 版本：v1.0
> 日期：2026-08-12
> 状态：待评审
> 需求：SRE Agent 当前没有任何事件触发。希望在 **SRE Agent 页面**可勾选监听事件，主要包含「PR 合入」及「合入的分支」；一旦这些事件触发，SRE Agent 就收到对应任务。**默认不配置**（不订阅时行为与现状完全一致）。

---

## 一、需求梳理

| # | 需求 | 落点 |
|---|------|------|
| 1 | SRE Agent 页面可配置「监听事件」：勾选 `PR 合入`（`pull_request_merged`），并可限定「合入的分支」白名单 | 前端 Agent 配置面板 + 后端 API |
| 2 | 一旦订阅的事件被触发（PR 合入到命中分支），SRE Agent 自动收到任务（走现有 pipeline 建 event + 执行链路） | `event_pipeline.run_pipeline` 路由 |
| 3 | 默认不配置：不勾选时 SRE 行为与现状完全一致（普通 PR 合入仍不触发） | 数据默认值 + 路由判定 |
| 4 | 订阅是**仓库级**配置（Agent 页面本就按仓库维度），不同仓库可各自配置 | 存储落在 `devops_agent_session` 或独立表 |

### 语义澄清

- **订阅 ≠ 全局 enable**：`enable` 决定「这个 Agent 是否启用」；`subscribe_events` 决定「哪些事件触发它」。两者独立，订阅命中后仍需通过现有 `enable` 检查（与现状其他 Agent 一致）。
- **「合入的分支」含义**：PR 合并的**目标分支**（GitCode `object_attributes.target_branch` / GitHub `pull_request.base.ref`），通常为 `main`/`master`。语义为「当 PR 合入到该分支时触发 SRE」。
- **默认空**：`subscribe_events = []` 表示不订阅，事件行为与现在完全相同（回归面为零）。

---

## 二、现状分析（为什么 SRE 收不到事件）

### 2.1 `agent-description.json:54` 的 `subscribe_events` 已是历史遗留

- 5 个 Agent 全为 `[]`；路由逻辑**已不再读它**：
  - `src/api/routing.py:1/32` —— 「替代 agent_type.subscribe_events 单级路由」
  - `src/api/event_pipeline.py:235` —— 「D9 阶段矩阵 + 非 Issue 静态映射，替代 agent_type.subscribe_events」
- 该字段仅 seed 进 DB `agent_type.subscribe_events` 列（`db_setup.py:584`）供展示；`tests/test_qa_skill.py:101` 还强制断言全空。
- 结论：**不能直接往 `configs/agent-description.json` 的 `subscribe_events` 里塞事件**——那既是全局粒度、又不被路由读取。

### 2.2 SRE 目前的触发路径

| 路径 | 事件 | 现状 |
|---|---|---|
| 非 Issue 静态映射 `NON_ISSUE_EVENT_ROUTE`（`routing.py:20-28`） | `release_created` / `tag_created` / `build_start` / `build_failure` / `deploy_start` / `deploy_failure` → sre | 仅当 GitCode 发来对应 webhook 才触发；普通 PR 流程不产生 |
| 阶段矩阵 `_resolve_issue_stage_agent`（`event_pipeline.py:124-168`） | PR 类事件 `pull_request_merged` 等 → 仅当关联 issue 的 `issue_state.stage == "sre"` | 需 grape A2A 链路把 Issue 推到 sre 阶段；普通 PR 无该链路 |
| 默认值 | `PR_DEFAULT_AGENT = ""`（`routing.py:15`） | 无关联 issue / 非 grape 开发的 PR 一律 `no_route` 跳过 |

- 另：`sre` 全局 `enable: false`（`agent-description.json:55`），即使路由命中也会被 `is_agent_enabled` 拦截（`event_pipeline.py:277-282`）。

**结论**：普通 PR 合入（`pull_request_merged`）因默认路由为空，SRE 收不到任何事件 → 与需求「当前 SRE Agent 没有任何事件触发」一致。

### 2.3 页面与 API 现状

- Agent 页面 Configure 面板只有 Backend / Skill（+ VOD IM 专属），无订阅 UI（`src/api/static/js/pages/agents.js:99-130`）。
- `PUT /api/agents/{name}/{owner}/{repo}` 的 `AgentConfig`（`src/api/routes/agents.py:199-207`）只有 `backend/skill/skill_scope/enable/im_*`，无订阅字段。
- 页面是 **repo 级**（`devops_agent_session`），而 `subscribe_events` 是**全局** `agent_type` 级 → 粒度不匹配，需要 repo 级存储。

### 2.4 合入分支信息现状

- `src/vcs/gitcode.py:68-71` 只对 push 事件提取 `branch`（顶层 `ref`）。
- PR merge 的目标分支在 payload：
  - GitCode：`object_attributes.target_branch`
  - GitHub：`pull_request.base.ref`
- 均未提取到内部 `evt["branch"]`，需补充。

---

## 三、设计目标与原则

1. **默认零行为变化**：不配置订阅时，pipeline 判定与现状逐字节一致。
2. **repo 级、可覆盖**：订阅存在仓库级，每个仓库可独立勾选。
3. **复用现有执行链路**：订阅命中后走 `run_pipeline` 现有的 建 event → 找 skill → 执行 全流程，不另起通道。
4. **订阅命中有优先级**：仓库级订阅仅在「现有路由为空」时兜底生效，不破坏阶段矩阵判定（阶段矩阵命中 `sre` 的 PR 合入仍按原逻辑走）。

---

## 四、数据模型变更

### 4.1 方案选择：`devops_agent_session` 加列（推荐）

Agent 页面是 repo 级会话维度，配置随会话存在最自然，且 `get_agent_detail` 已返回该行。

```sql
ALTER TABLE devops_agent_session ADD COLUMN subscribe_events TEXT DEFAULT '[]';
```

- 存 JSON 数组，元素结构：`{"event_type": "pull_request_merged", "branch": "main"}`。
- `branch` 为空串表示「不限分支」。
- 与全局 `agent_type.subscribe_events` **并存但独立**：全局列不动（保留兼容 + 现有测试），仓库列是本功能实际读取源。

> 备选：独立表 `agent_subscribe_rules(repo_key, agent_type, event_type, branch)`。多 agent 复用时可升级为此方案，本期先不引入。

### 4.2 变更清单

| 文件 | 变更 |
|---|---|
| `src/api/db_setup.py` | `_migrate_tables()` 幂等区加 `_ensure_column(c, "devops_agent_session", "subscribe_events", "TEXT DEFAULT '[]'")` |
| `src/api/routes/agents.py` | `AgentConfig` 增加 `subscribe_events: list | None = None`；`update_agent_config` 写列；`get_agent_detail` 返回列 |
| `src/api/session_store.py` | `get_or_create_agent_session` 建会话默认 `'[]'`（可不动，DDL 默认已覆盖） |

---

## 五、PR 合入分支提取

在 `run_pipeline` 归一化后、路由判定前，为 PR 类事件补充合入目标分支：

```python
# 归一化后：evt["branch"] 仅 push 有值；补 PR 目标分支
if not evt.get("branch"):
    _p = evt.get("payload", {}) or {}
    _obj = _p.get("object_attributes", {}) or {}
    _pr = _p.get("pull_request", {}) or _p.get("merge_request", {}) or {}
    _base = _pr.get("base", {}) or {}
    _branch = _obj.get("target_branch", "") or _base.get("ref", "")
    if _branch:
        evt["branch"] = _branch
```

- 位置建议：`event_pipeline.run_pipeline` step 1 之后（不侵入 `src/vcs/gitcode.py`，避免平台后端改动）。
- 该字段同时供「订阅分支匹配」与「event 展示」使用。

---

## 六、路由接入（`event_pipeline.run_pipeline` step 3）

现有 step 3（`event_pipeline.py:235-259`）在算出 `agent_type` 后若为空则 `no_route` 跳过。在此之后追加**仓库级订阅兜底**：

```
若 agent_type 为空（原 no_route 分支）：
    1. 读 repo 级 sre 订阅：SELECT subscribe_events FROM devops_agent_session WHERE repo_key=? AND agent_type='sre'
    2. 解析订阅列表：
       - event_type == "pull_request_merged" 且 当前 event_type 命中
       - 分支条件：订阅 branch 为空 或 等于 evt["branch"]（§五 提取值）
    3. 命中 → agent_type = "sre"，继续走原流程
       未命中 → 维持原 no_route 跳过逻辑
```

```python
# 伪代码（step 3 末尾）
if not agent_type:
    _sub_agent = _match_repo_subscription(repo_key, event_type, evt.get("branch", ""))
    if _sub_agent:
        agent_type = _sub_agent
        log.info(f"Pipeline: repo subscription matched -> agent_type='{_sub_agent}'")
    else:
        # 原 no_route / issue_done 跳过逻辑保持不变
        ...
```

- **不修改** `_resolve_issue_stage_agent` / `routing.py`：阶段矩阵结果优先，订阅只在结果为空时兜底。
- 订阅命中后仍走 step 3.3 `is_agent_enabled` 检查——SRE 默认关，用户需在页面同时开启，或「保存订阅时自动 enable」由前端提示（见 §八 取舍）。

---

## 七、前端 UI 与 API

### 7.1 `agents.js` Configure 面板（repo 级）

在 Backend/Skill 下方新增「事件订阅」区块（SRE 优先，通用渲染全部 Agent）：

```
事件订阅（Event Subscription）
  ☑ PR 合入到分支   [main]     (输入框，逗号分隔多分支；留空=不限分支)
  ─────────────────────────────
  说明：勾选后，当该仓库的 PR 合入到指定分支时，本 Agent 自动收到任务。
       默认不勾选。
```

- `data.subscribe_events` 从 `get_agent_detail` 回显。
- 保存走 `saveAgentConfig`，把勾选结果组装进 `body.subscribe_events`。

### 7.2 API

```python
class AgentConfig(BaseModel):
    ...
    subscribe_events: list | None = None   # [{"event_type": "pull_request_merged", "branch": "main"}]

# PUT /api/agents/{name}/{owner}/{repo}
#   body.subscribe_events 传入时写 devops_agent_session.subscribe_events
#   None 表示不修改

# GET /api/agents/{name}/{owner}/{repo} 响应增加：
#   "subscribe_events": <该 repo 的订阅列表>
```

---

## 八、取舍与边界

| 项 | 决策 | 理由 |
|---|---|---|
| 订阅粒度 | repo 级（`devops_agent_session`） | Agent 页面本为仓库维度；全局订阅会污染其他仓库 |
| 全局 `agent_type.subscribe_events` | 不动 | 避免破坏 `test_all_subscribe_events_cleared`；后续如需全局默认可再演进 |
| 订阅 vs 阶段矩阵 | 订阅仅兜底空路由 | 阶段矩阵是 Issue 执行链路的唯一权威，订阅不覆盖其判定 |
| 分支匹配 | 空=不限；否则精确匹配 `evt["branch"]` | 简单明确；PR 多目标分支场景可后续升级为通配/正则 |
| 与 enable 的关系 | 订阅命中仍过 enable 检查 | 与现状一致；页面保存订阅时提示「需同时启用 SRE Agent」 |
| 支持事件范围 | 首期仅 `pull_request_merged`（PR 合入） | 贴合需求；结构上可扩展其他 event_type |

### 回归风险

- 默认 `subscribe_events='[]'`：所有仓库现状行为不变，`run_pipeline` 空路由分支多一次空查。
- `tests/test_qa_skill.py:101` `test_all_subscribe_events_cleared` 断言的是**全局** `configs/agent-description.json` 字段，仓库级新列不影响；需新增仓库级订阅单测（见 §九）。

---

## 九、测试计划

| 用例 | 断言 |
|---|---|
| 仓库级订阅未配置时，`pull_request_merged` 仍 `no_route` 跳过 | 与现状一致 |
| 订阅 `pull_request_merged` + branch=`main`，PR 合入到 `main` → 路由 sre 并建 event | `agent_type == 'sre'` |
| 订阅但合入分支为 `dev`（不匹配） → 不触发 | 仍跳过 |
| 订阅 branch 为空（不限分支） → 任意分支合入都触发 | 路由 sre |
| 订阅命中但 sre 未 enable → 记录 `agent_disabled` 跳过 | 与现有 enable 语义一致 |
| `get_agent_detail` / `update_agent_config` 订阅字段读写往返 | 幂等回显 |

---

## 十、实施顺序（后续排期）

1. DB 迁移：`devops_agent_session.subscribe_events` 列（`db_setup.py`）。
2. 后端：`agents.py` 读写字段 + `event_pipeline` 分支提取（§五）+ 订阅兜底路由（§六）。
3. 前端：`agents.js` 订阅 UI + `saveAgentConfig` 组装。
4. 测试：§九 用例落地。
