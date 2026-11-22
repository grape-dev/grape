# Spec — Issue #3770（env_extra 存量明文凭据行爆量 / 明文落库收口）

> 来源：Issue #3770 Spec（评论 202841295，Issue Agent 发布）。本文件是 coding 侧对同一需求的落地版本，
> 只补充实现所需的代码级事实，不改变验收口径。

## Objective

停止 `agent_tasks.env_extra` 的凭据明文落库与告警刷屏，三件事同批交付：

1. **存量清零**：数据面执行迁移 `202611220003`（不是文案里的 `202611130001`），读侧就地自愈兜住迁移后
   新产生的明文行；
2. **增量收口**：写入侧（`sync_task_session_token` 回写）改走 `encrypt_env_extra()`，加密失败即任务失败，
   禁止明文回落；
3. **可观测恢复**：读侧告警按 (host, 行类型) 每小时 ≤1 条，新增 counter
   `grape_env_extra_plaintext_rows_total`，告警只输出键名/行 id。

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
   - 明文 + `policy="heal"` + 有行 id → 就地自愈（加密后条件 UPDATE 回写，幂等），成功返回明文内容并计数
     `healed`，**不告警**；自愈失败/无行 id → 计数 `heal_failed` + 节流告警；
   - 明文 + `policy="report"` → 不自愈（调用方同一操作里马上会加密回写），计数 + 节流告警；
   - 坏值（非 JSON 对象）→ 返回 `{}`，计数 `unparseable` + 节流告警，行保留原值。
2. 节流：进程内 `(host, row_kind)` 每小时最多 1 条 WARNING，超限只计数；host 取
   `kernel.host_identity.engine_host()`（四台主控各自可观测）。
3. 自愈 UPDATE 必须带 `AND env_extra NOT LIKE 'enc:v1:%'`，保证并发/重复调用幂等（不覆盖已加密行）。
4. `sync_task_session_token`：加密失败 → `log.error` + 计数 `write_refused` + 回滚 + 返回空串（任务失败），
   **不得**写回明文。
5. backfill：选行口径 = `env_extra IS NOT NULL AND env_extra<>'' AND env_extra NOT LIKE 'enc:v1:%'`；
   用 keyset（`id > last_id ORDER BY id LIMIT 1000`）分页保证失败行不会被重复选中 / 死循环；
   失败行**保留原值**（不置 NULL）+ 计数 + 告警（含行 id，最多列出 20 条）。
6. 日志脱敏：明文行告警只输出 host / 行类型 / 行 id / 键名集合，绝不输出值。

## Acceptance Criteria

- `SELECT COUNT(*) FROM agent_tasks WHERE env_extra<>'' AND env_extra NOT LIKE 'enc:v1:%'` = 0（数据面）
- 修复后连续 1 小时 `|= "存量明文落库行"` = 0（四台主控各自）
- 触发任务执行（含会话 token 轮换）后复验仍为 0（写入侧不复发）
- `grape_env_extra_plaintext_rows_total` 命中增长、清零后不再增长；告警 ≤1 条/主机/小时
- 失败行不丢凭据（保留原值 + 计数 + 告警）
- 凭据下发链路回归通过（任务 env 注入正常）
- 现有测试 + 新增用例通过

## 风险

| 风险 | 缓解 |
|------|------|
| 自愈与写入侧并发 | 条件 UPDATE 幂等；失败退化为计数 + 节流告警 |
| 自愈在 open 事务内嵌套写 | 读侧自愈只在 `get_task` 等无事务段落执行；`sync_task_session_token` 内部用 `report` 策略，由该次加密回写完成修复 |
| 自愈不可用（worker 无 runtime 库） | `owner_db("runtime")` 解析失败回落，异常被捕获 → 计数 + 节流告警，不影响任务读 env |