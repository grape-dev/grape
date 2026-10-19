# Plan: Issue #369 — 判死路径统一完整回写关联事件

## 判死置 failed 路径盘点（service.py 全量）

| # | 路径 | 位置 | 原事件回写 | 处置 |
|---|------|------|-----------|------|
| 1 | `_fail_task_pool_unreachable`：认领判死（绑定引擎全不可达） | service.py `_fail_task_pool_unreachable` | 仅 `update_event_status`（status） | ✅ 改 `_sync_event_failed` |
| 2 | `_fail_task_pool_unhealthy`：健康分不足超时判死 | service.py `_fail_task_pool_unhealthy` | 仅 `update_event_status` | ✅ 改 `_sync_event_failed` |
| 3 | dispatched 超时回收 attempts≥50 判死 | service.py 回收块（原 1045 附近） | 仅 `update_event_status` | ✅ 改 `_sync_event_failed` |
| 4 | `fail_task`（worker 上报失败） | service.py `fail_task` | `update_event_complete`（完整）+ 归档 | 无需改 |
| 5 | `complete_task` exit_code≠0 | service.py `complete_task` | `update_event_complete`（完整） | 无需改 |
| 6 | `update_task_status(task,"failed")` 通用入口 | service.py | 调用方（4/5）负责事件回写 | 无需改 |
| 7 | runtime/cleanup.py 判死清理 | cleanup.py | 直接 SQL 完整写 status/exit_code/completed_at/result | 无需改（已完整） |
| 8 | event_pipeline 入队/启动失败 | event_pipeline `_fail_event` | 完整（本 Issue 对齐的语义源） | 无需改 |
| 9 | 停机清理 DIRECT 任务判死 | agent/executor.py `terminate_active_agents` | 仅 `update_event_status`（status） | ✅ 改 `update_event_complete` 完整回写 |
| 10 | recover-orphans attempts 封顶判死 | api/routes/runtime.py `runtime_recover_orphans` | 完全无事件回写 | ✅ 封顶判死后补 `update_event_complete` 完整回写 |

## 方案
新增统一 helper `_sync_event_failed(seq, reason, exit_code=1, completed_at="")`
（对齐 `event_pipeline._fail_event` 语义），内部调 `event_store.update_event_complete`
写 status/result/exit_code/completed_at。

幂等/双主 scp 同步：`update_event_complete` 自带终态保护（terminal→terminal 跳过），
双主两侧重复回写同终态不冲突、不覆盖已 completed 结果（防迟到 failed 信号污染）。

## 测试
新增 `tests/test_issue_369_failed_event_writeback.py`（8 例）：认领判死、健康分超时、
helper 直调、终态保护（completed 不被覆盖）、failed 重复回写幂等、空 seq 安全、
停机清理判死（executor）、recover-orphans 封顶判死。
