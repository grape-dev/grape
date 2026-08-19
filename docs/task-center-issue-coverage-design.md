# 任务中心覆盖全部 Issue 设计（task-center-issue-coverage）

> 分支：feat/task-center-issue-coverage（基于 origin/dev ceb8100d）
> 状态：**设计稿，待用户审阅**（未实现）

## 1. 背景与问题

- 任务中心（Task Center）= 前端 `GET /api/tasks/mine` → 只查 **`tasks` 表**中 `user_id=?` 的行。
- `tasks` 表的**唯一写入口**是 `/api/tasks/notify`（`_create_task`，tasks.py:77），调用方仅两类：
  1. 引导类任务（onboarding 结束后 POST /api/tasks/notify，assignee=owner）
  2. issue agent 分配/换人时 POST /api/tasks/notify
- **普通 Issue（webhook issue_opened / 评论 / 更新）处理时不会进 tasks 表** → 用户在任务中心找不到大多数 Issue。

用户诉求：**每个 Issue 希望在任务中心（Task Center）都可以找到**。

## 2. 现状链路（代码核实）

```
webhook(Issue Hook/Note Hook/MR Hook)
  → run_pipeline（event_pipeline.py，按 stage 路由 agent）
  → append_event(events 表) + enqueue_task(agent_tasks 表)
  → issue agent 执行；仅在「分配/换人」时 POST /api/tasks/notify 建 tasks 行
  → 任务中心 GET /api/tasks/mine ← tasks 表
```

- `tasks` 表列：id, user_id, assignee, repo_key, issue_number, title, task_description,
  conversation_id, status(active/processing/completed), completed_reason(issue_closed/reassigned/bot_takeover),
  created_at, completed_at
- 已有幂等：`_create_task` 同 repo+issue+assignee 的 active 行复用，不重复建
- 已有关闭链：`/api/tasks/close`（issue agent 关闭 Issue 时调用，置 issue_closed）；
  换人 `_close_other_assignee_tasks`（reassigned）；bot 接管 `_close_issue_tasks_bot_takeover`
- 前端 tasks.js 展示字段已齐（Issue#、标题、仓库、状态、描述、「去对话」按钮——走 conversation_id 关联的 Issue 对话）

## 3. 方案（推荐 A：webhook 自动建档，旁路不改核心流程）

### A. 触发点（可选其一，推荐 A1，不侵入 pipeline 路由）

**A1（推荐）**：webhook 收到 `Issue Hook (open/update/reopened)` 事件时，在 `run_pipeline` 入口确认 issue 后**追加一次幂等建任务**：

```
webhook → issue 事件 → ensure_issue_task(repo_key, issue_number, payload)
  ├─ parse assignee（payload.assignees[0].username）或空
  ├─ assignee 有 → unified_user_mapping → user_id；无映射 → repo owner → user_id
  ├─ 都无 → user_id=0 落行（assignee 留 login 文本，见下方决策点 P2）
  ├─ _create_task(幂等复用 active)  title：从 payload 取 issue.title；description：事件类型/简述
  └─ 失败仅告警日志，不阻塞事件处理（非核心链路）
```

- 复用 tasks 表与任务中心全部现有 UI/流转，**不改 pipeline 主流程 / A2A / 配置模型 / worker 协议**
- 关闭/转派/接管沿用现有 `/api/tasks/close`、`_close_other_assignee_tasks`、`_close_issue_tasks_bot_takeover`，无需新增
- 存量：该特性上线时对**已有活跃 Issue** 做一次性回填（`SELECT DISTINCT issue_state … 补建 tasks`，幂等）

**A2（备选，不推荐）**：在 issue agent 首次处理该 Issue 时（task 完成后）写 tasks——延迟出现、依赖 agent 行为，不保证覆盖。

### B. 备选方案（仅作对比，不推荐实施）

- **B1 任务中心聚合**：`/ api/tasks/mine` UNION events/issue_state 的活跃 issue，前端加"未分配任务"分区——语义混入，改动 mine+前端，状态模型复杂
- **B2 每次 notify 已有事件**：覆盖不全，无新价值

## 4. 决策表（请产品确认）

| # | 问题 | 方案 A 默认 | 备选 |
|---|------|------------|------|
| P1 | 触发范围 | 所有被托管的仓库的 Issue 事件都建任务 | 仅指定仓库 |
| P2 | assignee 未注册平台(无 unified_user_mapping) | user_id=0 落库，`/api/tasks/mine` 查询 `user_id=? OR user_id=0` **展示**（用户拍板：展示不隐藏） | 不建行 |
| P3 | Issue 无 assignee（opened 未指派） | 建给仓库 owner 的 user_id | 不建，等 agent 分配后再 notify 建 |
| P4 | 已有「引导/notify」任务并发 | `_create_task` active 去重已防重复 | — |
| P5 | 关闭 | 沿用 /api/tasks/close（issue agent 当前已调用）；若部分链路没调，可补 webhook issue_closed 兜底自动 close | — |
| P6 | 存量回填 | 上线时 SQL 回填活跃 issue | 不回填 |

## 5. 改动清单（待确认后实施）

- `src/api/event_pipeline.py`（或 webhook 校验后入口）：`ensure_issue_task()` 调用，约 +15 行
- `src/api/routes/tasks.py`：新增 `ensure_issue_task()`（幂等，复用 `_create_task`），约 +30 行
- `tests/test_tasks.py`（或新测试文件）：ensure 幂等 / assignee 解析 / 无 assignee 兜底 owner / 关闭链，4 用例
- 存量回填 SQL（部署脚本，一次性）

## 6. 验证

- pytest 目标测试全绿
- 现场：新建一个 Issue → 任务中心立即可见；Issue 关闭 → 任务中心标记已完成(issue_closed)
- 不与既有 notify/引导任务冲突，无重复行