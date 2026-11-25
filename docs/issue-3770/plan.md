# Plan — Issue #3770

## 方案（写收口断增量 + 迁移/读侧自愈清存量 + 指标节流恢复可观测）

### 1. `env_extra_security.py`：读侧自愈 + 节流 + 计数

- `decrypt_env_extra(raw, *, task_id="", row_kind="agent_tasks", plaintext_policy="heal")`
  —— 保持既有「信封解密 / 明文降级解析」语义，只新增关键字参数，所有旧调用点零改动可继续工作。
- 新增 `heal_env_extra_plaintext_row(row_id, sealed, *, row_kind="agent_tasks")`：
  `UPDATE agent_tasks SET env_extra=? WHERE id=? AND env_extra IS NOT NULL AND env_extra<>'' AND env_extra NOT LIKE 'enc:v1:%'`，
  用 `kernel.owner_db.db_pair("runtime")`（owner 域连接 + 域锁），返回是否真正改写（`rowcount>0`）→ 幂等、并发安全。
- 新增节流：模块级 `{(host, row_kind): last_ts}`，`PLAINTEXT_ALERT_INTERVAL_SEC = 3600`；
  `reset_plaintext_alert_throttle()` 作为测试 seam。
- 新增计数转发 `_record_plaintext(outcome)` → `insight.observability.record_env_extra_plaintext_row`（函数内 import，
  缺失/异常静默，不拖垮读链路；worker 包无 prometheus 时为 noop 桩）。
- 告警文案：`env_extra: 存量明文落库行（C3）host=… kind=… row=… outcome=… keys=…` + 指向 `202611220003`，
  只输出键名/行 id。
- 新增 `record_env_extra_write_refused()`：写侧拒绝明文回落时计数 `write_refused`。

### 2. `service.py`：写入收口 + backfill 口径

- `sync_task_session_token`：
  - 读 env 用 `decrypt_env_extra(_raw_env, task_id=task_id, plaintext_policy="report")`
    （该函数在同一段逻辑里马上会以加密态回写，不自愈避免 open 事务内嵌套写）；
  - 回写从 `json.dumps` 改为 `_encrypt_env_extra_for_write(env)`；加密抛错 → `log.error` +
    `record_env_extra_write_refused()` + 回滚 + 返回空串（任务失败），删掉「沿用明文回写」注释。
- `_fn_migrate_env_extra_encryption`：
  - 选行条件改「非空且非 `enc:v1:`」；keyset 分页（`id > last_id ORDER BY id LIMIT 1000`）防失败行重复选中/死循环；
  - 失败行保留原值（不再 `SET env_extra=NULL`），计数 + 告警（行 id 最多 20 条）；成功/失败都进 counter
    （`healed` / `heal_failed`）；改用 `owner_db.db_pair("runtime")` 取 owner 连接。

### 3. `insight/observability`：新增 counter

```python
ENV_EXTRA_PLAINTEXT_ROWS = Counter(
    "grape_env_extra_plaintext_rows_total", "...", ["outcome"], registry=registry)
ENV_EXTRA_PLAINTEXT_OUTCOMES = ("healed", "heal_failed", "unparseable", "write_refused")
def record_env_extra_plaintext_row(outcome="heal_failed"): ...   # 未知 outcome → unknown（不丢数）
```

### 4. `db_setup.py`

只核对：`202611220003` / `issue2423-env-extra-encrypt-backfill` 注册与实现在位（用例锁定），本轮不改。

## 测试 seam

| 切片 | seam（用户可见行为） | 用例 |
|------|---------------------|------|
| 写入收口 | `sync_task_session_token(task_id)` 返回值 + `agent_tasks.env_extra` 列原文 | 轮换后列为 `enc:v1:` 且不含 `tk_`；加密失败 → 返回 `""` 且列原文不变（无明文） |
| 读侧自愈 | `service.get_task(task_id)` + 列原文 + counter | 明文行读出 → 列变信封；再读一次 counter 不增（幂等） |
| 节流/告警 | `env_extra_security.log.warning` 调用次数 + 文案 | 同一 (host, 行类型) 两次命中只 1 条；文案含 `202611220003` 且不含值 |
| backfill | `db_setup._fn_migrate_env_extra_encryption_2423()` + 列原文 | 脏值（前导空格）入选并加密；空/NULL/已加密行不动；失败行保留原值不 NULL 且不死循环 |
| 指标注册 | `/metrics` registry 采样 | `grape_env_extra_plaintext_rows_total{outcome=...}` 可取到并随 inc 增长 |

## 提交拆分（TDD 红/绿）

1. `docs(#3770)`：spec/plan/task
2. `test(#3770)`：新增用例（对当前实现为红）
3. `feat(#3770)`：实现（转绿）