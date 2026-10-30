# Plan: Issue #537 — pending_self_report 状态机 + 收口防误判

> 依赖顺序：状态常量（service） → db_setup 迁移 → executor 停机置 pending →
> scheduler 超时/zombie/poll → deploy_evidence → 前端图标 → 测试收口

## 切片清单（TDD 红绿拆分）

### S1 · 状态机常量 + db_setup 列迁移（基础层）
- service.py：`TASK_TERMINAL_STATUSES`（含 timeout_failed）/ `TASK_ACTIVE_STATUSES`（含
  pending_self_report）常量；`_TASK_COLUMNS_LIST` 补 pending_since；enqueue insert vals
  补 pending_since=None。
- db_setup.py：DDL status 注释扩展 + `_ensure_column(agent_tasks, pending_since, TEXT DEFAULT NULL)`。
- 红：test_task_insert_row 列契约（pending_since 在清单中，insert 值数对齐）。

### S2 · executor 停机置 pending_self_report（P0-1 主体）
- terminate_active_agents REMOTE 分支：UPDATE status='pending_self_report', pending_since=now。
- 红：停机后 REMOTE 任务 status=pending_self_report、pending_since 非空、busy 不释放、事件 running。

### S3 · service 状态机放行（P0-1 配套）
- update_task_status / complete_task / fail_task / validate_run_token 终态集扩 timeout_failed；
  validate_run_token pending → ok。
- _real_active_count / local_claimer._engine_busy 计入 pending_self_report。
- requeue_task / reap_stale_runtimes 不回炉 pending_self_report。
- 红：pending→completed 转换全链路（completed_at/busy/token 失效）；validate_run_token 分流。

### S4 · scheduler：pending 超时回收 + zombie 离线宽限 + poll 放行（P0-2）
- scheduler_loop zombie recovery 段后加 pending 超时扫描；offline 判死加
  last_seen_at 离线时长 > ZOMBIE_OFFLINE_GRACE_SECONDS（默认 300）；新增
  pending_self_report 分支（在线等待/离线+超时判 timeout_failed）。
- _poll_dispatched_completion：pending_self_report 视同 running 继续 wait。
- 红：超时判 timeout_failed 全链路（agent_tasks+events+autopilot+busy）；短离线不判死；
  长>5min 离线判死；poll 不写 failed。

### S5 · deploy_evidence 收口钩子（P1-1）
- 新模块 src/runtime/deploy_evidence.py；poll/zombie-recovery 的 success 分支前置调用。
- 红：非部署 skip / 版本指纹 pass / 版本不匹配 fail / health 不可达 skip / 收口接入分流。

### S6 · 前端状态图标（最小）
- observability.js 两处映射补 pending_self_report / timeout_failed。

### S7 · 回归 + 全量测试
- test_shutdown_force_fail 现有用例不破坏（REMOTE 用例断言从 running 改 pending_self_report——
  行为变化即测试预期变化，PR 说明）。
- #308/#320：terminate_event 源码断言不变。
- 全量 pytest 通过。

## 风险与对策
- pending_since 复用歧义 → 新列，无既有消费方，零回归面。
- timeout_failed 新终态值遗漏消费方 → 常量集中定义，grep 全部字面量终态集合逐一核对（已完成扫描：
  service.py 6 处、scheduler 2 处、routes 少量只读展示）。
- evidence 校验外部依赖（health URL）→ 全部降级 skip + warning，绝不阻断收口。
