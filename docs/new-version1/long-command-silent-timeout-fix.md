# 长命令静默导致 Agent 超时强杀：根因与修复（seq #501）

> 版本：v1.0
> 日期：2026-08-11
> 状态：已修复（executor 延长窗口 + skill 约束）

---

## 〇、现象

- seq #501（#8 coding，新增 PR 自动合入功能）：task failed（exit=-9，duration 1168.6s）。
- agent 执行到 Step 41 后，**600s 无任何输出** → executor 无输出超时强杀。

## 一、根因

agent 在 Step 38/39/40 连续执行 `pip install langgraph`（venv，依赖链大，命令带 `timeout 300000`=5min），
Step 41 开始后 opencode 等待 pip 长命令返回，**期间不产出任何 NDJSON 事件**（无 step/text/tool_use）。

executor 的「无输出 600s 超时」（`max_exec_seconds` 默认 600）将「长命令执行中的静默」误判为「进程卡死」→ SIGTERM 强杀 → exit=-9。

**与既有根因的区别**：
| 根因 | 场景 | 状态 |
|---|---|---|
| session 复用 → cwd 错位 | 多 Issue 共用 opencode session | 已修（3c86294） |
| 事件循环阻塞 | `stream_runtime` 同步迭代阻塞 claim/heartbeat | 已修（9899b7f） |
| **长命令静默超时**（本文） | opencode 执行 pip install 等长命令，无输出被误杀 | **本文修复** |

## 二、修复

### 1. executor 延长窗口（`src/agent/executor.py`）

无输出超时分支**先判断进程是否存活**：
- `proc.poll() is None`（进程仍活着，长命令/长思考中）→ **重置静默计时继续等待**，不立即强杀；
- 但受**硬上限** `AGENT_SILENT_EXTEND_CAP`（默认 `max_exec_seconds × 3` = 1800s）约束，防止进程存活死循环永久挂起；
- 仅当 `proc.poll() != None`（进程已退出但无 done，异常）才真正强杀。

```python
if elapsed_silent > max_exec_seconds:
    if proc.poll() is None and _silent_extend_total < _silent_extend_cap:
        _silent_extend_total += elapsed_silent
        log.info(f"... silent {elapsed_silent:.0f}s but process alive, extending window ...")
        _last_output = time.time()
        time.sleep(0.1)
        continue
    log.warning(f"... no output for {max_exec_seconds}s, terminating")
    ...
```

### 2. skill 约束（`skills/implementation/SKILL.md` 安全硬约束第 6 条）

- 长命令（`pip install`、`git clone`、大测试）运行期间 opencode 无输出；
- 执行时**加 `timeout` 限制（如 `timeout 300 ...`）**、一次只装必要依赖；
- 命令可能超长时先输出说明再执行。

## 三、测试

`tests/test_executor_silent_extend.py`：
- `test_alive_process_not_killed_until_extend_cap`：进程存活 + 无输出 → 延长窗口不立即 -9；cap 耗尽才强杀；
- `test_dead_process_fails_fast`：进程已退出但无 done → 不延长，直接强杀。

## 四、验证

- 回归：`test_executor_hang_fallback` / `test_executor_silent_extend` / `test_event_loop_block` / runtime 系列全通过（57 passed）。
- 生产：worker 重启后，长命令（如 pip install）执行期间 opencode 进程存活 → 不再被 600s 误杀；仅进程真退出无 done 才强杀。
