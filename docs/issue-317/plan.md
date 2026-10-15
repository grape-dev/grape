# Plan: Issue #317 — 引擎异常退出错误信息补采与自愈

## 改动点（均在 `src/agent/executor.py`，与既有 session_missing / invalid_done 重试通道同构）

### 1. 遗言尾部采集（_run_once 内）
- 读循环中对每条 stdout 原始行（解析与 raw 均计入）推入 `_log_tail`（list），超限裁剪：**末 50 行 / 累计 4KB**；
- 最终 done（L794）处：`exit != 0 且 stderr 为空` 时，`errors` 追加
  `agent_logs tail (last 50 lines/4KB): ...`（经 `_redact` 脱敏）。

### 2. OOM 启发式判定（_run_once 内，仅 exit==-9 触发）
- 新增模块级 `_oom_kill_evidence()`：依次尝试 `dmesg -T` → `journalctl -k -n 200` →
  `/var/log/{syslog,messages,kern.log}` 尾部 4KB，命中 `out of memory` / `oom-killer` /
  `killed process` 关键词即认为有 OOM 证据（5s 超时、异常静默，仅 POSIX）；
- returncode == -9：有证据 → errors 标记 `suspected OOM (SIGKILL, kernel oom-killer evidence)`；
  无证据 → `killed by SIGKILL (no oom-killer evidence in system logs)`。

### 3. 异常退出自动重试一次（stream_agent 收尾段）
- 判定：`exit_code != 0 且 stderr 为空 且 未发生过任何重试`（session_missing 重试共享同一额度，
  防止叠加双跑）；
- 重试：`_run_once("")`（不带 session，与既有降级重跑一致），重试期间事件照常转发，
  仅最终 done 对外唯一；
- 幂等性：重试仍在同一事件执行内（pipeline 已通过 event_active_guard 原子抢占 (repo,issue,agent)
  守卫并持有至终态），不会产生重复事件/重复调度；
- 重试后的 done 已含第 1/2 点补采的遗言信息，正确反映在事件 errors。

## 测试（tests/test_agent_executor.py 新增 TestAbnormalExitRecovery，沿用 FakePopen 范式）
1. 空 stderr 补采：exit=1、stderr 空、stdout 有部分输出 → 不重试场景断言 errors 含 tail；
   重试成功场景断言最终 done 成功且 errors 不含 tail（成功路径不补采）；
2. OOM 标记：returncode=-9、stderr 空 → errors 含 SIGKILL 判定文案（mock `_oom_kill_evidence` 两分支）；
3. 重试幂等：首次异常退出 + 重试仍异常退出 → Popen 恰好 2 次、done 恰好 1 个、
   errors 含第二次运行的遗言；session_missing 重试后不再叠加异常重试。

## 风险与守卫
- 系统日志读取仅在 -9 异常路径触发，5s 超时 + 全异常吞掉，不阻塞主流程；
- tail/遗言均过 `_redact` 脱敏，token 不落 errors；
- 不改正常路径（exit=0）行为，现有测试回归保障。
