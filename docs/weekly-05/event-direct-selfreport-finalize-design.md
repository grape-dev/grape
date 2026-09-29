# 方案：direct 模式自报技能双信号收敛修复（补置 stream_ended）

> 状态：技术方案（2026-09-29）｜目标分支：weekly-05（实际部署 weekly-04-lyz）
> 关联：Seq #2205（vod/vod-cli）持续 failed 根因之二——direct 执行完成后自报技能（`grape tasks complete`）
> 双信号永不齐（缺 stream_ended）→ 事件被 no-output 分支误标 failed、结论不落库。

---

## 1. 现象（实证补充）

- `grape tasks complete` 到服务端 → `self_complete_task` → `set_task_signal("self_reported")` ✅（task `self_reported=1`）
- direct 模式（本机 `_execute_agent_inner` + `stream_devops_agent`）**没有 worker 的 `/api/runtime/tasks/{id}/complete` 回调** → `stream_ended` 永不为 1 ❌
- 双信号（#219）`task_double_signal_ready` 恒 False → `finalize_task` 不收敛 → task 停留 running/queued
- 引擎流结束后 `_execute_agent_inner` 走 `evaluate_agent_output` → `has_output=False`（自报技能的结果不在引擎文本输出中）→ 走 no-output 失败分支 → `update_task_status(failed)` + `update_event_status(seq,"failed")` → **事件 failed**
- 后续 `grape tasks complete` 迟到的 complete 因事件/task 已 failed 被幂等拒绝 → 结论不落库

**核心**：`stream_ended` 缺失是 direct 模式的**设计预期**（无 worker 回调），但自报技能的完成条件仍然要求它——导致**半终态死锁**（self_reported=1 但永不收敛）→ 误失败。

## 2. 修复目标（方案 A）

- **direct 模式**下，自报技能任务引擎流结束后，若 `self_reported` 已置（agent 已 `grape tasks complete`），**由 direct 端补置 `stream_ended`** → 双信号齐 → `finalize_task` 用已存的 `result_text`（结构化）收敛 → 正常 completed + `structured_output` 落库。
- 保持 worker 模式双信号语义不变（worker 仍由引擎流结束回调置 stream_ended）。
- 非自报技能 direct 任务走原有 `complete_task`/no-output 路径，不改变。

## 3. 方案设计

### 3.1 收敛时机与位置

`_execute_agent_inner` 在 `stream_devops_agent` 流结束后（`final_text` 已取得、任务流结束），补一段：

```python
# ── direct 自报技能双信号收敛（方案 A）：引擎流结束即 stream_ended ──
# worker 模式的 stream_ended 由远端 /api/runtime/tasks/{id}/complete 置位；
# direct 模式无 worker 回调，需在此补置，使 self-reported+stream_ended 双信号齐后 finalize_task 收敛。
if task_id and _has_output_or_self_reported:
    from src.runtime.service import (
        skill_self_reports, set_task_signal, finalize_task,
    )
    if skill_self_reports(skill_name):
        set_task_signal(task_id, "stream_ended")
        finalize_task(task_id, exit_code or 0)   # 双信号齐 → 用 self_reported 存的 result_text 收敛
```

前置条件：
- 仅在 `task_id` 存在时
- 仅在 `skill_self_reports(skill_name)` True（vod-cli 等声明 `grape tasks complete`）
- 若双信号已齐（self_reported 早到）→ `finalize_task` 立即收敛；若未齐（agent 尚未 complete）→ 仅置 stream_ended，等 `self_complete_task` 后续置 self_reported 时触发 finalize（原有 #219 收敛点）

### 3.2 与 no-output 分支配合

`_execute_agent_inner` 结尾 no-output 分支（`if not has_output`）需先检查「是否已进入终态/自报完成」：

```python
if not has_output:
    if task_id:
        _t = get_task(task_id) or {}
        # 自报技能已 self_reported → 交给双信号收敛，不标 failed
        if _t.get("self_reported") == 1 or _t.get("status") == "completed":
            return   # 跳过 no-output 失败
```

（3.1 已解析结构化的场景自然 has_output=True，走 completed；此处兜底自报但文本无标准 output 的情形。）

### 3.3 worker 模式不变

- worker 模式 `stream_ended` 由 `runtime.py /tasks/{id}/complete`（`set_task_signal("stream_ended")` + `finalize_task`）置位——保持。
- 仅 `_execute_agent_inner`（direct）分支补置，双向互斥（direct 与 worker 不会同时跑同一 task）。

## 4. 各模块改动

| 模块 | 改动 | 说明 |
|------|------|------|
| `src/api/event_pipeline.py` | 修改 `_execute_agent_inner` | 引擎流结束后补：自报技能 → `set_task_signal(task_id,"stream_ended")` + `finalize_task`；no-output 分支加 self_reported 已完成判断 |
| `tests/test_direct_selfreport_finalize.py`（新增） | 测试 | direct 自报收敛：self_reported=1 + direct 补 stream_ended → 双信号齐 → finalize_task 执行 → event completed + structured_output |

## 5. 测试

- UT-01：`set_task_signal("stream_ended")` 在 self_reported 已置后 → `task_double_signal_ready` True → `finalize_task` 收敛（模拟 direct 补置）
- UT-02：direct 引擎流结束 + 技能自报 → 事件 completed、structured_output 有值（等价 #2205 复现路径）
- UT-03：no-output 分支在 self_reported=1 时跳过 failed（不误标）
- 回归：worker 模式双信号（`test_event_*`）不变

## 6. 验收对照

| 项 | 结果 |
|----|------|
| direct 自报技能正常收敛 → 详情有答复结论 | ✅ 3.1 |
| direct 引擎成功即使无标准 output 也不误标 failed | ✅ 3.2 |
| worker 模式双信号不回归 | ✅ 3.3 |
| 事件 failed/挂起不再复现 | ✅ 3.1/3.2 |