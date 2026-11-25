# Spec — Issue #3770（env_extra 存量明文凭据行爆量 / 明文落库收口）

> 来源：Issue #3770 Spec（评论 202841295，Issue Agent 发布）。本文件是 coding 侧对同一需求的落地版本，
> 只补充实现所需的代码级事实，不改变验收口径。

## Objective

停止 `agent_tasks.env_extra` 的凭据明文落库与告警刷屏，三件事同批交付：

1. **存量清零**：数据面执行迁移 `202611220003`（不是文案里的 `202611130001`），读侧就地自愈兜住迁移后
   新产生的明文行；
2. **增量收口**：写入侧（见「写入侧清单」）一律走 `encrypt_env_extra()`，加密失败即任务失败（或回退
   空串并计数 `write_refused`），禁止明文回落；
3. **可观测恢复**：读侧告警按 (host, 行类型) 每小时 ≤1 条，新增 counter
   `grape_env_extra_plaintext_rows_total`，告警只输出键名/行 id。

## 写入侧清单（QA #3770 审查 R2 收全——agent_tasks.env_extra 的全部写入点）

| 写入点 | 位置 | 落库形态 |
|--------|------|----------|
| `enqueue_task` | `service.py`（_task_insert_row 路径） | enc:v1 信封（#2423 已收口） |
| `sync_task_session_token` 回写 | `service.py` | enc:v1 信封；加密失败 fail-closed（任务失败 + revoke 新 token + 计数） |
| `_create_direct_task`（direct 快照） | `event_pipeline.py` | `_encrypt_env_extra_for_write({})`；加密失败回退空串 + 计数 `write_refused` |
| `_create_failed_task`（失败快照） | `event_pipeline.py` | `_encrypt_env_extra_for_write(env_extra)`；加密失败回退空串 + 计数 `write_refused`（failed 行不经 get_task 读，读侧自愈覆盖不到，必须写入即信封） |

写入侧统一约束：`agent_tasks.env_extra` 不再接受任何「非空且非 enc:v1:」的新值——
验收 SQL 的增量侧恒为 0。

## 现状（代码级核对，分支 `arch-weekly-11-ljt` @ 7103a2e）

| 位置 | 现状 | 问题 |
|------|------|------|
| `env_extra_security.py:102-107` | 值非 `enc:v1:` 即 `log.warning`，然后按明文 JSON 解析 | 无节流、无 counter、无自愈；任务每执行一次重复命中同一批行（4,020 条 WARNING/小时） |
| 告警文案 | 指向 `202611130001` | 该版本号实为 `repair-autopilot-exec-agent-task-id`（`db_setup.py:3424`），与 env_extra 无关 |
| `service.py:5887-5893` | `sync_task_session_token` 回写 `json.dumps(env, ensure_ascii=False)` 明文 | 每次会话 token 轮换新增一条明文行——迁移后重启仍复现、随任务量放大的直接机制 |
| `service.py:957-979` | backfill 只选 `env_extra LIKE '{%'`；单行失败 `SET env_extra=NULL` | 前导空格等脏值漏选（与验收 SQL 口径不一致）；失败行静默丢凭据 |
| `db_setup.py:8700-8705` | `202611220003` / `issue2423-env-extra-encrypt-backfill` 已注册 | 无需改动，只需核对 + 用例锁定 |

## Scope

**In**：写入收口、读侧自愈 + 节流 + counter、backfill 口径与失败策略、日志脱敏、用例与文档。
**Out**：#3763（mgmt 写 runtime 域）、#3737（日志明文 token）、加密算法/密钥管理、已导出备份文件的历史清洗。

## 行为契约（实现依据）

1. `decrypt_env_extra(raw, task_id=..., row_kind="agent_tasks", plaintext_policy="heal")`：
   - `enc:v1:` 信封 → 解密返回（行为不变）；
   - 明文 + `policy="heal"` + 有行 id → 就地自愈（加密后条件 UPDATE 回写：`AND env_extra NOT
     LIKE 'enc:v1:%'` + 原值 CAS `AND env_extra=?`，不覆盖已加密行、不用旧快照覆盖并发新值），
     真改行（rowcount>0）计数 `healed`（**不告警**）；rowcount=0（已被并发加密/删除/覆盖）计数
     `heal_skipped`（不告警）；自愈失败 → 计数 `heal_failed` + 节流告警；
   - 明文 + `policy="report"` / 无行 id → 不自愈（调用方同一操作里马上会加密回写，或读点无写通道），
     计数 `report_only` + 节流告警——**不与真失败混计**；
   - 坏值（非 JSON 对象）→ 返回 `{}`，计数 `unparseable` + 节流告警，行保留原值。
2. 节流：进程内 `(host, row_kind)` 每小时最多 1 条 WARNING，超限只计数；host 取
   `kernel.host_identity.engine_host()`（四台主控各自可观测）。
3. 自愈/迁移的写通道 = `runtime_agent_tasks_write()`（owner 域连接 + owner_lock；连接回落
   进程库——单库/同域拓扑，`not uses_own_dsn("runtime")`——时**同持 `session_manager.db_lock`**，
   与全仓 agent_tasks 写路径同一把锁，避免 heal/迁移的 commit 提交其它线程的未完成事务；
   三库拓扑继续 db_pair 语义）。
4. `sync_task_session_token`：加密失败 → `log.error` + 计数 `write_refused` + 回滚 + **撤销本次
   轮换签发的 token**（revoke_session_token，宽限期后失效）+ 返回空串（任务失败），
   **不得**写回明文、不得留下「有效但未落库」的 token。
5. backfill：选行口径 = `env_extra IS NOT NULL AND env_extra<>'' AND env_extra NOT LIKE 'enc:v1:%'`；
   用 keyset（`id > last_id ORDER BY id LIMIT 1000`）分页保证失败行不会被重复选中 / 死循环；
   失败行**保留原值**（不置 NULL）+ 计数 + 告警（含行 id，最多列出 20 条）。
6. 日志脱敏：明文行告警只输出 host / 行类型 / 行 id / 键名集合，绝不输出值。
7. takeover-context API（`/api/runtime/issues/takeover-context`）：env_extra 返回前经
   `decrypt_env_extra` 还原（类型保持 JSON 字符串，契约无感），不再把密文/明文凭据原样给到 API。

## Acceptance Criteria

- `SELECT COUNT(*) FROM agent_tasks WHERE env_extra<>'' AND env_extra NOT LIKE 'enc:v1:%'` = 0（数据面）
- 修复后连续 1 小时 `|= "存量明文落库行"` = 0（四台主控各自）
- 触发任务执行（含会话 token 轮换、direct/failed 快照）后复验仍为 0（写入侧不复发；
  用例 `test_direct_and_failed_task_rows_keep_acceptance_sql_zero` 为代码侧证据）
- `grape_env_extra_plaintext_rows_total` 命中增长、清零后不再增长；告警 ≤1 条/主机/小时
- 失败行不丢凭据（保留原值 + 计数 + 告警）
- 凭据下发链路回归通过（任务 env 注入正常）
- 现有测试 + 新增用例通过，且新增用例已接入 `linux-backend` CI 门禁（windows-smoke.yml）

## 风险

| 风险 | 缓解 |
|------|------|
| 自愈与写入侧并发 | 条件 UPDATE 幂等 + 原值 CAS（不覆盖并发新值）；失败退化为计数 + 节流告警 |
| 自愈在 open 事务内嵌套写 | 读侧自愈只在 `get_task` 等无事务段落执行（db_lock 之外）；`sync_task_session_token` 内部用 `report` 策略，由该次加密回写完成修复 |
| 自愈不可用（worker 无 runtime 库） | owner 连接解析失败回落，异常被捕获 → 计数 + 节流告警，不影响任务读 env |
| 锁错配（单库拓扑 heal/迁移与 agent_tasks 写路径不同锁） | 写通道统一 `runtime_agent_tasks_write()`：回落进程库时同持 `session_manager.db_lock`（有门禁用例锁定） |