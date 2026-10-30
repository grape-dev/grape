# Spec: Issue #537 — 自动化升级任务停双主终态误报 failed

> 基线分支：weekly-09 | 组合方案：候选 1（主）+ 候选 3（纵深防御）；候选 2（P2 可选）不做（超出代码范围，属 skill 层）
> 来源：Issue #537 需求 + issue agent Spec（Issue 评论）

## 1. 问题

升级类自动化任务（autopilot_executions）执行「停双主/重启 master」步骤时，master 退出触发
`terminate_active_agents`。#404 修复（2026-10-15）已让 REMOTE 任务（worker 承载，credential_id>0）
不再被标 failed，但只「跳过」留下三个残留缺口：

1. **悬空状态**：REMOTE 任务保持 running/dispatched，无任何标记说明「主控重启了、等 worker 自报」。
   下游（zombie recovery、心跳判死、_real_active_count）无法区分「正常跑」与「等自报」。
2. **重启窗口误判**：master 重启期间 worker 心跳断（3 次）→ `_reregister` → 引擎行被标
   offline / zombie recovery 的 `_rt_status != 'online'` 判死 → agent_tasks/autopilot_executions
   被同步 failed，终态定格。之后 worker 侧任务跑完调 complete_task 被终态幂等保护拒绝 →
   **永败误报**（生产实例：某次 exec 任务，实际升级已完整落地）。
3. **收口只看 status**：autopilot 收口（_poll_dispatched_completion / zombie recovery）只信
   agent_tasks.status，不校验部署类任务的实际落地证据 → 既可能 false-failed（本 Issue）也可能
   false-success（status=completed 但部署实际没落地）。

## 2. 状态机设计

### agent_tasks.status 新增中间态 `pending_self_report`

```
queued → dispatched → running ──(主控停机, REMOTE)──→ pending_self_report ──┬─→ completed  (worker 自报, exit=0)
                                                                            ├─→ failed      (worker 自报, exit≠0)
                                                                            └─→ timeout_failed (超时 30min 无自报)
```

- `pending_self_report` 语义：**任务进程活在 worker 上，主控已重启/停机，等待 worker 侧自报终态**。
- 进入：仅 `terminate_active_agents` 对 REMOTE 任务设置（替代 #404 的"跳过留悬空"）。
- 退出：worker 自报（complete_task/fail_task 正常转换）或超时判 timeout_failed。
- `timeout_failed` 是 agent_tasks 的新终态值：**带说明的准确终态**（区别于 plain failed），
  autopilot 侧翻译为 failed + 明确 error 文案（"worker 30 分钟未自报终态"）。
- 事件侧（events 表）：不新增状态值——`pending_self_report` 期间事件保持 running（与 worker 实际
  执行一致）；worker 自报时 complete_task 照常写事件终态；超时判死走已有 force-fail 回写路径。

### 幂等保护放行（P0-1 第 3 点）

- `update_task_status` / `complete_task` / `validate_run_token` 的终态集合从
  `("completed", "failed", "cancelled")` 扩为含 `timeout_failed`；
- `pending_self_report` **不属于终态**：worker 自报 complete_task 时允许
  pending_self_report → completed（update_task_status 的终态幂等只拦真终态，天然放行，
  需补显式分支说明）；complete_task 开头的 failed/cancelled 幂等跳过分支不含 pending_self_report，
  天然放行。
- `validate_run_token`：任务处于 pending_self_report 视为活动态（"ok"），不拒绝自报。

## 3. 各模块改动

### 3.1 src/agent/executor.py — `terminate_active_agents`（P0-1）

REMOTE 任务（runtime_id>0 且 credential_id>0）不再仅跳过，改为：

```
UPDATE agent_tasks SET status='pending_self_report'
  (不动 exit_code/result_text/self_reported；不释放 busy 槽——任务仍占 worker 槽)
事件侧：事件保持 running（不写终态，等 worker 自报）
```

同时把该任务列入本次停机的"自报等待名单"（见 3.4 超时回收）。

超时判定不放这里（停机函数应尽快返回），由 scheduler 的 pending 自报超时回收承担（3.4）。

### 3.2 src/runtime/service.py — 状态机放行（P0-1 配套）

- 模块常量 `TASK_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "timeout_failed")`
  与 `TASK_ACTIVE_STATUSES`（含 pending_self_report）导出，供各处引用，防字面量漂移。
- `update_task_status`：终态集合引用常量（含 timeout_failed）；pending_self_report→completed
  转换照常走（写 completed_at、释放 busy 槽、revoke token、健康度埋点）。
- `complete_task`：幂等保护分支 `task.status in ("failed","cancelled")` 增含 `timeout_failed`
  （timeout_failed 后迟到自报仍走既有"结构化产出覆盖"例外通道）；pending_self_report 放行。
- `validate_run_token`：pending_self_report → "ok"（活动态）；终态含 timeout_failed → "noop"。
- `fail_task`：幂等分支终态集含 timeout_failed。
- `_real_active_count` / local_claimer `_engine_busy`：pending_self_report **计入** 活跃数
  （worker 槽仍被占，任务没死，不能放新任务挤占）——`IN ('running','dispatched','pending_self_report')`。
- `requeue_task` / `reap_stale_runtimes` 的 running/dispatched 回收：**不碰** pending_self_report
  （引擎心跳断了也不回炉重派——任务进程还在 worker 上跑，回炉 = 双跑）。

### 3.3 src/runtime/scheduler.py — zombie recovery 防护 + pending 超时（P0-2 + P0-1 超时）

**zombie recovery 两处加固：**

1. agent_tasks=failed 分支：错误文案透传 result_text（保持现状），无改动需求——
   真失败照旧同步 failed。
2. running/dispatched + engine offline 判死分支：**加"引擎离线持续时长"校验**——
   读 agent_runtimes.last_seen_at，只有离线时长 > 阈值（`ZOMBIE_OFFLINE_GRACE_SECONDS`，
   默认 300s）才判死。重启窗口（几分钟）内 last_seen_at 刚过线 → 不判死，等下一轮。
   - 说明：offline 状态本身由 reap_stale_runtimes 写入（心跳 > 100s 即标 offline），
     "刚被标 offline"与"离线很久"在 last_seen_at 上可区分。
   - 引擎重新上线（worker 恢复心跳）→ zombie recovery 自然不再判死，等待自报/正常收口。
3. **新增分支：agent_tasks=pending_self_report**
   - 引擎在线（worker 活着）→ 等待自报（跳过，不判死不判超时——engine 在线说明 worker 能上报）
   - 引擎离线：与 running/dispatched 同样走离线持续时长校验；离线超阈值 **且**
     pending 时长 > `PENDING_SELF_REPORT_TIMEOUT_SECONDS`（默认 1800s=30min）→ 判
     timeout_failed（agent_tasks.errors 写明"主控重启后等待 worker 自报超时"；autopilot
     同步 failed，error 带说明）。
   - 引擎在线但 pending 超时 → 也判 timeout_failed（worker 活着但 30 分钟不自报 = 上报链路坏）。
     超时基准 = status 进入 pending_self_report 时刻。**时间戳落点**：复用 completed_at 列存
     "进入 pending 的时刻"（非终态不写 completed_at 的注释对 pending 需要破例——改用
     started_at？不行，started_at 有多重消费方。**决定：新增列 `pending_since`，TEXT，NULL**，
     db_setup _ensure_column 幂等迁移，语义清晰无复用歧义）。

**_poll_dispatched_completion 增加 pending_self_report 处理：**
- 查到 agent_tasks.status=pending_self_report → 继续 wait（视同 running），不同步 failed。
- 超时兜底（3600s）文案带说明：任务在等待 worker 自报。

### 3.4 pending 超时回收的落点

超时判定统一放 scheduler_loop 的每轮扫描（60s 周期）里，紧跟 zombie recovery：

```
SELECT ... FROM agent_tasks WHERE status='pending_self_report'
  → pending_since 距今 > PENDING_SELF_REPORT_TIMEOUT_SECONDS
  → update_task_status(task, "timeout_failed", errors="主控重启后等待 worker 自报超时(30min)...")
  → 事件侧（若有 event_seq）update_event_complete(seq, "failed", result=超时说明)
  → 关联 autopilot_executions（event_seq 匹配）同步 failed（带说明）
  → 释放 busy 槽（update_task_status 终态路径自动做）
```

参数均 env 可配：`PENDING_SELF_REPORT_TIMEOUT_SECONDS`（默认 1800）、
`ZOMBIE_OFFLINE_GRACE_SECONDS`（默认 300）。

### 3.5 db_setup.py

- `_AGENT_TASKS_DDL` 的 status 注释扩为 `queued/dispatched/running/pending_self_report/completed/failed/timeout_failed/cancelled`（列本身 TEXT，无需 DDL 变更）。
- 新增列：`_ensure_column(c, "agent_tasks", "pending_since", "TEXT DEFAULT NULL")`
  （在 _migrate_tables 的 agent-cluster 列批区域追加，幂等）。
- `_TASK_COLUMNS_LIST`（service.py）补 `pending_since`，insert 契约测试同步扩列。

### 3.6 autopilot 收口 evidence 校验（P1-1，纵深防御）

新增 `src/runtime/deploy_evidence.py`：

```python
def verify_deployment_evidence(task: dict, result_text: str) -> dict:
    """部署类任务收口前的实际落地证据校验钩子。

    返回 {"checked": bool, "passed": bool, "skipped_reason": str, "details": [...]}:
    - 非部署类（skill/prompt 无部署特征）→ checked=False（按原逻辑收口）
    - 版本指纹：任务 prompt/事件上下文中的目标版本号（如 202610301316）与
      agent_runtimes.worker_version（心跳上报）比对——只校验"出现过的引擎"
      与目标版本一致的占比；查不到目标版本号 → skip（不阻断）
    - 健康检查：本地 GET {server_base_url}/api/health（2s 超时，失败降级 skip）
    - 任一证据缺失/失败 → checked=True, passed=False, details 带原因
    """
```

- **判定为"部署类"**：task.skill / task.prompt 含部署特征词（"部署"、"升级"、"打包"、
  "生产部署"、"deploy"、"upgrade"）——保守词表，宁缺勿滥。
- **接入点（收口前）**：scheduler 的 `_poll_dispatched_completion` 与 zombie recovery 在
  同步 success 之前调用：`checked and not passed` → autopilot 终态改 failed（error 带
  evidence 详情）；`checked and passed` → success（result 附 evidence 摘要）；
  `not checked` → 原逻辑。校验自身异常 → skip + warning（不阻断收口）。
- 版本指纹来源：目标版本从 result_text 中解析（升级 skill 的收口输出含目标版本号，
  格式 `\d{8,14}` 时间戳状版本）；实际版本 = agent_runtimes 表中执行该任务的引擎
  （runtime_id）的 worker_version 列。**语义**：master 重启后引擎行 worker_version 已
  被心跳刷新为新版本 → 部署已落地；仍是旧版本 → false-success 防护命中。
- 健康检查来源：`/api/health`（已有免鉴权端点，返回 server/agent_runtime 状态）。
  用 urllib（标准库）+ 2s 超时；server_base_url 取 `src.user.db.get_server_base_url()`，
  空 → skip。

### 3.7 前端状态渲染（最小改动）

- `src/api/static/js/pages/observability.js` 两处 statusIcon/statusColor 映射补
  `pending_self_report: '⏳'/'var(--y)'`（待自报）与 `timeout_failed: '❌'/'var(--r)'`。
- `autopilot.js` 状态映射已有 success/failed 等值，无需新值（autopilot 侧仍是
  success/failed/cancelled 语义，evidence/超时差异进 error/result 文案）。

## 4. #308/#320 边界（不受影响）

手动终止口径在 `src/api/routes/events.py` terminate_event（worker→cancelled / direct→failed），
与本次改动路径（executor 停机、scheduler 收口、service 状态机）零交集。唯一共享点是
agent_tasks.status 字段——本次只增状态值、不改现有值语义，events.py 的分支
（`pending/running/dispatched` 可终止）不命中新值，行为不变。回归测试看护。

## 5. 测试计划（tests/test_issue_537_*）

**停机置 pending（executor）**
1. REMOTE 任务停机 → status=pending_self_report、pending_since 写入、busy 槽不释放、事件保持 running、exit_code 不动。
2. DIRECT 任务停机 → 照旧 failed（回归，现有 test_shutdown_force_fail 看护，扩展断言）。
3. 停机时无 REMOTE 任务 → 无 pending 写入（回归）。

**worker 自报收口（service）**
4. pending_self_report + worker complete_task(exit=0) → completed、completed_at 写入、busy 槽释放。
5. pending_self_report + worker fail_task → failed（自报失败照常生效）。
6. timeout_failed 状态 + worker 迟到自报（带结构化 output）→ 既有覆盖例外通道仍生效（不覆盖为 completed，除非结构化产出）。
7. validate_run_token：pending → "ok"；timeout_failed → "noop"。

**pending 超时回收（scheduler）**
8. pending 且 pending_since 超过 30min → timeout_failed + errors 带说明 + 事件 failed + autopilot failed + busy 释放。
9. pending 未超时 → 不动（等下一轮）。
10. 引擎离线 < 5min → 不判死（zombie recovery 防护）。
11. 引擎离线 > 5min（last_seen_at 距今 > 300s）→ 判死（既有行为保留，阈值化）。

**zombie recovery 防护（scheduler）**
12. agent_tasks=running + 引擎刚离线（last_seen_at=now-50s）→ exec 不被同步 failed。
13. agent_tasks=running + 引擎离线 > 5min → exec 同步 failed（既有兜底）。
14. agent_tasks=pending_self_report + 引擎在线 + 未超时 → exec 不动。

**_poll_dispatched_completion**
15. agent_tasks=pending_self_report → 不写 failed，继续轮询（下一轮 completed → success）。

**evidence 校验（deploy_evidence）**
16. 非部署类 → checked=False。
17. 部署类 + result_text 含目标版本 + 引擎 worker_version=目标版本 + health ok → passed。
18. 部署类 + 引擎 worker_version≠目标版本 → failed（false-success 防护）。
19. 健康检查不可达 → skip（不阻断）。
20. 收口接入：poll/zombie-recovery 同步 success 前调用钩子（mock 验证调用与状态分流）。

**#308/#320 回归**
21. events terminate_event 对 pending 状态事件的 400 行为不变（源码断言 evt_status not in ('pending','running','dispatched')）。

## 6. 验收标准（对齐 Issue）

- [x] 测试环境（pytest + SQLite 测试库）复现停双主流程：REMOTE 任务停机 → pending_self_report → worker 自报 → completed，无 failed 误报
- [x] worker 自报完成 → agent_tasks=completed，autopilot_executions 同步 success
- [x] pending 30min 无自报 → timeout_failed（agent_tasks）/ failed+说明（autopilot）
- [x] zombie recovery 引擎短暂离线（<5min）不误判 failed
- [x] autopilot 收口对部署类任务校验版本指纹 + 健康检查
- [x] #308/#320 路径回归通过（terminate_event 语义不变）
- [x] 全部现有测试 + 新增测试通过
