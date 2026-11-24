# Issue #4023 需求文档：基线失败日志七要素不再被截断，并结构化落库与同因打点

> 基线：`arch-weekly-11-ljt`（开发分支 `feat/issue-4023`）

## 1. 现象

`Task failed ... err={str(error)[:120]}` 的 120 字符上限恰好切在 `expected_baseline=`
处，`WorkspaceBaselineError` 承诺的六要素（#2514「错误文本六要素可照抄定位」）
在唯一的运行失败日志出口失效；同时 worker 只回传 `str(error)` 字符串，
服务端无结构化字段，任务详情 `errors` 只能存半截文本，同因反复失败也无聚合视角。

## 2. 目标与验收

1. 基线异常 → `WorkspaceBaselineError.format_one_line()` 固定七要素（issue/path/
   expected_baseline/actual_head/ahead/behind/reason；发生基线回退时追加
   requested_baseline），**不截断**；对象与 worker 上报字符串两条来源同构
   （`parse_baseline_error_text` 按 key=value 还原）。
2. 其他异常 → 日志上限 `GRAPE_FAILURE_LOG_LIMIT`（默认 500、下限 400），超限追加
   `…(truncated N chars)` 显式标记，杜绝「半截单词无标记」。
3. 完整错误以 JSON 优先写入 `agent_tasks.errors`（`error_code=WORKSPACE_BASELINE_MISMATCH`
   + to_dict() 全字段），兼容既有 dict 消费方。
4. 同 issue 同 reason 连续失败达 `GRAPE_BASELINE_REPEAT_THRESHOLD`（默认 3）在
   `GRAPE_BASELINE_REPEAT_WINDOW_SECONDS`（默认 3600）窗口内 → ERROR 打点
   `baseline_mismatch_repeat`（含 count/threshold/window/首末时间/全字段）；
   任务成功完成即清空该 issue 失败链。
5. 用例 `tests/runtime/test_issue_4023_baseline_failure_visibility.py`，并接入
   CI 门禁 `.github/workflows/lock-regression.yml`（复审 Required 口径）。

## 3. 边界与风险声明

- **打点为单进程内计数**（`_baseline_failure_state` 进程内 dict + 锁），**跨进程不做
  聚合**；多实例部署下各进程独立计数，聚合交由日志采集侧（Loki 按打点行聚合）。
  达到阈值的打点可能被同 issue 的其他成功打断而延迟，不会漏报持续不一致。
- 成功重置按 issue 全量清链、不区分 agent_type：语义是 Issue 级「工作区基线持续
  不一致」，任何 Agent 在该 Issue 上成功完成即视为工作区恢复。
- `parse_baseline_error_text` 仅在解析出 issue + reason 时才判定为基线错误；
  仅有前缀的半截文本按普通异常走截断护栏，不落 WORKSPACE_BASELINE_MISMATCH。
- 失败原因 `result_text` 复用 `_err_log`（有界 + 截断标记），不再 `[:500]` 硬截断。
