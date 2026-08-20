# Agent Session 智能续接与死会话自动恢复方案（smart-session-recovery-design.md）

> 版本：v1.0
> 日期：2026-08-19
> 状态：待评审（未实施）
> 触发背景：#1750（hello-html2 issue #2, issue agent）连续 3 次失败（exit=-15），根因是
> 旧 session `ses_fead3ae3...` 积累 6 次运行上下文后 LLM 处理超时。当前重派（redispatch）暴力清空
> runtime_session_id，虽规避死会话但丢了正常的会话连续性。
>
> 目标：会话正常时续接（同一 Issue 同一 Agent 的所有事件共享上下文），仅在检测到死会话时智能清空。

---

## 一、现状问题

### 1.1 会话设计意图

`issue_agent_session` 的 `UNIQUE(repo, agent, issue)` 设计目标：**同一 Issue 同一 Agent
的所有事件共享 opencode 会话**——分析 Spec → 追问细节 → 处理评论，opencode 延续同一对话
记忆，保持上下文连续性。

单次执行生命周期：
```
enqueue_task/execute_agent:
  oc_session_id = issue_agent_session.runtime_session_id   → 启动 opencode --session
  opencode 执行 → 产出 new_session_id
  update_issue_session(issue_session_id, runtime_session_id=new_session_id)   → 写回
```

### 1.2 当前重派的"暴力清空"（2026-08-19 实施）

**`redispatch_event`（events.py）** 在创建新任务时强制 `runtime_session_id=""`：
- **远端 Worker**：Worker 读取 task 自带的 `runtime_session_id=""` → 清空生效 → **丢失连续性**
- **本地 direct**：`_execute_agent` 直接从 DB 读 `issue_agent_session.runtime_session_id` → **不生效**

| 问题 | 说明 |
|------|------|
| **连续性丢失** | 健康 session 也被清空，后续事件不记得之前的分析 |
| **两条路径不一致** | Worker 清空、direct 不清空，同一 Issue 下 session 行为不同 |
| **无恢复机制** | 死 session 没有自动检测，依赖人工感知"一直 failed" |

---

## 二、目标设计

### 2.1 会话智能续接 + 死会话恢复

```
正常会话：
  task 1 → opencode --session ✅ → 成功 → 写回新 session
  task 2 → opencode --session ✅ (续接) → 成功
  task 3 → opencode --session ✅ (续接) → 成功

死会话：
  task 1 → opencode --session → exit=-15 (LLM timeout)
  task 2 → opencode --session → exit=1 (quick fail)
  task 3 → opencode --session → exit=-15 (LLM timeout)
  → 连续 3 次 exit=-15/timeout → 清空 session *
  task 4 → opencode (全新 session) → 成功 ✅
```

**\* 连续失败判定**：同一 `issue_agent_session` 下连续 N 次 task 的 exit_code 为 -15 或 execution 超时 → 触发自动清空。

### 2.2 两条路径统一

| 路径 | 现状 | 改为 |
|------|------|------|
| **远端 Worker** | 重派 == "" ，始终清空 | 重派**不设空**，续接原 session；失败检测触发清空 |
| **本地 direct** | `_execute_agent` 读 DB | 同逻辑，也走失败检测 |

> 责任落在 **`_execute_task`（Worker）/ `_execute_agent`（direct）** 的完成/失败处理，
> 而非 `redispatch_event`。重派只是"重新入队"，不再干预 session。

### 2.3 判定阈值

| 触发条件 | 动作 |
|------|------|
| task `exit_code` == 0 且 opencode 产出 new session | **保留** `runtime_session_id`（正常写回） |
| task `exit_code` = -15 或执行超时 | 连续失败计数 **+1** |
| 连续失败 ≥ 3 次 | **清空** `issue_agent_session.runtime_session_id=""`（下次走全新会话） |

> **为何不按错误码区分**：session 过期/上下文溢出不返回独立错误码。opencode 加载大量上下文后
> LLM 卡住 → 看门狗 SIGTERM → `exit=-15`，和"LLM 响应慢"表现相同。因此无法用单次 exit_code
> 判定"session 已死"，只能靠**连续失败次数**区分——单次 -15 可能是偶发慢，保留重试；连续 N 次
> 则是 session 问题，清空。

> 连续计数为 **内存计数**（不同 issue_session 独立，重启清零，可接受，无需持久化）。

---

## 三、改动点清单

### 3.1 `redispatch_event`（events.py）

- **去掉** `runtime_session_id=""` 的强制清空
- 保留 `update_task_status(task_id, "cancelled")`（旧任务清理）
- 新增：清空逻辑不在此处，交给执行端判定

### 3.2 `_execute_task`（Worker 端，agent.py）

```python
async def _execute_task(client, task):
    oc_session_id = task.get("runtime_session_id") or get_issue_session_db(...)  # 优先 task，回退 DB
    ...
    # done 处理：
    if done and done["exit_code"] == 0:
        update_issue_session(sid, runtime_session_id=done["oc_session_id"])  # 正常写回
        reset_fail_count(sid)                                                # 重置失败计数
    elif done and done["exit_code"] in (-15, ...):  # 超时/看门狗
        incr_fail_count(sid)
        if fail_count(sid) >= 3:
            update_issue_session(sid, runtime_session_id="")                 # 清空死 session
```

### 3.3 `_execute_agent`（Direct 端，event_pipeline.py）

- 与 Worker 端同逻辑：成功时正常写回；失败累积 → 清空

### 3.4 连续失败计数

- 内存 `dict: issue_session_id → int`，重启清零
- 或写入 `issue_agent_session.consecutive_failures` 列（需 DDL 变更）
- **推荐内存计数**：简单、故障重启后自然重置，可接受

### 3.5 `_execute_task` / `_execute_agent` 统一读取 session

当前两条路径 session 来源不同：

| 路径 | 现状 | 改为 |
|------|------|------|
| Worker | `task.get("runtime_session_id")` | 同上，空时回退 `issue_agent_session` |
| Direct | `issue_agent_session.runtime_session_id` | 同上 |

统一后：**task 自带的 `runtime_session_id` 优先**（支持显式覆盖），回退 DB 读 `issue_agent_session`。

---

## 四、验证

| 用例 | 步骤 | 断言 |
|------|------|------|
| 正常续接 | 同 session 连续 2 次 task，均 exit=0 | session_id 延续（第二次 task 的 opencode 用第一次的 session） |
| 死会话清空 | 同 session 连续 3 次 exit=-15 | 第 4 次 task 走全新 session（`runtime_session_id` 已清空） |
| 偶发失败不清空 | 同 session 1 次 exit=-15 + 1 次 exit=0 | session 正常续接 |
| 重派不干预 | 重派后 task 正常执行 | session 来自 issue_agent_session（未清空），保持连续性 |
| 两条路径一致 | Worker 和 direct 各触发一次 | session 行为一致（失败累积计数独立但在同一 issue_agent_session 下共享） |

---

## 五、结论

- **保留 session 续接能力**（不破坏设计初衷），仅在检测到死会话时清空
- **统一 direct/queue 两条路径**的 session 行为
- 改动集中在 `agent.py`（Worker `_execute_task`）和 `event_pipeline.py`（direct `_execute_agent`）的 done/fail 处理
- `redispatch_event` 不再干预 session（移除 `runtime_session_id=""` 强制清空）

*本文为方案设计，待评审后实施。*