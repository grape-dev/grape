# 需求文档：Issue #4367 env_extra kid=dev 信封链路切断（B/C 代码增量）

> 来源：Issue #4367 Spec（评论 #203711411，评级 ready）。本环节只做 B/C 两项代码增量；
> A（生产标识纠正）与 D（008 迁移生产执行）为运维/数据执行项，不在本 PR 范围。

## 目标

切断「生产写入 `kid=dev` 信封 → 执行侧 `UnknownKeyError` → 空 env 静默降级」链路：

- **B 写入侧 fail-closed**：即使 `is_production()` 被误判为 False（security.json 缺失/env 误配），
  目标库 DSN/env 命中 prod、或进程在 prod 主机时，也禁止签发 dev 信封与明文降级；
  `encrypt_env_extra` 在该状态下抛错，入队失败可见，不落注定解不开的行。
- **C 空降级可观测**：`decrypt_env_extra` 解密失败分支从「一条 WARNING + 返回 {}」
  升级为「WARNING + `agent_tasks.errors` 结构化诊断槽（code=env_extra_unknown_kid, kid）
  + insight 计数器」，使「env 被清空」可告警。

## 范围

### In Scope（代码）
- `src/kernel/security_config.py`：新增「允许签发 dev 信封」显式开关（默认关）与
  生产判据收敛（is_production OR 目标库 DSN/env 命中 prod OR 进程在 prod 主机）；
  `get_credential_keys()` 的 dev 常量回落拒绝条件从 `is_production()` 扩展为禁令。
- `src/domains/mgmt/auth/credential_crypto.py`：`_load_keys` 生产拒 dev 条件扩展；
  `encrypt()` 拒绝 dev 信封签发与明文降级（禁令命中时）。
- `src/domains/runtime/engine/env_extra_security.py`：`decrypt_env_extra` except 分支
  写诊断槽 + 递增计数器（保留 WARNING）。
- `src/domains/insight/observability`：新增 `grape_env_extra_decrypt_failures_total{code,kid}`
  计数器（与 `record_env_extra_plaintext_row` 同级口径）。
- `deploy/alerts.yml`：新增解密失败告警规则（支撑「可告警指标」验收）。

### Out of Scope
- A 项配置纠正、D 项 008 迁移生产执行（复用 `scripts/migrations/008_rewrap_dev_env_extra.py`，不另写脚本）。
- #3770 明文存量迁移（保持独立）；#4365 发布节奏；#1943（编号笔误，与本单无关）。

## 验收标准（代码部分）
- 目标库命中 prod 时签发 dev 信封/明文降级被拒绝，有回归用例（fail-closed 可验证）。
- 解密失败写 `agent_tasks.errors` 诊断槽且计数器递增，有单测断言；不再是仅一条 WARNING。
- 开发/生产双向用例通过；现有测试全绿。
