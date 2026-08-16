# Issue #704 任务中心状态未更新根因分析与修复方案

> 版本：v2.1（聚焦任务中心 `tasks` 修复）
> 日期：2026-08-16
> 状态：分析结论 + 修复方案（待实施，本文档不涉及代码改动）
> 范围：`Jason7602/hello-html2` 仓库 `issue` agent 处理 Issue #2（`events.seq=704`）。issue agent 已执行并输出"标记 ready、指派 @grape_bot"，但**任务中心 `tasks` 表该任务仍为 `processing`、assignee=Jason7602**，从未更新。

---

## 一、现象与结论

任务中心里 Issue #2 的任务（`tasks.id=26`）一直 `processing`、assignee=Jason7602，多次运行后仍未变化。

**根因（一句话）**：`issue-advance` 技能的"任务通知硬规则"把 `/api/tasks/notify` 限定为"责任人是**人**时才调用"，而"评级 ready→分配给机器人 grape_bot"——分配对象是机器人时 notify 被跳过 → 服务端既不会关闭旧任务（`bot_takeover`），也不会创建/重派任务 → **`tasks` 表永远不更新**。

> 注意区分：`assigned @grape_bot` 是指 GitCode Issue 的 `assignee`（agent 调 PATCH issues API 完成），与任务中心 `tasks` 是两套东西；任务中心要更新必须走 `/api/tasks/notify`（或 `/api/tasks/close`）。

---

## 二、任务中心 `tasks` 的数据流（修复落点）

### 2.1 表结构
```
tasks
├─ id / user_id / assignee / repo_key / issue_number
├─ title / task_description / conversation_id
├─ status            -- active / processing（前端派生） / completed
├─ completed_reason  -- reassigned / bot_takeover / issue_closed
└─ created_at / completed_at
```

### 2.2 更新入口（`src/api/routes/tasks.py`）
| 入口 | 触发 | 行为 |
|---|---|---|
| `POST /api/tasks/notify`（`tasks.py:228`） | issue agent 处理结束、责任人确定 | assignee=**bot** → `_close_issue_tasks_bot_takeover`（旧任务→completed/bot_takeover，不建新任务）；assignee=**人** → `_close_other_assignee_tasks` + `_create_task`（重派/新建）；未注册平台 → 400 |
| `POST /api/tasks/close`（`tasks.py:330`） | 关闭 Issue | 该 Issue 全部 active/processing → completed/issue_closed |
| 内部 `_close_issue_tasks_bot_takeover`（`tasks.py:109`） | notify 收到 bot assignee | `UPDATE tasks SET status='completed', completed_reason='bot_takeover'` |
| 内部 `_close_other_assignee_tasks`（`tasks.py:96`） | 重派给人 | 其它 assignee 的 active → completed/reassigned |

### 2.3 生产现状（hello-html2 Issue #2）
```
tasks.id=26  assignee=Jason7602  status=processing  completed_reason=''
服务端日志：多次运行窗口（08-15 23:28-23:40、08-16 00:05-00:09）均无 POST /api/tasks/notify
对照：hello-flask / hello-calculator2 的 agent 会 curl /api/tasks/notify → 200 OK → tasks 正常创建
```

---

## 三、根因（任务中心角度）

`skills/issue-advance/SKILL.md` 两条规则自相矛盾：

1. **任务通知硬规则（第 29-46 行）**
   > 责任人（**人**，非 gitcode_bot 机器人账号）最终确定后，**必须调用 `POST /api/tasks/notify`** 创建任务；唯一例外：完全未确定责任人。

2. **能力表·分配负责人（第 145 行）**
   > ready→机器人，非 ready→项目责任人。

**矛盾链**：ready → 分配 `grape_bot`（机器人）→ 按硬规则"责任人是机器人，不属于 notify 范畴" → **notify 不调用** → 旧任务（processing）不被 `bot_takeover` 关闭、也不重派 → `tasks` 卡死。

**佐证**：08-16 08:09 agent 日志自我质疑：
> "…确定排除了机器人账号：'机器人'（grape_bot / gitcode_bot 机器人账号）'那么我把 Issue 分配给 grape_bot（机器人账号）…然后再调用 tasks/notify 把任务交办给开发…"

---

## 四、修复方案（任务中心 `tasks`）

### 4.1 技能规则理顺（核心，改 `skills/issue-advance/SKILL.md`）
把"任务通知硬规则"改为：**处理结束、责任人最终确定（含机器人接管）后，一律调用 `POST /api/tasks/notify`**：

| 场景 | notify.assignee | 服务端行为（已支持） |
|---|---|---|
| ready → 分配给 grape_bot（机器人接管） | `grape_bot` | `bot_takeover` 关闭旧任务（completed_reason='bot_takeover'） |
| 非 ready → 分配给项目责任人（人） | 该人 GitCode 用户名 | 旧任务 completed/reassigned + 创建新任务 active |
| 完全未确定责任人（关闭/等待配置） | 不调用 | 例外保留 |

> 语义对齐：`notify` 是"处理结束上报责任人"的唯一入口；机器人接管 = 关闭旧任务，人 = 创建/重派。这消除了"分配机器人但任务不更新"的死角。

### 4.2 服务端确认（`tasks.py`，一般无需改，若需可增强）
- `notify` 对 bot assignee 走 `_close_issue_tasks_bot_takeover` 已实现（`tasks.py:245-249`），验证返回值 `{ok:true, message:"…已接管，关闭 N 条待处理任务…"}` 可观测。
- （可选增强）`bot_takeover` 关闭后，若该 Issue 后续又分配给某"人"，由 `notify` 正常 `_create_task` 新建——现状已支持，无需额外改动。
- 补一条 `test_tasks` 用例：`assignee=grape_bot` 时 notify 关闭旧任务（bot_takeover），且不产生新任务。

### 4.3 验收用例（任务中心）
| ID | 场景 | 预期 |
|---|---|---|
| TC-1 | issue agent ready → notify(assignee=grape_bot) | `tasks` 旧任务 → completed/bot_takeover |
| TC-2 | 非 ready → notify(assignee=人) | 旧任务 completed/reassigned；新建 active 任务给该人 |
| TC-3 | 未注册平台责任人 → notify | 400/ok:false，任务保持原状，agent 评论说明 |
| TC-4 | 关闭 Issue → `/api/tasks/close` | 全部 active/processing → completed/issue_closed |
| TC-5 | 重复 notify（幂等） | 不重复建任务、不重复关闭 |

---

## 五、关联问题（任务中心之外，另行处理，不在本文修复范围）

为保持文档聚焦，下列问题仅记录，修复另立：

| 问题 | 位置 | 说明 |
|---|---|---|
| `agent_tasks` 孤儿（runtime_id=0 永久 queued） | `service.py:343` stale 回收置 0 | 回收应保留原 runtime_id 或置 failed |
| 事件 `completed_at` 为空 | `service.py:534` 未传 completed_at | 补传时间戳 |
| worker `started_at` 缺失 | `agent.py` task_start 时序 | 首事件即上报 |
| coding 链路中断（exit_code=-15） | seq=711 | 23:37 服务重启窗口被 SIGTERM 杀 |

---

## 六、实施建议

1. 先改 `skills/issue-advance/SKILL.md` 的 notify 硬规则（§4.1）——一行语义修正，风险低，直接打通任务中心更新。
2. 补 `tests/test_tasks.py` 的 bot_takeover 用例（§4.2/TC-1）。
3. 验证一条真实事件：issue agent ready → 任务中心 Issue #2 显示 completed/bot_takeover。

---

## 附：相关代码速查

| 文件 | 位置 | 说明 |
|---|---|---|
| `skills/issue-advance/SKILL.md` | `:29-46` / `:145` | notify 硬规则（仅"人"）与 ready→机器人（矛盾根因，修复点） |
| `src/api/routes/tasks.py` | `:109-123` / `:228-249` | `bot_takeover` 关闭 / notify 分发（已支持 bot 分支） |
| `src/api/routes/tasks.py` | `:330-343` | `/api/tasks/close`（issue_closed） |
| `tests/test_tasks.py` | — | notify/close/bot_takeover 用例 |
