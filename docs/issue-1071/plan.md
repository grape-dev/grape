# 方案设计：Issue #1071 pending_self_report 超时兜底活跃流豁免

## 改动点一：`_sweep_pending_self_report_timeouts`（src/runtime/scheduler.py）

现状：候选 = `status='pending_self_report' AND pending_since 非空`，`parse_dt(pending_since) <= now - PENDING_SELF_REPORT_TIMEOUT_SECONDS` → 全链路判 `timeout_failed`。

改动：候选进入终态化循环**之前**，逐个做活跃流豁免检查：

```python
def _pending_self_report_stream_active(task_id: str, cursor) -> bool:
    """活跃流豁免判据（Issue #1071）：task_messages 最新一条写入距 now < 600s。

    返回 False 的情况（回退原判死逻辑，Spec：数据缺失不豁免）：
    - 表无该任务任何消息（执行流从未产出，无法证明在跑）；
    - 查询异常 / 表缺失 / 时间解析失败（保守：信息不足不豁免）。
    """
    try:
        row = cursor.execute(
            "SELECT created_at FROM task_messages WHERE task_id=? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,)).fetchone()
        if not row or not (row[0] or "").strip():
            return False
        from src.runtime.health import parse_dt
        _ts = parse_dt(row[0])
        if _ts is None:
            return False
        return (datetime.now(timezone.utc).timestamp() - _ts) < _PENDING_STREAM_ACTIVE_WINDOW_SECONDS
    except Exception:
        return False
```

- 常量：`_PENDING_STREAM_ACTIVE_WINDOW_SECONDS = 600`（模块级，与扫描周期匹配）。
- 调用位置：`_sweep_pending_self_report_timeouts` 内对每个已过 `_cutoff_ts` 的候选行、**在取行事务内**（复用同一 cursor，不额外加锁）调用；豁免命中则 `continue`（不 append 到待判死列表），下轮扫描再评估。
- 判死 reason 文案补充活跃豁免说明，便于日志对账。
- 既有注释更新：默认 30min → 90min + 活跃流豁免说明。

## 改动点二：阈值默认值（src/runtime/scheduler.py:32）

`PENDING_SELF_REPORT_TIMEOUT_SECONDS = int(os.environ.get("PENDING_SELF_REPORT_TIMEOUT_SECONDS", "1800"))`
→ 默认 `"5400"`。env 覆盖能力不变；测试内不依赖新默认（种子行直接给相对时间戳）。

## 改动点三：worker 成功上报遇终态拒收告警（src/runtime/service.py complete_task）

现状（2519-2530）：任务已 `failed/cancelled/timeout_failed` 时——结构化产出（actions+conclusion）→ override completed；否则 `log.info(skip complete_task)` 直接 return。

改动：`else` 分支前增加「成功上报 + timeout_failed 终态 + 执行流证据齐全」的告警判定：

```python
if task.get("status") == TASK_TIMEOUT_FAILED and exit_code == 0:
    _has_evidence = False
    try:
        with db_lock:
            _has_evidence = bool(db.cursor().execute(
                "SELECT 1 FROM task_messages WHERE task_id=? LIMIT 1", (task_id,)).fetchone())
    except Exception:
        _has_evidence = False
    if _has_evidence:
        log.warning(
            f"[pending_self_report 误杀对账] task {task_id} 已被超时兜底判 timeout_failed，"
            f"worker 上报成功（exit_code=0）且执行流证据齐全（task_messages 非空），"
            f"真实结果与终态相反；终态不翻转（幂等保护），请人工对账。"
            f"pending_since={task.get('pending_since')}, completed_at={task.get('completed_at')}")
```

- 不改变任何状态写入路径：override 例外、普通 skip 全部保留（Out of Scope：终态不可翻转）。
- 日志级别 `warning`（Spec：告警日志），含 task_id / pending_since / completed_at 时间线。

## 改动点四：测试（tests/test_issue_1071_pending_stream_exemption.py）

沿用 test_issue_537/588 的种子函数模式（独立 runtime/cred/repo_key id 段，互不污染）：

- T1 活跃流豁免：pending_since=1900s 前 + task_messages 最新写入 60s 前 → 不判死（RED：现实现会判 timeout_failed）；
- T2 沉默判死保留：pending_since=1900s 前 + 消息写入 700s 前 → 照常 timeout_failed 全链路；
- T3 无消息回退：pending_since=1900s 前 + 零消息 → timeout_failed；
- T4 阈值默认：`PENDING_SELF_REPORT_TIMEOUT_SECONDS` 默认 5400；
- T5 阈值边界：pending_since=5300s 前 → 不判死；5500s 前 + 沉默 → 判死；
- T6 告警日志：timeout_failed + 成功上报 + 有消息 → caplog 命中「误杀对账」且状态不变；
- T7 回归：普通 pending→completed 自报、timeout_failed 幂等拒收（复刻 #537 语义在新阈值常量下）。

## 提交切分（TDD 红/绿分提交）

1. `test(#1071): 活跃流豁免/沉默判死/阈值/告警 先写失败测试`（红：T1/T6 在现实现下失败）
2. `feat(#1071): sweep 活跃流豁免 + 阈值 1800→5400 + 上报终态拒收告警`（绿）
3. `docs(#1071): spec/plan/task 文档`

## 影响面

- 仅 `src/runtime/scheduler.py`（sweep 判死 + 常量）、`src/runtime/service.py`（complete_task 拒收路径告警）、新增测试、docs/issue-1071/。
- 不改：executor terminate 写入、update_task_status 终态幂等、validate_run_token、zombie reap、event_pipeline。
