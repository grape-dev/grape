# Event 手动重新派发设计（Event Redispatch）

> 目标：在 Event Detail 页的 **Status** 区域增加「🔄 重新派发」按钮，当事件长时间
> `running`、`failed`（想重试）或关联任务卡死（dispatched/running 悬空）时，人工一键
> 将任务重新入队，让 worker/本机 RuntimeAgent 重新 claim 执行，避免事件永久 running。
>
> 本文同时修正 **`running` 语义**：`running` 必须由 worker 侧在**引擎真正开始执行后**
> 主动设置（当前实现过早标记，见 §2.2/§3.1）。

## 1. 背景与问题

### 1.1 现状

- Event Detail 页（`src/api/static/js/pages/events.js`）Status 行为**纯文本**：
  - `⏳ running` / `✅ Success` / `❌ Failed`，无可操作入口。
- 后端 `get_event_detail`（`src/api/routes/events.py:12`）返回事件信息 + `engine_instance`，
  **但不返回关联 `agent_tasks` 的 id / status / runtime_id**。

### 1.2 卡死形态（真实案例 seq#678）

```
claim: runtime_id=4 task=t_067a26be1afa        (11:43:31.496)
opencode 启动                                       (11:43:31.499)
opencode 进程活着但不产出，watchdog 每 600s 续命一次
  → task 永远 running，事件永远 running
  → 无 complete/fail 回调，无任何自动兜底可收敛
```

根因通常是：opencode 卡在权限确认 / LLM 网络超时 / 子进程挂起，agent 侧既不
complete 也不 fail，task 悬空在 `running`。

### 1.3 期望

- 运维/用户在 Event Detail 页看到**卡死的 `running`（>10 分钟）**或 **`failed`** 时，
  点击「重新派发」：
  - 关联任务非终态 → 置回 `queued`，由 worker 重新 claim；
  - 关联任务已终态/缺失 → 重新入队一个新任务；
  - 事件状态回写 `running`，等待新执行回报。
- `completed` 事件不显示按钮（成功无需重派）。

## 2. 现状机制梳理

### 2.1 任务状态机（`agent_tasks.status`）

```
queued → dispatched → running → completed / failed / cancelled
              ↑            │
              └── 超时回收 / requeue 回滚 ──┘
```

| 状态 | 说明 |
|---|---|
| queued | 可被 claim |
| dispatched | **已 claim 但引擎尚未真正启动**（技能落地/worktree/Popen 准备阶段） |
| running | **引擎已真正启动并开始执行**（opencode 进程已产出首个执行事件） |
| completed / failed / cancelled | 终态 |

### 2.2 ⚠️ `running` 语义偏差（本次一并修正）

**现状问题**：`_execute_task` 中 `task_start`（置 running）在 `stream_runtime`
（真正 Popen 引擎）**之前**调用（`src/runtime/agent.py:181` vs `:201`）：

```python
await client.task_start(tid)      # ← 先置 running（此时引擎未启动）
...
for evt_type, data in stream_runtime(...):   # ← 之后才 Popen opencode
```

**后果**：只要 worker 拿到任务、还没启动引擎就标记 running。若技能落地失败、
worktree 失败、opencode 启动异常 → 任务早已 running 但引擎根本没跑（正是 seq#678
形态），`dispatched`（已 claim 未 start）与 `running`（已 start）边界被模糊。

**修正原则**：`running` 必须由 worker 在**引擎确实开始执行**后设置：
- 引擎成功 Popen 并产出**首个执行事件**（`step_start` / `text` / `tool_use`）时才
  `task_start` 置 running；
- 若引擎启动失败 / 立即异常退出 → 直接 `client.fail(tid)`，任务由 `dispatched`
  **直接置 `failed`**（跳过 running），事件同步为 `failed`——而不是"停留在
  dispatched"（fail 必然写终态 failed）。

**三种"没正常执行"场景须区分**（避免把"启动失败"和"worker 丢弃"混为一谈）：

| 场景 | 任务状态流转 | 事件状态 | 收敛机制 |
|---|---|---|---|
| A. 引擎启动失败（Popen 抛错 / 找不到 CLI / 立即异常退出） | dispatched → **failed**（跳过 running） | running → **failed** | 立即 fail；重派按钮在 `failed` 上显示，手动重试 |
| B. 引擎已启动但卡死无产出（进程活着静默，如 seq#678） | dispatched → running → watchdog 超时 → **failed** | running → failed | watchdog 终结 → fail；或用户对 `running`>10min 手动重派 |
| C. worker 丢弃任务（claim 后不 start 不执行） | **dispatched**（一直）→ 120s 回收重排 | running →（回收置 failed 时同步） | `claim` 内置 120s 超时回收自动重试，attempts≥3 → failed |

> 关键：三种场景最终都可能变 `failed`，但**路径与停留状态不同**——
> A 停在 `failed`（无 dispatched 停留期）；B 停在 `running`（被误标后卡死）；
> **C 才是唯一真正"停留在 dispatched"的场景**（worker 丢弃，120s 回收兜底）。

**联动收益**：修正 running 语义后——
- 场景 A：启动失败直接 `failed`，由重派按钮手动重试（事件 failed 显示按钮）；
- 场景 B：不再有"running 但引擎没跑"的脏状态，卡死 running 由 watchdog 或手动重派收敛；
- 场景 C：worker 丢弃的任务停留在 `dispatched`，由 `claim` 内置 120s 超时回收自动回池重排。

### 2.3 现有可复用能力

| 能力 | 位置 | 说明 |
|---|---|---|
| `requeue_task(task_id)` | `src/runtime/service.py:527` | `dispatched/running` → `queued`，`started_at=NULL` |
| `claim(runtime_id)` | `service.py:369` | 原子抢单（含 120s dispatched 超时回收、串行槽） |
| `update_event_status(seq, status)` | `src/api/event_store.py:211` | 事件状态回写 |
| `enqueue_task(...)` | `service.py:239` | 业务层入队（需 agent_session/runtime/prompt/cwd 等） |
| `cancel_task(task_id)` | `service.py:523` | 用户态端点 `/tasks/{id}/cancel`（`runtime.py:273`）|

### 2.4 认证现状

| 端点 | 认证 | 说明 |
|---|---|---|
| `/api/runtime/tasks/{id}/requeue` | `_require_credential`（worker 凭据） | 浏览器登录用户**不可调** |
| `/api/runtime/tasks/{id}/cancel` | `get_current_user`（登录用户） | 已有用户态先例 |

> ⚠️ 重新派发需要**登录用户可调**，故需新增用户态端点（见 §4.1.2）。

## 3. 设计要点

### 3.1 判定"该显示按钮"

按钮显示条件（满足任一即显示，仅作提示不强校验）：
- 事件 `running` **且已超过阈值（`started_at` 距今 > 10 分钟）**，或
- 事件 `failed`（允许手动重试一次），或
- 关联 `task_status` 为 `dispatched`（已 claim 但引擎未启动，疑似卡在启动阶段）。

`completed` 事件不显示按钮。`running` 但 `started_at` 距今 ≤ 10 分钟不显示
（仍在正常执行窗口内，避免误触）。

> 注：若按 §2.2 修正了 running 语义，`task_status=dispatched` 才真正代表
> "引擎未执行"；在修正落地前，`task_status=running` 也视为可疑卡死（因旧实现
> 可能引擎没跑就 running），故 running>10min 仍需显示按钮作为兜底。

前端仅负责展示 + 发起请求；真正能否重派由后端判定。

### 3.2 重新派发语义

核心：**无论 task 处于什么状态，最终都让该事件有一个可被 claim 的任务**。

| 事件状态 | 关联 task 状态 | 操作 |
|---|---|---|
| running（>10min，卡死） | dispatched / running | `requeue_task`（置回 queued） |
| running（>10min） | completed / failed / cancelled | 重新 `enqueue_task` 新建任务 |
| running（>10min） | 无 task 记录 | 重新 `enqueue_task` 新建任务 |
| failed（手动重试） | 任意终态 / 无 | 重新 `enqueue_task` 新建任务 |
| completed | — | 按钮隐藏（不提供重派） |

### 3.3 重复执行防护（重要）

若旧 task 实际仍在执行（opencode 进程活着但卡住），仅 requeue 会形成
"新旧两个执行者抢同一 session"：

1. **优先尝试终止旧执行**：
   - 若 task 有 `runtime_session_id`，调用该 runtime 对应 worker 的终止能力；
   - 服务端侧可记录 `started_at`，重派时对**卡住（>阈值）的 running** 任务，
     先尝试通知执行侧退出（未来可接 `cancel` 下发），再 requeue。
2. **重派后新任务语义独立**：requeue 复用原 task（同 `task_id`、同 `runtime_session_id`）；
   新建任务则换新 `task_id`、**清空 `runtime_session_id`**（全新上下文，避免复用坏会话）。
3. **幂等**：同一事件 30s 内重复点击，后端去重（记录 `last_redispatch_at`）。

> 取舍建议（第一阶段）：对 `dispatched`/`running` 任务采用 **requeue 复用原 task**，
> 保留 `runtime_session_id`（opencode 断点续跑，若进程已死则新 claim 会新建）；
> 对已终态/缺失任务**新建 task**。是否 kill 旧进程作为增强项（见 §6 后续项）。

## 4. 实现方案

### 4.1 后端

#### 4.1.1 `get_event_detail` 附带 task 信息

`src/api/routes/events.py` 的 `get_event_detail`，在返回前追加：

```python
# 关联任务（重派/展示用）
task_info = {"task_id": "", "task_status": "", "runtime_id": 0}
try:
    with db_lock:
        c3 = db.cursor()
        c3.execute(
            "SELECT id, status, runtime_id, runtime_session_id, created_at "
            "FROM agent_tasks WHERE event_seq=? ORDER BY created_at DESC LIMIT 1",
            (seq,))
        tr2 = c3.fetchone()
        if tr2:
            task_info = {
                "task_id": tr2[0], "task_status": tr2[1], "runtime_id": tr2[2] or 0,
                "runtime_session_id": tr2[3] or "", "task_created_at": tr2[4] or "",
            }
except Exception:
    pass
result["task"] = task_info
```

#### 4.1.2 新增用户态端点 `POST /api/events/{seq}/redispatch`

放 `src/api/routes/events.py`（该文件已有 `get_current_user` 鉴权风格）：

```python
@router.post("/{seq}/redispatch")
async def redispatch_event(seq: int, user: dict = Depends(get_current_user)):
    """手动重新派发事件任务（卡死 running>10min 或 failed 时人工收敛）。

    非终态任务 → requeue 置回 queued；终态/缺失 → 重新入队新任务。
    事件状态回写 running。30s 内重复调用幂等拒绝。
    """
    from src.runtime.service import requeue_task, get_task, enqueue_task
    from src.api.event_store import update_event_status
    # 1) 幂等：查最近重派记录（30s 内拒绝）
    # 2) 查事件 + 关联任务
    # 3) 分支处理（见 §3.2）：
    #    - task 非终态（dispatched/running）→ requeue_task
    #    - task 终态或缺失（含事件 failed）→ 复用原 task 快照参数 enqueue_task 新建
    # 4) update_event_status(seq, "running")
    # 5) 记录 last_redispatch_at（meta 表）
    return {"status": "redispatched", "task_id": ...}
```

关键实现细节：

- **终态/缺失 → 重新入队**：需要复现原任务参数。从 `events` 行 + `devops_agent_session`
  取 `repo_key/agent_type/skill/runtime_id/prompt/cwd`。但事件表不存完整 prompt——
  **最稳妥是从原 task 快照复制**：若原 task 存在（终态，含 failed），直接以原 task 的
  `prompt/cwd/skill_files/project_info_files/knowledge_files/env_extra` 重新 `enqueue_task`
  （复用参数，新 task_id，清空 runtime_session_id）。若连 task 都没有，
  回退走 `_enqueue_pipeline_task` 用事件 payload 重建 prompt。
- **requeue 分支**：`requeue_task(task_id)` 即可，worker 下一轮 claim 会重新领取。
- **failed 事件重派**：走"新建 task"分支（旧 task 已终态无法 requeue），复用快照重试。

#### 4.1.3 权限

- `get_current_user` 登录校验；
- 可选增强：校验该用户对 `repo_key` 有管理权限（沿用现有 `managed_repos` / 角色逻辑），
  第一阶段可仅做登录校验，与 `cancel` 端点对齐。

#### 4.1.4 修正 worker 端 `running` 语义（配合 §2.2）

`src/runtime/agent.py` `_execute_task`：把 `await client.task_start(tid)`（现 line 181，
`stream_runtime` 之前）**移入事件循环内，首个执行事件触发一次**：

```python
started = False
for evt_type, data in stream_runtime(...):
    if evt_type in ("step_start", "text", "tool_use") and not started:
        await client.task_start(tid)   # 引擎确已开始执行 → 才置 running
        started = True
    if evt_type == "done":
        done = data
        ...
# 循环结束仍未 start（引擎启动失败/无产出）→ 直接置 failed（跳过 running，事件同步 failed）
if not started:
    await client.fail(tid, "engine did not start")
    return
```

> 该改动与 `stream_agent` 的 `done` 产出兼容：引擎启动失败会在首次迭代即产出
> `done`/抛异常，此时不置 running，直接 fail。worker 侧 `service._run_task`（本地
> queue_local 模式）同样按此原则调整（`service.py` 的 `_run_task` 内同步调用 `task_start`）。

### 4.2 前端（`src/api/static/js/pages/events.js`）

Status 行（line 68）后追加按钮。显示条件用后端返回的 `task` 信息判定：
- `data.status === 'failed'` → 显示（重试）；
- `data.status === 'running'` 且 `data.started_at` 距今 **> 10 分钟** → 显示（卡死）；
- `data.task && (data.task.task_status === 'dispatched')` → 显示（引擎未启动疑似卡死）；
- 其余（含 `completed`、`running` ≤ 10min）→ 隐藏。

```js
const _redispatchVisible =
  data.status === 'failed' ||
  (data.status === 'running' && data.started_at && (Date.now() - new Date(data.started_at).getTime()) > 10 * 60 * 1000) ||
  (data.task && data.task.task_status === 'dispatched');

<div><span style="color:var(--t3)">Status:</span> <span class="...">${...}</span>
  ${_redispatchVisible ? `
    <button class="bt bt-o bt-s" style="margin-left:8px" onclick="redispatchEvent(${data.seq})">🔄 重新派发</button>` : ''}
</div>
```

新增函数：

```js
function redispatchEvent(seq) {
  if (!confirm('确认重新派发该事件任务？\n将把卡住的任务重新入队执行，可能导致重复执行。')) return;
  API.post('/events/' + seq + '/redispatch').then(d => {
    app.showToast('已重新派发');
    // 刷新详情
    const cur = ...;  // 重拉 /api/events/{seq}
  }).catch(e => app.showToast('重派失败: ' + (e.message || e)));
}
```

### 4.3 缓存版本号

`src/api/static/index.html`：`events.js?v=12 → v=13`。

## 5. 涉及文件清单

| 文件 | 改动 |
|---|---|
| `src/api/routes/events.py` | `get_event_detail` 附 task 信息；新增 `POST /{seq}/redispatch` |
| `src/runtime/agent.py` | `task_start` 延迟到引擎首事件后触发（§2.2/§4.1.4 running 语义修正） |
| `src/runtime/service.py` | `_run_task`（queue_local）同步 `task_start` 延迟（若本地 worker 采用） |
| `src/api/static/js/pages/events.js` | Status 行加按钮 + `redispatchEvent()` |
| `src/api/static/index.html` | events.js 版本号 +1 |
| 本文档 | `docs/new-version2/event-redispatch-design.md` |

## 6. 验收标准

1. 打开一个卡死的 `running` 事件（`started_at` 距今 > 10 分钟，如 seq#678 复现后）→
   显示「🔄 重新派发」按钮。
2. 打开一个 `failed` 事件 → 显示「🔄 重新派发」按钮。
3. 打开 `completed` 或 `running` ≤ 10 分钟的事件 → 不显示按钮。
4. 点击后：非终态 task 置回 `queued`（或终态/缺失时新建 task），事件状态回 `running`，
   toast 成功。
5. worker/本机 RuntimeAgent 在下一轮 claim 重新领取，事件最终进入 `completed/failed`。
6. 30s 内重复点击被幂等拒绝（toast 提示"已重派过，请稍候"）。
7. 登录用户可调；未登录 401。
8. **running 语义修正后**：
   - 引擎启动失败（场景 A）→ 任务由 `dispatched` 直接置 `failed`，事件 `failed`，
     不误标 running，可在 failed 事件上手动重派；
   - worker 丢弃任务（场景 C）→ 停留在 `dispatched`，由 `claim` 的 120s 超时回收
     自动重排（attempts≥3 后置 failed）。

## 7. 风险与后续增强

| 项 | 说明 |
|---|---|
| 重复执行 | 旧 opencode 进程可能仍活着。第一阶段 requeue 复用原 task/session；后续增强：服务端下发终止（kill 旧进程 / cancel 会话）后再 requeue。 |
| 事件/任务状态不一致 | 终态 task + running 事件是历史遗留，重派走"新建任务"分支收敛。 |
| running 语义修正影响 | 延迟 `task_start` 后：引擎启动失败任务直接置 `failed`；worker 丢弃任务停留 `dispatched`（120s 回收）。需确认 UI/统计对 `failed`/`dispatched` 的展示（Pipeline/历史列表）可接受。 |
| prompt 重建 | 无 task 时回退 `_enqueue_pipeline_task` 重建 prompt，可能因 payload 缺失降级；优先走"复制原 task 参数"。 |
| 权限 | 第一阶段对齐 cancel 端点（仅登录校验）；如需收紧到 repo 管理权限，后续补。 |

---

*本文为设计文档，纳入 git（docs/new-version2/event-redispatch-design.md）。*
