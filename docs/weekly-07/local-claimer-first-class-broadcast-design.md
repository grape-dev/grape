# Local Claimer：本机 direct 引擎一等广播认领设计

- 日期：2026-10-14（事故）/ 2026-10-15（设计定稿并实现）
- 关联事故：pvs-zombies-demo2 issue #4 / #6（广播任务全链路错路由到本机 opencode）
- 关联 Issue：grape Issue #4；stale-queued 语义关联 Issue #253
- 实现：`src/runtime/local_claimer.py`、`src/runtime/service.py`（enqueue/stale 扫描）、
  `src/api/db_setup.py`（迁移）、`src/api/main.py`（lifespan 启动）
- 测试：`tests/test_local_claimer.py`（9 UT）

## 1. 背景与事故

plugin 广播模式下，任务入队时带 `pool_engine_ids` 白名单（用户在场景里显式绑定的引擎，
或系统自动选择的绑定）。此前的实现里存在一条"入队即 self-run"兜底路径
（`_pool_only_local_direct`）：当队列里出现广播任务且本机有 direct 引擎时，
本机引擎在 enqueue 的同一毫秒内直接把任务"抢走"本地执行——**完全绕过白名单**。

2026-10-14 pvs-zombies-demo2 环境复现：issue #4 的全链路各阶段都应路由到绑定的
aishell 引擎（15249），实际全部被本机 opencode 引擎 self-run 兜底截胡；绑定的远端
引擎从未获得认领机会（issue #6 同因）。

根因：本机 direct 引擎（credential_id=0）在广播语义里**不是认领者**，却拥有一条
绕过认领检查链的执行后门——这是权限模型漏洞，不是调度抖动。

## 2. 目标 / 非目标

**目标**

1. 本机 direct 引擎成为与远端 worker **完全同级**的广播认领者：同一条
   `claim_by_tag` 检查链（tag 匹配、scope 隔离、满槽、`pool_engine_ids` 白名单、
   健康分拦截、会话引擎守卫），不引入任何新认领规则。
2. 删除 self-run 后门（enqueue 即本地执行 + `_pool_only_local_direct`）。
3. 白名单全灭（绑定引擎全部下线）时按来源分流：自动绑定降级放行（一次性），
   用户显式绑定保持诚实失败（#253 语义）。
4. 提供总开关 `GRAPE_LOCAL_CLAIMER`（默认开启，可关闭回退纯远端认领）。

**非目标**

- 不改变远端 worker 的认领/执行路径（复用 `claim_by_tag`，零改动语义）。
- 不引入新的调度优先级或负载均衡策略。
- 不处理 legacy（非 plugin）广播模式。

## 3. 设计

### 3.1 本机 direct 引擎一等认领循环（local_claimer.py）

- **引擎发现**：`credential_id=0`（本机直连）且 `scope='global'` 且 `status='online'`，
  排序 opencode → codearts → 其他（与本方案前的发现查询同源）。
  unhealthy 引擎照常返回——健康分拦截在 `claim_by_tag` 内完成，与远端同约束。
- **认领**：对每个发现的引擎，先本地满载检查（`busy_slots >= max_concurrent` 跳过），
  再按 `engine_capabilities` 的 tag 逐个调 `service.claim_by_tag(tag, runtime_id)`。
  领到即走本机执行；一轮只领一个任务，下轮继续（与 worker 单任务节奏一致）。
- **节奏**：3s 轮询 + jitter（与 worker 的 `RUNTIME_POLL_INTERVAL` 默认值一致）。
- **执行**：认领后复用已验证的 direct 执行通道
  （`start_direct_agent` + `_execute_agent`，线程池），状态流转与 worker 路径一致
  queued → running → completed，任务行不另起。

### 3.2 enqueue_task：pool_engine_source 来源标记

`agent_tasks` 新增 `pool_engine_source` 列（`scene` / `auto` / `degraded`）：

- `scene`：用户在场景配置里**显式绑定**的引擎白名单；
- `auto`：系统按 tag/健康度**自动选择**的绑定；
- `degraded`：曾走 §3.3 降级放行（标记用于防重复降级）。

来源标记是 §3.3 分流的判别依据；迁移见 §4。

### 3.3 stale-queued：白名单全灭按来源分流

引擎不健康但物理可达时任务先有界等待；超时后（`_check_stale_queued_tasks`）：

- `pool_engine_source='auto'`（系统自动选择的绑定）→ **降级**：清空
  `pool_engine_ids`、标记 `pool_engine_source='degraded'`、重新入队，
  释放回全局池（**只做一次**，UPDATE 带 `AND status='queued'` 条件，
  `degraded` 标记防重复；降级动作审计进 result_text）。
- `pool_engine_source='scene'`（用户显式绑定）→ 保持 Issue #253 的**诚实失败**：
  标记 failed，不静默换引擎——用户绑定即意图，换引擎执行比失败更危险。

### 3.4 evt 忠实重建

任务行只存摘要字段；本机认领执行前按 `event_seq` 从 `events` 表还原完整 evt
（payload/type/actor/source/title），与 enqueue 时同一数据源，
`build_devops_agent_prompt` 重建结果与远端 worker 路径逐字段一致。
autopilot 等不写 events 表的任务（查不到 event_seq）→ 最小 evt，
`_execute_agent_inner` 按任务行内已有 prompt 走（writeback 只填空）。

### 3.5 开关 GRAPE_LOCAL_CLAIMER

- 默认开启；`GRAPE_LOCAL_CLAIMER=0` 关闭（本机引擎不参与广播认领，
  回退纯远端认领，lifespan 不启动循环）。
- 运行态热切换：认领循环**每轮实时读取**环境变量——关闭即时停抢、
  重新开启即时恢复，无需 reload/restart（状态变化只打一次日志防刷屏）。

### 3.6 双主安全

双主部署下两台 master 都可能跑认领循环：`claim_by_tag` 的认领是
`UPDATE agent_tasks SET status='running', runtime_id=? WHERE ... AND status='queued'`
的原子操作——谁先 UPDATE 成功谁执行，另一方 rowcount=0 自然放弃，
同一 task 不会双跑（与远端 worker 争抢同一原子语义）。

## 4. 数据库迁移（幂等）

`db_setup.py` 启动时 `_ensure_column` 补列（已存在则跳过）：

- `agent_tasks.pool_engine_ids TEXT DEFAULT ''`（前序已有）
- `agent_tasks.pool_engine_source TEXT DEFAULT ''`（本设计新增）

SQLite/MySQL 同一 DDL 模板，无数据回填需求（存量行 source 为空 →
stale 扫描按非 scene 处理，行为与旧自动绑定一致）。

## 5. 单元测试（tests/test_local_claimer.py）

内存 sqlite 隔离，monkeypatch `health_scorer.is_healthy` 控制健康状态：

| 编号 | 场景 | 断言 |
|---|---|---|
| UT-01 | 白名单隔离（事故回归） | 白名单 [15249] 时本机 246 claim 返回 None，15249 可领 |
| UT-02 | 本机在白名单 | 正常认领，queued→running、runtime_id、busy_slots 正确 |
| UT-03 | 满载不抢 | `busy_slots >= max_concurrent` → `_engine_busy` True |
| UT-04 | 低分不抢 | health_scorer 判不健康 → claim 返回 None（与远端同约束） |
| UT-05 | 降级按来源分流 | auto 全灭 → 清白名单重入队（degraded）；scene → failed |
| UT-05b | 降级只做一次 | degraded 任务不再被重复降级/失败 |
| UT-06 | 双主竞争 | 同一 task 只能被认领一次（UPDATE 原子性），第三者领不到 |
| UT-07 | evt 重建 | events 表按 seq 还原 payload/type/actor/repository |
| 开关 | kill-switch | `GRAPE_LOCAL_CLAIMER=0` 时 `start_local_claimer` 不启动 |

## 6. 部署与验证

1. 双主部署（0007+0001）：健康检查通过，启动日志见
   "Local claimer loop started (3s interval)"。
2. 端到端（issue #7）：绑定引擎 15249 经远端 worker poll 认领并执行（ACP session）；
   本机引擎被白名单正确拦截——错路由不再发生。
3. 回归：`pytest tests/test_local_claimer.py`（9 passed）、
   `pytest tests/test_worker_package.py`（16 passed，weekly-07 基线）。

## 7. 风险与回退

- **风险**：本机引擎满载/不健康时广播任务等待远端认领，时延从"毫秒级 self-run"
  变为"3s 轮询级"——这是正确性换来的诚实代价，与远端认领节奏一致。
- **回退**：`GRAPE_LOCAL_CLAIMER=0` 关闭本机认领（回退纯远端）；
  self-run 后门已删除，如需恢复旧行为需回滚整个提交（不建议——旧行为即事故根因）。
