# 任务拆解：Issue #4367 B/C 代码增量

## T1 B-判据收敛（security_config.py）
- [x] 新增 `ALLOW_DEV_ENVELOPE_ENV` / `allow_dev_envelope()`（默认关）
- [x] 新增 `dev_envelope_forbidden_reasons()`（is_production + DSN/env + prod 主机三判据）
- [x] 新增 `forbid_dev_envelope_reason()`（禁令单一入口，显式放行短路）
- [x] `get_credential_keys()` dev 常量回落拒绝：`is_production()` → `forbid_dev_envelope_reason()`

## T2 B-写入侧执行（credential_crypto.py）
- [x] `_load_keys()` 生产拒 dev：条件扩为禁令命中（含 `GRAPE_ALLOW_DEV_ENVELOPE` 放行）
- [x] `encrypt()` 明文降级：禁令命中一律拒绝
- [x] `encrypt()` 签发 kid=dev：禁令命中拒绝（堵住「dev 排首的混配环」漏网）

## T3 C-读侧可观测（env_extra_security.py + insight/observability）
- [x] observability：`grape_env_extra_decrypt_failures_total{code,kid}` + `record_env_extra_decrypt_failure()`
- [x] decrypt_env_extra except：保留 WARNING + 计数 + `agent_tasks.errors` 诊断槽
      （code=env_extra_unknown_kid 携 kid；同 code 幂等覆盖、保留 5 条、diagnostic=True）
- [x] 嵌套 db_lock 跳过诊断槽写（计数/告警不受影响）；best-effort 不拖垮读链路

## T4 告警与验收支撑
- [x] deploy/alerts.yml：`GrapeEnvExtraDecryptFailing`（1h 窗口，对应 kid=dev WARN=0 验收口径）

## T5 测试（TDD：先红后绿）
- [x] tests/kernel/test_issue_4367_dev_envelope_gate.py（B：判据/开关/三执行点/双向/端到端）
- [x] tests/runtime/test_issue_4367_env_extra_decrypt_diag.py（C：诊断槽/计数/幂等/嵌套降级/registry/alerts）

## T6 回归
- [x] tests/test_issue2423_env_extra_crypto.py、tests/runtime/test_issue_3770_env_extra_plaintext.py
- [x] tests/kernel/test_credential_key_version_scenarios.py
- [x] AGENTS.md 门禁：tests/runtime/test_repogit.py、test_issue_3482_workspace_order.py、
      test_issue_2245_git_writable.py + make lint + scripts/check-arch.py

## 不做（边界）
- A 项生产配置纠正、D 项 008 迁移执行（运维/数据项，复用既有脚本，不另写）
- #3770 明文迁移实现、#4365 发布节奏、#1943（编号笔误）
