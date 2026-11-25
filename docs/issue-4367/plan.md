# 方案设计：Issue #4367 B/C 代码增量

## B 写入侧 fail-closed

### 判据收敛（src/kernel/security_config.py，单一事实源）

新增公开函数：

```
forbid_dev_envelope_reason() -> str   # 禁令原因串；空串 = 允许签发 dev 信封
dev_envelope_forbidden_reasons() -> list[str]  # 判据证据清单（可观测）
allow_dev_envelope() -> bool          # 显式开关 GRAPE_ALLOW_DEV_ENVELOPE（默认关）
```

判据叠加（任一命中即禁，除非显式放行）：

1. `is_production()`（security.json env=production，既有判据）；
2. 目标库 DSN/env 命中 prod：`GRAPE_DSN_{RUNTIME,MGMT,INSIGHT}`、`DEVOPS_DB_NAME`、
   `GRAPE_RUNTIME_DB_NAME`、`DEVOPS_DB_HOST`、`DEVOPS_DB_PATH` 中出现 prod 标记
   （`(?<![a-z0-9])prod(?:uction)?(?![a-z0-9])`，大小写不敏感——`grape_prod_runtime` 命中、
   `productbox`/`grape_dev_*` 不误伤）；
3. 进程在 prod 主机：显式 `GRAPE_HOST_ENV=prod|production`，或引擎主机标识
   （`engine_host()`：GRAPE_HOSTNAME > 系统 hostname）带同款 prod 标记。

开关语义（Spec 关键设计决策）：**默认关，仅在 prod 判据命中时生效**——

- prod 判据未命中（开发/测试）：行为不变，dev 信封照常可用（零回归）；
- prod 判据命中 + 开关关（默认）：禁止 dev 信封与明文降级 → 抛错；
- prod 判据命中 + `GRAPE_ALLOW_DEV_ENVELOPE=1`（显式应急放行）：允许（留痕于环境变量，不再静默）。

### 执行点（credential_crypto.py）

| 执行点 | 旧行为 | 新行为 |
|--------|--------|--------|
| `security_config.get_credential_keys()` dev 常量回落 | 仅 `is_production()` 拒绝 | `forbid_dev_envelope_reason()` 拒绝（含 is_production） |
| `credential_crypto._load_keys()` 密钥环只剩 dev | 仅 `is_production()` 抛错 | 禁令命中抛错（含显式配置 dev kid 的错误姿势） |
| `credential_crypto.encrypt()` 明文降级（无 cryptography） | 仅密钥串非 dev 前缀时抛错 | 禁令命中一律拒绝明文降级（今日 9 行明文的成因路径） |
| `credential_crypto.encrypt()` 签发 kid=dev | 无检查（cred1,dev 混配且 dev 排首时仍发 dev 信封） | 禁令命中拒绝签发 |

`env_extra_security.encrypt_env_extra` 不加独立守卫：真实密钥（cred1/derived）在 prod 进程
必须照常可用（否则误伤生产入队）；禁令只在「即将落 dev 信封/明文」的瞬间生效，异常经
`encrypt_env_extra` 现有包装上抛 → `enqueue_task` INSERT 中断 → 入队失败可见
（`service.py` 写入路径不吞该异常）。

### 数据流

```
enqueue_task / sync_task_session_token / backfill
  └─ env_extra_security.encrypt_env_extra
       └─ credential_crypto.encrypt
            ├─ forbid_dev_envelope_reason() 命中 + 即将明文降级 → RuntimeError（#4367 B）
            ├─ _get_current_key() → _load_keys：环内仅 dev + 禁令命中 → RuntimeError
            └─ kid=dev + 禁令命中 → RuntimeError
       ↳ 上抛 → enqueue_task INSERT 不执行 → 任务入队失败（可见）
```

## C 空 env 降级可观测

`decrypt_env_extra` except 分支（env_extra_security.py）：

```
except Exception as e:
    kid = e.kid（UnknownKeyError）/ 空
    code = env_extra_unknown_kid（信封 kid 不在本进程密钥环）/ env_extra_decrypt_failed（其余）
    1. 保留原 WARNING（日志既有检索词「credential key unknown for kid=」不变）
    2. 递增 grape_env_extra_decrypt_failures_total{code,kid}（insight /metrics 出口）
    3. agent_tasks.errors 结构化诊断槽（同 error_code 幂等覆盖、保留最近 5 条、
       diagnostic=True——与 service._persist_task_diagnostic 同口径）：
       {"error_code": "env_extra_unknown_kid", "kid": "dev", "reason": ..., "diagnostic": true, "ts": ...}
```

工程约束：

- **诊断槽写通道**复用 `runtime_agent_tasks_write()`（单库只持 session_manager.db_lock、
  三库只持 owner_lock("runtime")，不嵌套——QA #3770 Critical 锁序口径）；
- **嵌套持锁降级**：`_nested_under_process_db_lock()` 命中（claim 的 db_lock 段内读任务）
  时跳过诊断槽写（避免 commit 提前提交外层事务），计数器与 WARNING 照常；
- **best-effort**：诊断槽写失败只降级 debug 日志，观测不得拖垮读链路；无 task_id
  （worker 多数读点）只计数 + 告警；
- 模块自包含：env_extra_security 随 worker 包分发，不反向依赖 service（本地实现诊断槽写）。

### 计数器（insight/observability，与 record_env_extra_plaintext_row 同级）

```
grape_env_extra_decrypt_failures_total{code,kid}
  code ∈ {env_extra_unknown_kid, env_extra_decrypt_failed, unknown}
  kid   低基数（dev/cred1/derived-<8hex>），超长截断 40，缺省 unknown
```

### 告警（deploy/alerts.yml）

```
GrapeEnvExtraDecryptFailing: sum by (kid) (increase(grape_env_extra_decrypt_failures_total[1h])) > 0
```

1 小时窗口直接对应验收口径「1 小时 kid=dev WARN = 0」。

## 测试策略

- `tests/kernel/test_issue_4367_dev_envelope_gate.py`（B）：
  - 判据收集三分支 + `productbox` 反例 + 开关显式放行；
  - `get_credential_keys` prod-DSN 拒 dev 回落 / 开发环境照常回落（双向）；
  - `encrypt` 拒 dev kid / 拒明文降级 / 显式放行后可用 / 真实 kid 在禁令下照常（不误伤）；
  - `encrypt_env_extra` 端到端抛错。
- `tests/runtime/test_issue_4367_env_extra_decrypt_diag.py`（C）：
  - UnknownKeyError → 诊断槽（code+kid）+ 计数器 + WARNING 保留；
  - 同 code 幂等覆盖、嵌套 db_lock 跳过 DB 写只计数、registry 端到端、alerts.yml 规则在场。

## 风险与对策

| 风险 | 对策 |
|------|------|
| fail-closed 误伤开发环境 | 开关默认关但仅在 prod 判据命中时生效；prod 判据用带边界的 prod 标记（`productbox` 不误伤）；双向用例 |
| 禁令命中但密钥环正常（cred1）被误拦 | 禁令只拦 dev kid/明文降级瞬间，真实 kid 照常；专项回归用例 |
| 诊断槽写与 claim 事务嵌套 | 复用 #3770 锁序结论：嵌套持锁跳过 DB 写，只计数 + WARNING |
| kid 高基数 | 截断 40、缺省归 unknown，与现有 reason 归并口径一致 |
