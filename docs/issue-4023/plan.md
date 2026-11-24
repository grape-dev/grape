# Issue #4023 方案设计

## 1. 结构

| 件 | 位置 | 职责 |
|---|---|---|
| 异常单行化 | `repogit.WorkspaceBaselineError.format_one_line()/to_log_fields()` | 固定七要素、不截断；与 `str(error)` 同源 |
| 字符串还原 | `repogit.parse_baseline_error_text()` | worker 上报文本按 key=value 还原结构化字段；缺 issue+reason → {} |
| 失败出口 | `service._format_failure_error()` | 统一产出 (log_text, structured)；非基线走 `GRAPE_FAILURE_LOG_LIMIT` 截断护栏 |
| 打点/重置 | `service._record_baseline_failure()/_reset_baseline_failure()/_baseline_issue_key()` | 进程内连续计数 + 成功清链，key 去掉入队侧 `#` 前缀归一 |
| 落库 | `fail_task()` | `_err_struct` JSON 优先写 `agent_tasks.errors`（合并保留 TRANSIENT_RETRY_*），`result_text` 复用 `_err_log` |

## 2. 关键取舍

- 打点状态放进程内 dict（不建表不加锁开销），跨进程聚合交日志采集侧——见 spec §3。
- errors 合并沿用「首条不挤掉」既有口径，`[:5]` 上限不变。
- CI 门禁复用 `lock-regression` job（同 #3963 口径），追加本单用例文件。
- `.env.example` 登记 `GRAPE_FAILURE_LOG_LIMIT` / `GRAPE_BASELINE_REPEAT_THRESHOLD` /
  `GRAPE_BASELINE_REPEAT_WINDOW_SECONDS`。

## 3. 测试策略

`tests/runtime/test_issue_4023_baseline_failure_visibility.py`：七要素不截断（对象/字符串两源）、
requested==expected 时字段仍存在、前缀半截文本不判基线、超限截断带标记、结构化落库、
3 次触发/2 次不触发、reason 变化重置、key 归一与成功清链。
