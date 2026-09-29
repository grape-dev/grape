# 方案：redispatch 后事件终态可重写——修复「重派成功执行但结论不落库」

> 状态：技术方案（2026-09-29）｜目标分支：weekly-05（实际部署 weekly-04-lyz）
> 关联：生产 Seq #2205（hello-calculator-14/#7，vod）——redispatch 后引擎成功执行（`opencode done: exit=0` + agent `grape tasks complete`），
> 但事件仍 failed、`structured_output`/`result` 为空、详情无「处理记录/答复结论」。

---

## 1. 现象

Seq #2205（issue_opened/vod，hello-calculator-14/#7）：

- 用户 **redispatch 3 次**（07:38 / 07:42 / 07:47），每次都是**本机 direct 执行**（`[redispatch] seq=2205 direct execution started on rid=24`）
- 引擎 **opencode 成功**：`[Agent:vod ... engine:opencode-cred0] opencode done: exit=0 text_len=373`
- agent 执行了 `grape tasks complete --json`（完整 result_text/actions/conclusion/artifacts）
- **但事件最终 status=failed**、`agent_logs` 有（实时日志 OK）、`structured_output=NULL`、`result=''`
- 关键日志（3 次一致）：`Event 2205 already failed, skip failed overwrite (result=)`
- 关联 task：`t_900d52e63bf6/t_819922a5be64/t_d5097fa9789f` 均 `status=failed`、`stream_ended` 有值、`self_reported` 未置、`result_text=''`、`runtime_id=24`

## 2. 根因（深挖实证）

### 2.1 事件为何 failed

事件 #2205 的 **首次 failed** 发生在最早一轮派发（webhook issue_opened 入队时 worker 不可达——远端 worker 已删除、任务 queued 超时 → 事件 failed）。此后 redispatch 复用**同一事件 seq** 重跑，但：

- redispatch 本机 direct 分支（`events.py` redispatch_event local_only 路径）调 `update_event_status(seq, "running")`
- 但 `update_event_complete`（`event_store.py`）的 **终态幂等保护**：
  ```
  if row[0] in (completed,failed,cancelled) and status in (completed,failed,cancelled): skip
  ```
  事件已 failed → `opencode done` 后的 `complete_task` 里 `update_event_complete(2205,"completed", structured_output=...)` **被跳过** → 结构化产出不落库。

### 2.2 task 为何 failed / self_reported 未置

- task 由 `grape tasks complete`（`self_complete_task`）处理：若 task 已在 failed 终态 → `return already`（不置 self_reported、不存 result_text）
- `stream_ended` 来自引擎流结束回调；因 self_reported 未置 → **双信号（#219）不齐** → `finalize_task` 不收敛
- 三方（引擎 done=0 / agent complete / 事件)时序竞争，终态各自 fail/被拦

### 2.3 本质

`redispatch`（重跑）**复用了已 failed 的事件终态**，但事件状态机**不因重跑重置** → 后续成功执行无法写入。这是**事件生命周期缺陷**（redispatch 语义 = "重试"，应使事件可重写），非引擎失败、也非 `grape tasks complete` 未执行。

---

## 3. 修复目标

- redispatch（重派/重试）时应**重置事件终态为可重写态**（failed → running），使重跑成功后的 `complete_task` 能写 `structured_output` / `result` / `status=completed`。
- 保持既有终态保护**不回归**：普通（非重试）场景下完成→失败/失败→完成的覆盖仍被拒。
- task 层面：redispatch 新任务从「非终态」开始，agent 延迟的 `grape tasks complete` 不再被 already 拒（若新 task 正常 running/queued）。

## 4. 方案设计

### 4.1 核心：redispatch 时清除事件终态（置 running）

在 `redispatch_event` 两处任务重建分支（local_only L466-500 与主分支 enqueue 后）**先重置事件状态**：

```python
# 事件重派前：清除终态，置回 running（允许后续 complete 覆盖）
def _mark_event_redispatched(seq: int):
    """redispatch 语义：事件视为重新处理中，清终态、置 running（不落 structured_output）。"""
    from src.api.event_store import update_event_status, reset_event_terminal
    reset_event_terminal(seq)          # 见 4.1.1
    update_event_status(seq, "running")
```

**4.1.1 新增 `reset_event_terminal(seq)`（event_store.py）**：
- 无视终态保护，将 `status` 置 `running`，并**清空结构化产出**（`structured_output=NULL`, `result=''`）——避免旧 failed 的残留结论与重跑结果混淆。

```python
def reset_event_terminal(seq: int):
    """redispatch 专用：无视终态保护重置事件为 running 并清 structured_output/result。"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "UPDATE events SET status='running', exit_code=NULL, completed_at='', "
                "structured_output=NULL, result='' WHERE seq=?",
                (seq,))
            db.commit()
        except Exception as e:
            log.warning(f"reset_event_terminal failed seq={seq}: {e}")
```

### 4.2 调用点

- `redispatch_event`（`events.py`）**local_only 分支**（L466 前后）与**主分支**（非 onboarding，enqueue 成功后）都调用 `reset_event_terminal(seq)` —— 放在 `enqueue_task` 成功后、`update_event_status(seq,"running")` 处统一替代。

### 4.3 update_event_status 保持不动

现有终态保护（`update_event_status` L386-409）保留——普通终态覆盖仍被拒；reset 走专用强力重置，不绕过其他保护。

### 4.4 local_only 早停保护

local_only 分支在引擎健康校验失败时 return 4xx（不 reset），避免"未实际重派却清结论"。

---

## 5. 各模块改动

| 模块 | 改动 | 说明 |
|------|------|------|
| `src/api/event_store.py` | 新增 `reset_event_terminal(seq)` | redispatch 专用：重置 running + 清 structured_output/result |
| `src/api/routes/events.py` | `redispatch_event` local_only 分支 L491 与主分支 | enqueue 成功后调 `reset_event_terminal` + `update_event_status(running)` |
| `tests/test_event_redispatch.py`（新增） | 测试 | redispatch 重置 + 后续 complete 能写 structured_output |

## 6. 测试

- UT-01 `reset_event_terminal`：failed 事件 → running + structured_output 清空
- UT-02 redispatch(local) 后 complete_task 写入成功：failed→redispatch→running→complete→completed 且 structured_output 有值
- UT-03 非 redispatch 普通 complete 仍受终态保护（failed 不被 completed 覆盖）
- 回归：现有 redispatch/vod 事件测试 `tests/test_event_structured.py` 等

## 7. 验收对照

| 项 | 结果 |
|----|------|
| redispatch 后成功执行的事件 → 详情有「处理记录 + 答复结论」（structured_output 落库） | ✅ 4.1 |
| 普通 failed 事件不被误覆盖（非重试） | ✅ 4.3 |
| 不回归终态保护 | ✅ 4.3 / UT-03 |