# Task — Issue #3770

- [x] T1 `env_extra_security.py`：`decrypt_env_extra` 增 `task_id`/`row_kind`/`plaintext_policy`；
      就地自愈 `heal_env_extra_plaintext_row`（条件 UPDATE 幂等）；(host, 行类型) 每小时 ≤1 条节流；
      计数转发 + 文案改 `202611220003` + 只输出键名/行 id
- [x] T2 `insight/observability`：新增 `grape_env_extra_plaintext_rows_total{outcome}` + `record_env_extra_plaintext_row`
- [x] T3 `service.py·sync_task_session_token`：回写改 `_encrypt_env_extra_for_write`，加密失败 → 任务失败（回滚 + 返回空串 + 计数 write_refused），删明文回写注释
- [x] T4 `service.py·_fn_migrate_env_extra_encryption`：口径改「非空且非 `enc:v1:`」；keyset 分页；失败行保留原值（不置 NULL）+ 计数 + 告警
- [x] T5 `service.py·get_task`：读 env 传 `task_id` 走自愈
- [x] T6 `db_setup.py`：核对 `202611220003` 注册（本轮不改代码，用例锁定）
- [x] T7 用例：`tests/test_issue_3770_env_extra_plaintext.py`（15 例：回写加密/加密失败不回落/自愈幂等/`{}` 空对象/节流/坏值/backfill 脏值与失败保留/指标注册）
- [x] T8 回归：`tests/test_issue2423_env_extra_crypto.py`(6 passed)、`tests/test_issue2423_server_env_credentials.py`、
      `tests/runtime/test_issue_990_*`、`tests/arch/test_domain_boundaries.py`、`tests/mgmt/test_account_contract_routing.py`、
      `tests/test_issue_2085_contract_guard_alert.py`、`tests/insight/test_pr1077_observability_endpoints.py`、`scripts/check-arch.py`、`scripts/check-secrets.py`
      （2 例基线既有红：`test_snapshot_persist_failure_rolls_back`、`test_multiprocess_sqlite_rotation_keeps_single_token`，未改前同样失败）
- [x] T9 文档：`docs/issue-3770/{spec,plan,task}.md` + Issue 评论