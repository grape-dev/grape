# A2A 队列化改造设计（消除 pipeline 显示空闲 + 统一执行路径）

> 版本：v1.0
> 日期：2026-08-10
> 状态：待评审
> 前置：`agent-runtime-event-design.md`（M2 执行模式）、`multi-issue-isolation-design.md`（串行槽）、`runtime-admin-two-view-design.md`（Engines/Workers）
> 触发背景：Issue #5（gitcode-pr-assistant）A2A 交办成功后，pipeline 页 Coding 阶段始终显示「空闲」，即便 coding 正在执行。

---

## 〇、问题现象与根因

### 0.1 现象

- Issue #5 已由 VOD → A2A → Issue Agent（seq 468 completed）→ A2A → Coding Agent（seq 469 running）；
- pipeline 页 Coding 阶段**灯**为「In Progress」（来自 `events.status`，正常）；
- 但顶部/历史表**徽章**始终显示「空闲」，不出现「⚡ 执行中」。

### 0.2 根因（两层字段来源不同）

| 前端字段 | 数据来源 | seq 469（A2A coding） |
|---|---|---|
| `statuses`（阶段灯） | `events.status` | running → sc=1 ✅ 正常 |
| `executing`（空闲/执行中徽章） | `agent_tasks` 的 `dispatched/running` | **无记录** → false → 「空闲」❌ |

**核心矛盾**：A2A 触发的 Agent 执行**从不创建 `agent_tasks`**，而 pipeline 的「执行中」徽章只认 `agent_tasks`。

### 0.3 两条执行路径不一致

```
webhook 事件（issue_comment 等）
  → event_pipeline.run_pipeline（queue 模式）
  → enqueue_task → agent_tasks（queued/dispatched/running）
  → pipeline executing = true ✅

A2A 事件（/api/a2a）
  → webhook.py a2a_endpoint
  → threading.Thread(_execute_agent)   ← 硬编码 direct 线程，不走执行模式/不入队
  → 只更新 events.status，无 agent_tasks
  → pipeline executing = false（永远「空闲」）❌
```

---

## 一、目标

1. **A2A 触发的 Agent 执行进入 `agent_tasks` 队列**，与 webhook 事件执行路径完全一致；
2. pipeline 页 A2A 执行的 Agent 正常显示「⚡ 执行中」；
3. A2A 执行享有队列全部能力：确定路由（runtime_id）、串行槽、技能/凭据/上下文打包下发、任务可观测（claim/start/progress/complete）、优雅排空与重投。

---

## 二、现状代码定位

| 位置 | 现状 |
|---|---|
| `src/api/routes/webhook.py:196-199` | `a2a_endpoint` 收到 A2A 后 `threading.Thread(_execute_agent, ...)` 直接执行 |
| `src/api/event_pipeline.py:542` `_execute_agent` | 线程内同步：build_prompt → stream_agent → 更新 events |
| `src/api/event_pipeline.py:414-425` | webhook 事件已有完整的分派：`get_execution_mode()` → direct 线程 / 否则 `_enqueue_pipeline_task` |
| `src/runtime/service.py:158` `enqueue_task` | 入队入口（确定路由 D3 + 技能打包 + project-info + 凭据注入） |
| `src/api/event_pipeline.py:450` `_enqueue_pipeline_task` | webhook 侧入队的完整封装（init_event_json + enqueue_task + 会话） |

---

## 三、方案设计

### 3.1 核心：a2a_endpoint 复用 `_enqueue_pipeline_task`（或等价入队逻辑）

将 `a2a_endpoint` 的「spawn 线程」分支改为与 `run_pipeline` 相同的模式判断：

```
GET/POST /api/a2a
  → 鉴权 / 校验 / issue_state 推进 / append_event（status=running）  [现状保留]
  → if get_execution_mode() == "direct":
        threading.Thread(_execute_agent, ...)                     [保留现状降级]
     else:
        _enqueue_pipeline_task(repo_key, seq, agent_name, backend, skill_name, evt,
                               skill_id, skill_path, runtime_id, issue_id)
```

### 3.2 复用点

- **`_enqueue_pipeline_task`（event_pipeline.py:450）** 已实现：`init_event_json` + 会话（repo/issue 级）+ `enqueue_task`（含技能打包、project-info、凭据注入、`server_url`）。a2a 直接调用即可，**无需新写入队逻辑**。
- **`evt` 构造**：a2a_endpoint 已构造好 `evt`（含 `_a2a_context`、`_skill_path`），与 `_enqueue_pipeline_task` 的入参兼容（`evt` 里有 `repository/account/issue_id/payload`）。

### 3.3 direct 模式保留

direct 模式（本地直接子进程）不受影响，维持 `threading.Thread(_execute_agent)`，与 webhook 事件 direct 行为一致（现状 `run_pipeline` 也是 direct 时 spawn 线程）。

### 3.4 状态回写

入队后 `events` 状态流转与 webhook 事件完全一致（由 worker 侧 `complete_task`/`fail_task` → `update_event_complete` 回写），无需额外改动。

---

## 四、改动点清单

| # | 文件 | 改动 | 说明 |
|---|---|---|---|
| 1 | `src/api/routes/webhook.py`（196-200） | a2a 分支改为模式判断：direct → 线程；非 direct → `_enqueue_pipeline_task` | 核心改动 |
| 2 | `src/api/event_pipeline.py`（可选） | 若 `_enqueue_pipeline_task` 对 a2a 有差异化需要（如 a2a 无 event json 初始化差异），可抽出公共入队辅助；否则无需改动 | 复用优先 |
| 3 | `tests/`（新增） | a2a 在 queue 模式下应创建 `agent_tasks` 行；direct 模式仍线程 | 覆盖两条路径 |

### 关键实现注意（webhook.py 内）

```python
from src.runtime.service import get_execution_mode
if get_execution_mode() == "direct":
    threading.Thread(target=_execute_agent, args=(...), daemon=True).start()
else:
    from src.api.event_pipeline import _enqueue_pipeline_task
    _enqueue_pipeline_task(repo_key, seq, agent_name, backend,
                           skill_name or agent_name, evt,
                           skill_id, skill_path, runtime_id or 0, issue_number)
```

> ⚠️ `_enqueue_pipeline_task` 内部已处理 `runtime_id` 为空时的 `ensure_default_runtime("opencode")` 兜底，与现状 webhook 路径一致。

---

## 五、验证方案

1. **单测**：queue 模式下 POST `/api/a2a` → `agent_tasks` 出现对应行（status=queued，含 event_seq/issue_id）；direct 模式无新行；
2. **端到端**：复现 Issue #5 场景 → A2A 交办 coding → pipeline 页显示「⚡ 执行中」，且 coding 完成后变 Done、徽章消失；
3. **回归**：既有 70 项 runtime/pipeline 相关测试不回归；webhook 事件路径行为不变。

---

## 五·1 测试用例设计

> 新增测试文件 `tests/test_a2a_queue.py`（参考 `test_m2_execution_mode.py` 的隔离写法：`DEVOPS_EXECUTION_MODE` 环境变量切换 + `_clean()` 清 `agent_tasks/events/issue_state/会话`，避免污染其他用例）。

### 5.1.1 公共准备

```python
import json, os, pytest
from src.session_manager import db, db_lock
from src.api import event_pipeline
from src.runtime import service

def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM events")
        db.cursor().execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo%'")
        db.cursor().execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()

def _a2a_body(agent="coding", issue_number="5"):
    return {
        "agent": agent,
        "payload": {
            "repository": "owner/repo",
            "account": "owner/repo",
            "issue_number": issue_number,
            "pr_number": "",
            "context": "测试 A2A 交办",
        },
    }
```

### 5.1.2 用例表

| # | 用例 | 前置（mode） | 步骤 | 断言 |
|---|---|---|---|---|
| T1 | **queue 模式 A2A 入队** | `queue_local` | 直接调用 `a2a_endpoint` 对应 handler（或 POST `/api/a2a`） | `agent_tasks` 新增行：`status='queued'`、`event_seq` 对应新 event、`issue_id='#5'`、`agent_type='coding'`；`events` 新增 `a2a_event` 且 `status='running'` |
| T2 | **direct 模式 A2A 不建任务** | `direct` | 同 T1 | `agent_tasks` **不新增行**（仍走 `_execute_agent` 线程）；`events` 新增 `a2a_event` |
| T3 | **入队后 task 与 event 关联** | `queue_local` | 发起 A2A → 查 `agent_tasks` | `agent_tasks.event_seq == events.seq`，`issue_id` 一致；`server_url` 非空（= `get_server_base_url()`） |
| T4 | **串行槽生效** | `queue_local` | 同一 issue 已有 running/dispatched task 时再 A2A | 新任务不 claim（`service.claim` 返回 None 或仍 queued），同 `issue_session_id` 串行 |
| T5 | **A2A 任务被 worker 消费后回写** | `queue_local` | 入队 → 模拟 `_run_task`/claim → complete | `events.status` 变为 `completed`，task 终态；pipeline `executing` 字段先 true 后 false |
| T6 | **queue_remote 同样入队** | `queue_remote` | 同 T1 | 与 T1 一致（不因 remote 而改走线程） |
| T7 | **pipeline executing 派生** | `queue_local` | A2A 入队后调 `GET /api/pipeline/{owner}/{repo}` | `active.executing == true`；任务完成后 `executing == false`（覆盖 §四 改动点 1 的前端预期） |
| T8 | **webhook 事件路径不回归** | `queue_local` | `run_pipeline(issue_comment)`（复用 `test_m2_execution_mode` 的 `_evt_body`） | 行为与改造前一致：入队 + 不直接执行 |

### 5.1.3 关键断言点

1. **入队判定**：改造后 `a2a_endpoint` 内 `get_execution_mode() != "direct"` 分支调用 `_enqueue_pipeline_task`，断言 `agent_tasks` 计数 +1 且字段齐全；
2. **direct 不变**：`mode == "direct"` 分支仍 `threading.Thread(_execute_agent)`，断言 `agent_tasks` 计数不变；
3. **pipeline 徽章**：T7 通过 `_running_issue_ids` 覆盖 —— A2A 入队后 task 为 `queued`（T1 用 `queue_local` 时 claim 可能立即消费，需在断言前**冻结任务**或断言 `dispatched/running/queued` 任一），建议 T1/T7 用「入队后不立即 claim」的窗口断言，或直接断言 `_running_issue_ids` 返回集合含 `#5`。

### 5.1.4 回归范围

- 既有 `tests/test_m2_execution_mode.py`、`tests/test_runtime.py`、`tests/test_runtime_protocol.py`、`tests/test_pipeline.py` 全量通过；
- 新增 T1/T6 确认 A2A 与 webhook 事件**共用同一入队路径**，不破坏既有 `enqueue_task` 契约。

---

## 六、影响与风险

| 项 | 说明 |
|---|---|
| 行为变化 | A2A 执行从「立即线程」变为「入队 + worker claim」，非 direct 模式下会引入**队列延迟**（worker 轮询间隔 ~3s），可接受 |
| 幂等 | `enqueue_task` 每次新 task_id；A2A 重试由上层保证（现状同） |
| 回滚 | 仅 webhook.py 一处分支；回滚即恢复线程直跑 |
| 与 `executing` 派生 | `_running_issue_ids`（pipeline.py:136）已按 repo 查 agent_tasks 的 **queued/dispatched/running**（改造含 queued），A2A 入队后自然覆盖，**前端无需改动** |

---

## 七、附带观察（非本次改动范围）

- pipeline「空闲/执行中」徽章语义 = `agent_tasks` 实时运行；若期望也反映「events 有 running 但尚未入队」的窗口，可后续评估在 `executing` 判定中并入 `events.status=running`（需权衡与队列实际执行的语义偏差）。
- a2a 的 `tasks/notify` 端点当前不存在（Issue Agent 尝试调用 `POST /api/tasks/notify` 得到 Not Found）；如任务通知需要，属另一独立功能，另文规划。

---

*本文为 A2A 队列化改造方案，待评审后实施。*
