# 同 Issue 同 Agent Session 复用能力分析

> 版本：v1.0 ｜ 日期：2026-09-06
> 范围：分析"同一 Issue + 同一 Agent（agent_type）"的多次任务执行，是否能复用 opencode 会话上下文。
> 核心对象：`issue_agent_session` 数据表及其在任务执行链路中的读写与应用。
> 状态：分析报告（不改代码）

---

## 一、结论（先行）

**能支持 session 复用**——当前系统已实现"同 Issue + 同 Agent"的会话续接，

且采用**严格隔离**策略：每个 `(repo_key, agent_type, issue_id)` 独立会话，
通过 `issue_agent_session.runtime_session_id` 持久化 opencode 会话指针，
在同 Issue 同 Agent 的**后续任务**中复用该指针作为 `opencode --session`，实现上下文延续。

---

## 二、数据表：issue_agent_session

### 2.1 表结构（db_setup.py:386-396）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | uuid，= `agent_tasks.issue_session_id` |
| `repo_key` | TEXT | 仓库（带 gitcode: 前缀） |
| `agent_type` | TEXT | vod / issue / coding / qa / sre |
| `issue_id` | TEXT | `#5`（Issue 号） |
| `runtime_session_id` | TEXT | **opencode --session 指针（会话续接核心）** |
| `status` | TEXT | idle / running / failed |
| `last_active_at` | TEXT | 最近活跃（排序/清理依据） |
| `created_at` | TEXT | 创建时间 |
| 唯一约束 | UNIQUE(repo_key, agent_type, issue_id) | 同 Issue 同 Agent 唯一会话 |

### 2.2 设计定位（session_store.py:491-493）

- 与 `devops_agent_session`（repo+agent **配置**）分离；
- `issue_agent_session` 只承载 **Issue 级执行会话指针 + 状态**，
  作为 `agent_tasks.issue_session_id` 的串行粒度来源；
- 每个 Issue 独立 session：新 Issue 首建 `runtime_session_id` 为空（**不继承 repo 级指针**，
  避免多 Issue 共用 session 导致 cwd/上下文错位）。

---

## 三、Session 复用链路（读写全流程）

### 3.1 读取（复用入参）

```
任务执行前：
  get_or_create_issue_session(repo_key, agent_type, issue_id)   # session_store.py:495
    → 有行：返回 runtime_session_id（复用）
    → 无行：INSERT 新行（runtime_session_id 置空全新）
    → 该返回值作为 opencode --session 续接
```

- **direct 路径**：`event_pipeline.py:1703-1712`
  ```python
  _isess = get_or_create_issue_session(repo_key, agent_type, issue_id)
  issue_session_id = _isess['id']
  oc_session_id = _isess['runtime_session_id']      # → stream_agent session_id
  ```
- **queue 路径**：`event_pipeline.py:1310-1312`（同款，写入 `agent_tasks.runtime_session_id`）
  → worker `agent.py:1158` `session_id=task.runtime_session_id` → `--session`
  → opencode `build_args`（opencode.py:13-14）`--session <id>`

### 3.2 执行（复用会话运行）

- executor 用 `oc_session_id → opencode --session`，让同会话的后续任务**延续对话历史/工具状态**。
- 会话失效自动重建（executor.py:730 `session_missing → 无 session 重跑`）。

### 3.3 回写（持久化新指针）

```
执行完成后：
  direct：event_pipeline.py:1907-1911
    if new_oc_session_id != oc_session_id: update_issue_session(issue_session_id, runtime_session_id=new_oc_session_id)
  queue：service.py complete_task:1363-1368
    _new_sid = usage.session_id; update_issue_session(task.issue_session_id, runtime_session_id=_new_sid)
```

→ 下一次同 Issue 同 Agent 任务读到更新后的 `runtime_session_id`，续接新会话。

### 3.4 状态与约束

- **状态双写**：pipeline.py:298 维护 `issue_agent_session.status` 双镜像（防漂移）。
- **失败处置**：service.py:1503-1504 同一 issue_session 连续 3 次 `exit_code<0`（-15/-1/timeout）
  → 清空 `runtime_session_id`（避免复用坏会话）。
- **清扫保护**：cleanup.py:30-44 有 dispatched/running 任务的 issue_session 不清理；
  workdir 清理按 `(repo, agent, issue)` 判断活跃。

---

## 四、支持情况矩阵

| 维度 | 支持情况 |
|---|---|
| 同 Issue 同 Agent 连续任务 | ✅ 复用 `runtime_session_id` 续接会话 |
| 同 Issue 不同 Agent | 各自独立 session（按 agent_type 分列，互不干扰） ✅ |
| 多 Issue 并行 | 各 Issue 独立 session（unique 三元组隔离） ✅ |
| 会话续接历史上下文 | ✅ opencode --session 延续（对话历史/工具状态） |
| 会话失效自愈 | ✅ executer 检测 session not found → 无 session 重跑 |
| 失败回退 | ✅ 连续 3 次失败清空指针，避免复用坏会话 |
| repo 级旧指针 | ❌ 已废弃（devops_agent_session.runtime_session_id 不再回写，无消费方） |

### 关键约束：每 Issue 独立，不跨 Issue 复用

- 新 Issue 的某 Agent 首次会话 `runtime_session_id=''`（全新）——**刻意不继承** repo 级或其它 Issue 的会话，
  防止上下文错位（vod-agent-hang-rootcause-fix.md 方案 A）。

---

## 五、链路风险与观察点

1. **direct 与 queue 一致性**：两条路径都读写 `issue_agent_session`，但 direct 用 `oc_session_id` 存内存、
   queue 用 `task.runtime_session_id`——需保证两边回写时机一致（direct 完成后、queue complete_task 中）。
2. **并发同 Issue 同 Agent**：串行粒度靠 `agent_tasks.issue_session_id`（claim 串行槽），
   防止同会话并发写坏指针。
3. **会话过期 vs 复用判断**：opencode 侧 session 可能过期但其 `runtime_session_id` 仍指向旧值——
   依赖 executer 的 `session_missing` 自动重建（有兜底，但每次重建丢上下文）。
4. **状态字段语义**：`status=idle/running/failed` 与 `last_active_at` 主要供运维/清扫，
   不作为复用判定依据（复用只看 `runtime_session_id` 非空）。

---

## 六、结论

- **能力已具备**：同 Issue 同 Agent 的 session 复用完整闭环（读→执行→写→再读）。
- **隔离正确**：per (repo, agent, issue) 独立，多 Issue 并行安全，不跨 Issue 串上下文。
- **潜在增强（非必须）**：
  1. 会话 TTL/过期主动失效策略（当前依赖执行时 opencode 自愈）；
  2. `status` 语义强化（如 running 时同会话新任务是否该排队/阻塞可显式化）；
  3. 前端/详情页展示当前 Issue 各 Agent 的 session 状态（`list_issue_sessions` 已提供数据）。

---

*分析报告；不改代码。如需针对某点深入或实施增强，另行展开。*