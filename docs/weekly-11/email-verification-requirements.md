# 邮箱验证码链路加固需求（Issue #3469）

日期：2026-11-19 · 分支：fix/issue-3469-email-verification-hardening · 基线：origin/arch-weekly-11 @ f10dc01a0
关联：Issue #3469（P0，标签 auth/backend/bug/db-migration）· 设计：email-verification-design.md

## 背景

现网主机时区 `Asia/Shanghai`，**全部邮箱验证码 100% 秒过期**：`verification_codes` 行的
`expires_at` / `created_at` 由 `time.gmtime()` 写成 UTC 字符串，校验时 `time.mktime()` 按
本地时区解析，东八区下 epoch 提前 28800 秒，`now > exp_ts` 恒成立，`verify_code()` 不进哈希
比对直接走过期分支（现网取证：同码按 `{kid}:HMAC-SHA256` 复算与库内 `code_hash` 逐字节一致，
该行 `used=1 / attempts=0`）。

同一病根让登录失败锁定永不生效：`login_throttle._get_lock_until()` 同型解析，锁定截止时间恒在
过去（现网实测某 identifier 15 分钟窗口失败 12097 次从未被锁）。

沿链路审计另有 15 项逻辑不自洽：邮箱语义分叉（update_email 只写一列 → 解绑误删真实邮箱、
email_verified 恒 0、占位行不参与冲突判定）、不可投递邮箱仍按"已绑定"发码、改绑限流键错位、
先通知后落库、改绑缺二次认证（会话+改绑+重置密码=无口令接管）、限流影子路径零落行、
验证码哈希不存 kid、只比对最新一条、生产环境邮件未配置静默降级、失败原因四合一且响应缺
`error_code`、改绑验证码未绑定目标邮箱、存量列语义分叉、`uq_active_email` 名不副实。

本批范围：Issue 正文 A–Q 全量收口（含 P3 数据治理），一次交付代码 + 文档 + 场景用例。

## 现状问题表

| # | 位置 | 问题 | Issue 条目 |
|---|------|------|-----------|
| M1 | `auth/verification.py:123,269` | `time.mktime(time.strptime(...))` 按本地时区解析 UTC 串 → 验证码落地即"过期"、上次发送时间错位 | A |
| M2 | `auth/login_throttle.py:175` | 同型解析 → `lock_until` 恒在过去，账号/IP 双维度锁定失效 | B |
| M3 | `auth/grape_account_db.py:382-395` | `update_email()` 只 `SET email=?`，不同步 `email_source/email_is_placeholder/email_verified` | C |
| M4 | `auth/grape_account_db.py:1226` + `routes/grape_auth.py:1009` | 解绑走 `clear_placeholder_email()`，判据全落在未更新的标志上 → 解绑第三方删掉用户自绑的真实邮箱 | C1 |
| M5 | 库内实测 | 有邮箱但 `email_verified=0` 62 行 | C2 |
| M6 | `auth/grape_account_db.py:530` | 冲突查询带 `AND (email_is_placeholder=0 OR ...)` → 占位行持有的真实邮箱对外不可见，注册落 500 `ACCOUNT_CREATE_FAILED` | C3 |
| M7 | `routes/grape_auth.py:525,743,860` | 发码前只判 `bool(account["email"])`，`noreply.*`（27 行）/字面串 `"未公开邮箱"`（1 行）仍按已绑定处理 → 设密码/解锁/注销返回 429 且"请先绑定邮箱"分支不可达 | D |
| M8 | `routes/grape_auth.py:608` | 改绑限流键 = 旧邮箱，邮件发往新邮箱 → 新地址零限流，仅剩 IP 维度 | E |
| M9 | `routes/grape_auth.py:634-649` | 先 `send_notification` 后 `update_email`；失败仍发"已变更"，旧邮箱不可投递时异常 `except: pass` 吞掉 | F |
| M10 | `routes/grape_auth.py:617-649` | 改绑只要求 `require_jwt_only` + 新邮箱验证码 → 会话内改绑后立刻找回密码并 `increment_token_version`，全程无原凭据确认 | G |
| M11 | `auth/verification.py:128-155` | `check_rate_limit()` 只读不写；影子路径（未注册/已注册不发码/解绑异常分支）不落行，IP 计数永不增长 | H |
| M12 | `auth/verification.py:52-59` | `_hash_code()` 用 `_get_signing_secret()[0]`，只存 digest 不存 kid → 轮换密钥后 10 分钟内的码全部失效 | I |
| M13 | `auth/verification.py:251-256` | `ORDER BY created_at DESC LIMIT 1` → 重发即废旧码；错码只记最新行，5 次连带新码作废 | J |
| M14 | `routes/grape_auth.py:303` | `if email_configured: verify_code(...)`：SMTP 配置被判空时注册跳过验证码且无告警 | K |
| M15 | `routes/grape_auth.py:594` | 改绑只判 `"@" in new_email`，注册用严格正则 → `a@b` 可绑成账号邮箱 | L |
| M16 | `auth/verification.py:209-222` | 发信成功但 INSERT 异常被吞后仍 `return ok=True` | M |
| M17 | `auth/verification.py:263`、`routes/grape_auth.py:594/600/603/619` | 四种失败压成一句"验证码无效或已过期"；四处 400 无 `error_code`，前端只能回退 `detail.message` | P |
| M18 | `routes/grape_auth.py:608,628` | 码的存储键是 `(旧邮箱, scene)`，与 `new_email` 无关 → "发往 A 的码能确认 B"；占位字面串相同的账号共享 key 互踩 60s 间隔与 3 次窗口 | Q |
| M19 | `auth/grape_account_db.py` 列语义 | `email IS NULL 且 placeholder=0` 85 行、`noreply.*` 27 行、`email_source IS NULL` 94 行、非邮箱形态 1 行；三套判据各读不同列 | N |
| M20 | `src/api/db_setup.py:390-395` | `uq_active_email` 无 status 条件：status=2/4 行仍占唯一键但业务不可见；任何写 `''` 的路径都撞唯一键 | O |

## 目标形态

- **时间语义单一来源**：所有时间列写入 UTC 字符串、解析一律 `calendar.timegm`；仓库内不再出现
  `time.mktime(time.strptime(...))`（本地时间语义点除外，本链路没有）。
- **邮箱判据单一来源**：形态校验 / 可投递 / 冲突资格 / 入库归一分属
  `identity_email.ProviderEmailPolicy.is_valid / is_deliverable / is_private / for_storage`，
  路由与账号层不再各写一套。
- **改绑闭环**：验证码绑定目标邮箱；二次认证（原密码或原邮箱验证码）**有则要求、无则放行**，
  绝不给不可投递邮箱账号制造死锁（它们是"换绑到真实地址"自救的唯一入口）。
- **失败可定位**：`verify_code()` 返回结构化 `error_code`，改绑链所有 400 带 `error_code`，
  前端按码出文案。
- **限流可计量**：影子路径也落记录，IP 维度不再形同虚设；改绑按 `user:<id>` 计频。
- **存量归一 + 唯一键语义收敛**：`''`→NULL；活跃邮箱唯一约束只约束 `status IN (1,3)`。

## 范围

- P0：M1、M2（热更级）。
- P1：M3–M10、M17、M18。
- P2：M11–M16。
- P3：M19（归一脚本，dry-run + 备份）、M20（迁移改造唯一索引）。

## 非目标

- 邮件服务商/短信通道选型；前端 UI 改版（只加必要的二次认证输入与 error_code 文案映射）。
- 与邮箱链路无关的 IM/OAuth 重构；不改已有迁移版本号；不做"改绑后 24h 禁止找回"这类会锁死
  不可投递邮箱账号的加固（Issue 正文 Q 明确禁止）。

## 场景用例清单

| 编号 | 场景 | 前置 | 动作 | 预期 | 用例 |
|------|------|------|------|------|------|
| S257 | 验证码立即校验通过 | `TZ=Asia/Shanghai`，固定码 | send_code(register) → 立即 verify_code | ok；行 `used=1 attempts=0` | test_issue_3469_verification.py::test_utc_expiry_not_instant_in_utc8 |
| S258 | 过期码拒绝 | 行 `expires_at` 改为过去 | verify_code | `ok=False, error_code=CODE_EXPIRED` | test_issue_3469_verification.py::test_utc_expiry_not_instant_in_utc8（含过期分支） |
| S259 | 错码计数与上限 | 有效码行 | 连提交 5 次错码 | 每次 `CODE_INVALID` 且 `attempts+1`；第 5 次后该行 `used=1` | test_issue_3469_verification.py::test_wrong_code_increments_attempts_then_burns |
| S260 | 重发后旧码仍可用 | 同 key+scene 两行 | 用第一封的码校验 | ok，且同场景其余行一并 `used=1` | test_issue_3469_verification.py::test_resend_keeps_old_code_valid |
| S261 | 验证码绑定目标邮箱 | 码按 target=A 生成 | verify_code(target=B) | `CODE_INVALID`（不能跨地址确认） | test_issue_3469_verification.py::test_target_binding |
| S262 | 哈希存 kid 可跨密钥轮换 | 行 hash 形如 `kid:digest` | 用该 kid 对应密钥校验 | ok；旧格式 digest 走当前密钥回退 | test_issue_3469_verification.py::test_hash_carries_kid_and_old_kid_still_verifies |
| S263 | 影子路径推高 IP 计数 | 未注册邮箱 | 连续 check_rate_limit | 第 2 次受 60s 间隔限制；落影子行（`used=1`） | test_issue_3469_verification.py::test_check_rate_limit_writes_shadow_record |
| S264 | 落库失败不再谎报已发送 | INSERT 抛异常 | send_code | `ok=False, error_code=CODE_STORE_FAILED` | test_issue_3469_verification.py::test_store_failure_returns_false |
| S265 | 失败原因四值可分 | 构造四类输入 | verify_code | 分别 `CODE_NOT_FOUND/CODE_EXPIRED/CODE_USED/CODE_INVALID` | test_issue_3469_verification.py::test_failure_reasons_are_structured |
| S266 | 锁定真正生效 | `TZ=Asia/Shanghai` | 连续 5 次 record_failure → check_or_raise | 拒绝且 `lock_until > now`（SQL 复算） | test_issue_3469_throttle.py::test_account_lock_effective_in_cst |
| S267 | IP 维度锁定 | 同 IP 20 次失败 | check | `ip_locked=True`、`retry_after>0` | test_issue_3469_throttle.py::test_ip_lock_effective_in_cst |
| S268 | 改绑后三列归一 | OAuth 占位账号 | update_email 真实邮箱 | `email_source='user'`、`email_is_placeholder=0`、`email_verified=1` | test_issue_3469_email_identity.py::test_real_email_sets_source_placeholder_verified |
| S269 | 解绑不清自绑邮箱 | S268 之后 | clear_placeholder_email(user, provider) | 邮箱仍在（C1） | test_issue_3469_email_identity.py::test_clear_placeholder_email_keeps_real_email |
| S270 | 占位行参与冲突判定 | 占位=1 且持有真实邮箱 | is_email_registered(同邮箱) | True（C3，注册侧 409 而非 500） | test_issue_3469_email_identity.py::test_placeholder_row_holding_real_email_is_visible |
| S271 | 邮箱冲突返回 conflict | 邮箱已被占 | update_email 到另一账号 | 返回 `"conflict"` | test_issue_3469_email_identity.py::test_second_account_gets_conflict |
| S272 | 不可投递邮箱按未绑定处理 | `noreply.*` 账号 | set-password/send-code | 400 `EMAIL_UNBOUND`，不返回 429 | test_issue_3469_routes.py::test_set_password_send_code_rejects_undeliverable_mailbox |
| S273 | 改绑双维度限流 | 旧邮箱 A、新邮箱 B | change-email/send-code | 验证码与计数键=`user:<id>`（每小时 5 次），**且**新地址维度独立限流（60s / 3 次 10 分钟，落影子记录），分别返回 `CHANGE_EMAIL_TOO_OFTEN` / `CHANGE_EMAIL_TARGET_LIMITED` | test_issue_3469_email_change_service.py::test_hourly_cap_rejects_before_sending、::test_request_code_uses_user_key_and_binds_target、test_issue_3469_review_fixes.py::test_target_address_rate_limited |
| S274 | 改绑成功才通知 | UPDATE 会冲突 | change-email 提交 | 不发"已变更"通知；409 `EMAIL_TAKEN` | test_issue_3469_email_change_service.py::test_no_success_notification_when_update_conflicts |
| S275 | 改绑二次认证（有密码） | 账号有密码 | change-email 提交缺 password | 400 `NEED_SECOND_FACTOR` + `require_factor=password`；错密码 `PASSWORD_INVALID` | test_issue_3469_email_change_service.py::test_password_factor_required_then_validated、test_issue_3469_routes.py::test_commit_needs_second_factor_maps_require_factor |
| S276 | 改绑二次认证（原邮箱码） | 无密码 + 原邮箱可投递 | send-code → 提交缺 old_email_code | 400 `NEED_SECOND_FACTOR`；补正确原邮箱码后成功 | test_issue_3469_email_change_service.py::test_old_email_code_required、test_issue_3469_email_change_service.py::test_request_code_sends_confirm_code_to_old_mailbox_when_no_password |
| S277 | 不可投递邮箱不死锁 | 无密码 + `noreply.*` | send-code（require_factor=none）→ 提交 | 换绑成功，验收 11 的逃生口成立 | test_issue_3469_email_change_service.py::test_escape_hatch_for_undeliverable_mailbox |
| S278 | 生产环境邮件未配置拒绝注册 | `is_production_env()` 真、`is_email_configured()` 假 | register/verify | 503 `EMAIL_NOT_CONFIGURED`（不再静默跳过） | test_issue_3469_routes.py::test_register_verify_fails_closed_without_email_config_in_production |
| S279 | 改绑格式校验统一 | 新邮箱 `a@b` | change-email/send-code | 400 `EMAIL_FORMAT` | test_issue_3469_email_change_service.py::test_request_code_format_same_and_taken |
| S280 | 失败响应带 error_code | 与原邮箱相同 / 已被占用 / 码过期 | change-email 链各分支 | 400 分别 `EMAIL_SAME` / `EMAIL_TAKEN` / `CODE_EXPIRED` | test_issue_3469_routes.py::test_send_code_maps_domain_errors、test_issue_3469_routes.py::test_commit_conflict_returns_409 |
| S281 | 活跃邮箱唯一索引语义 | 旧形态索引 + status=4 持邮箱 | 跑迁移 | 同邮箱 status=1 可插入；两条 status=1/3 仍冲突；`''`→NULL | test_issue_3469_email_index_migration.py::test_migration_resculpts_index_and_frees_tombstone_rows |
| S282 | 索引缺失必须不盖章 | monkeypatch 让建索引静默失败 | 跑迁移 fn | raise（下次启动重试） | test_issue_3469_email_index_migration.py::test_silent_index_creation_failure_raises |
| S283 | 存量归一脚本幂等 | 构造 5 类异常行 | dry-run → --apply → 再 dry-run | dry-run 不改库；--apply 后计数符合规则且先建备份表；二次 dry-run 待改 0 | test_issue_3469_email_index_migration.py::test_script_dry_run_changes_nothing、test_issue_3469_email_index_migration.py::test_script_apply_normalizes_and_reports、test_issue_3469_email_index_migration.py::test_script_apply_is_idempotent |
| S284 | 前端二次认证输入 | 后端 `require_factor=password` / `old_email_code` | 打开改绑弹层并提交 | 渲染对应输入框并放进 body；`none` 不渲染 | tests/js/test_issue_3469_change_email.js |
| S285 | 前端 error_code 文案 | 后端 400 带 error_code | 改绑失败 | 按码出文案，不回退统一话术 | 同上 |
| S286 | 注销验证方式判据 | `/me` 返回 `email_deliverable=false` | 打开注销弹层 | 不出现"邮箱验证码"方式（验收 11） | 同上 |
| S287 | 改绑二次因子输入（后端契约） | `require_factor=password` / `old_email_code` | 请求缺因子 | 400 `NEED_SECOND_FACTOR` + `require_factor`；补正确因子后成功 | test_issue_3469_email_change_service.py、test_issue_3469_routes.py |
| S288 | 验证码键与目标绑定（领域侧） | 服务收到 target=A | 校验时传 target=B | 不命中；send/verify 均带 `user:<id>` 与同 target | test_issue_3469_email_change_service.py::test_commit_verifies_with_target_and_user_key |
| S289 | 每用户每小时换绑上限 | `count_sends` 达 5 | 再次 send-code | 429 `CHANGE_EMAIL_TOO_OFTEN`，且未发码 | test_issue_3469_email_change_service.py::test_hourly_cap_rejects_before_sending |
| S290 | 改绑失败不误发成功通知 | UPDATE 冲突 | 提交 | 只发"变更失败"通知 | test_issue_3469_email_change_service.py::test_no_success_notification_when_update_conflicts |
| S291 | 验证码模块职责守卫 | — | 静态扫描 | 值对象零 IO；`verification_codes` 字面量只在仓储；门面 API 可导出 | test_issue_3469_verification_modules.py |
| S292 | 路由层错误映射 | 领域抛错 | 调 HTTP 接口 | 状态码 + `error_code` + `require_factor` 一一对应 | test_issue_3469_routes.py |
| S293 | kid 前缀哈希可落库可校验 | JWT 密钥池启用（kid=k1） | 发码→落库→校验 | `code_hash` 列宽 ≥191；`k1:<64hex>` 原样存取并能验过 | test_issue_3469_review_fixes.py::test_stored_hash_with_kid_exceeds_legacy_width / ::test_kid_hash_roundtrip_verifies、test_issue_3469_mysql_gate.py::test_code_hash_column_fits_kid_prefixed_hash |
| S294 | 二次因子输错不烧码 | 账号有密码，已发新邮箱码 | 提交错密码 → 再提交正确密码+同一个码 | 第一次 `PASSWORD_INVALID`；第二次成功，码只需一个 | test_issue_3469_review_fixes.py::test_wrong_second_factor_does_not_burn_code |
| S295 | 注销发码按可投递判定 | `noreply.*` 账号 | delete-account/send-code | 400 `EMAIL_UNBOUND`，不再 429 | test_issue_3469_review_fixes.py::test_delete_account_send_code_rejects_undeliverable |
| S296 | 旧邮箱不可投递改走站内信 | 无密码 + `noreply.*` | 改绑成功 / 改绑失败 | 不向 `noreply` 发信；落站内信（邮箱已变更 / 邮箱变更失败） | test_issue_3469_review_fixes.py::test_undeliverable_old_email_falls_back_to_inapp / ::test_failed_change_with_deliverable_old_email_notifies_failure |
| S297 | MySQL 真库门禁 | `GRAPE_GATE_TEST=1` + 可连 MySQL | 跑迁移与新装 DDL | `code_hash>=191`、`active_email` STORED 生成列、活跃唯一/墓碑让键/`''`归NULL、二次执行零改动 | test_issue_3469_mysql_gate.py（CI 默认 skip；本机 MySQL 8.0.46 实例实跑 3 passed，实例已销毁） |
| S298 | 清空占位邮箱语义正确 | OAuth 占位账号解绑 provider | clear_placeholder_email | `email=NULL` 且 `placeholder=1`、`updated_at` 为 UTC | test_issue_3469_email_identity.py::test_clear_placeholder_email_keeps_real_email（+ 断言） |
| S299 | 并发消费只成功一次 | 同一码两个请求 | 同时 verify(consume=True) | 只有一个 `mark_all_used` 拿到 rowcount>0，另一个 `CODE_INVALID` | test_issue_3469_verification.py::test_mark_all_used_second_call_reports_zero / ::test_consume_false_does_not_burn_code |
| S300 | 新地址维度限流（E 复审补齐） | 同一账号连续对多个新地址发码 / 多账号对同一新地址发码 | request_code | 目标地址维度 60s 间隔、3 次/10 分钟，超限 `CHANGE_EMAIL_TARGET_LIMITED` | test_issue_3469_review_fixes.py::test_target_address_rate_limited |
| S301 | 注册侧邮箱格式同源（L 复审补齐） | 邮箱 `a@b` | register/send-code | 400 `EMAIL_INVALID`（与改绑同用 `ProviderEmailPolicy.is_valid`） | test_issue_3469_review_fixes.py::test_register_send_code_uses_identity_email_format |
| S302 | 存量归一覆盖 verified=0（N/C2 复审补齐） | 真实邮箱且 verified=0（含非 user 来源） | 007 `--apply` | 全部置 verified=1；`email_source IS NULL` 同时补 `user`；二次 dry-run 0 待改 | test_issue_3469_email_index_migration.py::test_script_apply_normalizes_and_reports |
| S303 | 解锁发码不静默谎报（N3） | 账号邮箱不可投递 / 账号不存在 | unlock/send-code | 200 + 通用 hint（不区分账号存在性，防枚举） | test_issue_3469_review_fixes.py::test_unlock_send_code_returns_hint |
| S304 | 两套前端判据同源（N4） | 壳前端 / Vue 前端 | 读 profile | `/auth/profile` 与 `/auth/me` 都返回 `email_deliverable`，壳侧优先用它、域名表仅兜底 | tests/js/test_issue_3469_change_email.js、test_issue_3469_review_fixes.py::test_profile_exposes_email_deliverable |
| S305 | O 双机 DDL 撞车可恢复（部署说明） | 两台主控同时重启 | 迁移 202611190101 | 后到者可能 `Duplicate key name` → 不盖章、下次启动重试（无害），要静默就先停另一台 | 部署清单（design D12），无自动化断言 |

## 验收标准（对 Issue 正文第 5 节逐条）

1. 发码后 28 秒内提交正确码 → 200，行 `used=1 attempts=0`（S257）。
2. 错码 → `CODE_INVALID` 且 `attempts+1`；5 次后行 `used=1`（S259）。
3. 5 次登录失败后立即登录被拒，`lock_until > now`（S266/S267）。
4. 不可投递邮箱点"设置密码" → 明确提示先绑定邮箱，不返回 429（S272）。
5. 占位账号改绑后三列归一（S268）。
6. 改绑后解绑第三方，自绑邮箱仍在（S269）。
7. 并发绑同一邮箱到不同账号 → 一方 409 无 500（S270/S271 + 路由兜底）。
8. 重发后用第一封的码仍通过（S260）。
9. 全仓 grep 无 `time.mktime(time.strptime(...))`（守卫见 design D12）。
10. 新增单测在 `TZ=Asia/Shanghai` 下通过，且在修复前代码上失败（S257 反证）。
11. 不可投递邮箱账号仍能通过 change-email 完成换绑，换绑后 set-password 可走通；
    `canVerifyByEmail` 不把不可投递地址当可用验证方式（S277/S286）。

## 实施状态（滚动）

| 条目 | 状态 | 落点 |
|------|------|------|
| A/B | 代码+用例已落 | `auth/verification_code.py`（值对象）、`auth/login_throttle.py` |
| C/C1/C2/C3 | 代码+用例已落 | `auth/grape_account_db.py` |
| D/E/F/G/K/L/P/Q | 代码+用例已落（路由只留映射） | `auth/email_change.py`（服务+端口）、`routes/grape_auth.py`（接线） |
| 前端 G/P/验收11 | 已落 | `shell/js/app.js`、`AccountView.vue`、`locales/{zh,en}`（cacheVer 已 bump） |
| H/I/J/M/P/Q | 代码+用例已落 | `auth/verification_store.py`（仓储）、`auth/verification.py`（门面） |
| N/O | 迁移+脚本+用例已落 | `mgmt/migrations/active_email_uniqueness.py`、`scripts/migrations/007_*`、`db_setup.py`（一行注册） |
| 复审修复（kid 列宽 / 因子顺序 / D 补齐 / F 降级 / MySQL 门禁） | 代码+用例已落 | `migrations/verification_hash_width.py`、`auth/email_change.py`、`routes/grape_auth.py`、`auth/grape_account_db.py`、`tests/api/test_issue_3469_mysql_gate.py` |
| 复审第二轮（通知真降级 / 目标可投递 / 密码因子节流 / IP 计数与错误码 / P3 四项） | 代码+用例已落 | `runtime/im/notify.py`（同步入队）、`runtime/public`（返回 bool）、`auth/email_change.py`、`auth/verification*.py`、`auth/grape_account_db.py`、`db_setup.py`、`scripts/migrations/007_*`、`tests/mgmt/test_issue_3469_routes.py`（session client） |
| 目标环境实机复验 | 待部署后执行 | 验证实例 + 浏览器实测（多实例须同时热更，见 design D12） |

## 第二轮与第三轮复审场景（S306–S315）

| 编号 | 场景 | 期望 | 用例 |
|------|------|------|------|
| S306 | 把 `1+u@noreply.github.com` 之类不可投递地址作为改绑目标 | 400 `EMAIL_UNDELIVERABLE`，账号邮箱不变 | `test_undeliverable_target_rejected` |
| S307 | 拿到会话后反复用 change-email 试当前密码 | 每次失败落计数，超限 429；成功则清零 | `test_password_factor_uses_login_throttle` |
| S308 | 同一 IP 连发多次改绑码（每账号一次，含 target 影子行） | IP 计数只算真发码+影子路径，不因 target 影子行被放大；超限报 `IP_LIMITED` | `test_ip_limit_reports_ip_limited`、`test_ip_count_ignores_change_email_target_shadow` |
| S309 | 站内信降级在无事件循环的同步路由里触发 | 同步写进 IM outbox（不再 `no running loop, skip`），无可用安装时返回 False 并记日志 | `test_sync_enqueue_when_no_loop` |
| S310 | 账号 id 是 UUID 时发站内信 | 不做 `int()` 强转；runtime 返回 False 时适配器如实回传 False | `test_notification_adapter_does_not_coerce_id_and_reports_failure` |
| S311 | 解绑/清空邮箱 | `email`/`email_source` 置空且 `email_is_placeholder=1`、`email_verified=0` | `test_clear_placeholder_email_zeroes_verified` |
| S320 | 注册/找回/设密码/注销/解锁的发码失败 | 429 响应带 `error_code` 与 `retry_after`：限流=`RATE_LIMITED`/`IP_LIMITED`（含秒数），**发信失败=`CODE_SEND_FAILED`**（`send_code` 三个失败分支已补码），落库失败=`CODE_STORE_FAILED`；兜底为 `CODE_SEND_FAILED`，不得把 SMTP 故障标成限流 | `_rate_limit_detail()`（`routes/grape_auth.py`），实跑 `429 {"error_code":"RATE_LIMITED","retry_after":59}` |
| S321 | 三库模式（`GRAPE_3DB_ENV`）启动 | 启动器必须导出 `DEVOPS_DB_BACKEND=mysql`；否则 DSN 只给了 host/port/user/password/name，后端判定退回 sqlite，四个进程各自落到本地库（db-guard 却照旧打印 DSN） | `scripts/grape-systemd-run.sh` 三库分支 |
| S316 | 账号被管理员禁用（status=0）后，他人用同邮箱注册 | 唯一键仍被占用 → 注册失败；读侧同 email 多行时**确定性优先活跃/冷静期行**（不撞 LIMIT 1） | `test_disabled_account_still_occupies_email`、`test_email_lookup_is_deterministic`、`test_disabled_email_counts_as_registered` |
| S317 | NONE 档（无可验证因子）完成改绑 | 落审计事件 `email_change_no_secondary_factor`（设计 D6 要求，此前未实现） | `email_change.py::_require_second_factor` + 审计 grep |
| S318 | 007 执行 R1（置空非邮箱形态） | `email` 置空时 `email_verified` 同步归零，不留"空邮箱但已验证"的畸形 | `scripts/migrations/007_*` R1 语句 |
| S319 | 生产环境未配置邮件服务时发注册码 | 与 verify 同口径 `503 EMAIL_NOT_CONFIGURED`（不再 200 软提示把用户引到必失败的路径） | 路由 `register_send_code` |
| S314 | 60s 重发间隔被拒 | 429 `RATE_LIMITED` + `retry_after`（不再是 `CODE_SEND_FAILED`）；窗口/`IP_LIMITED` 各自独立错误码 | `test_send_interval_not_reported_as_send_failure`、`test_target_address_rate_limited` |
| S315 | 契约副本与实现请求 schema 漂移 | 两份副本 `ChangeEmailBody` 必须含可选 `password`/`old_email_code`（`required` 仍两键）；`check-openapi-endpoints` 盲区由新守卫覆盖 | `tests/api/test_openapi_request_schema_drift.py`（5 passed） |
| S313 | D105 之后的 UUID 账号走站内信降级（改绑成功/失败） | 真实 `out_track_id` 可出单号、真正入队 outbox；不再因裸 `int()` 抛异常静默丢失（缺陷 #3510） | `tests/runtime/test_issue_3510_im_uuid_notify.py`（5 passed；同文件在修复前 4 failed） |
| S312 | 007 脚本在 MySQL 上 dry-run | 横幅打印真实后端 host/db（不再恒为 `<session default>`） | 实跑 dry-run（只读） |
