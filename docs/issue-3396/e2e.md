# E2E 验证清单（Issue #3396，PR #2078）

> 逐条对应 spec.md Acceptance Criteria。自动化回归见
> `tests/runtime/test_issue_3396_role_tags_seed_compat.py`、
> `tests/runtime/test_issue_3396_role_caps_backfill.py`、
> `tests/api/test_i18n_locale_cache_ratchet.py`、`tests/kernel/test_mysql_compat.py`；
> 本文记录端点级 / 双后端的手工验证步骤与结果，供 QA 复审复核。

## A1/A2 调度放开与 fail-closed

- 勾了 `sre`（或 `all`）的引擎可认领 SRE 任务；未勾 `sre` 且未勾 `all` 的引擎
  派发与认领两侧均不可调度，决策日志出现 `role_no_candidates`。✅ 自动化覆盖。

## A3/A4 标签事实源与心跳不覆盖

- UI 改标签即时生效（engine_capabilities 为准，无需重启/重注册）。
- 心跳/再注册不覆盖已有角色行（含 `enabled=0` 的显式停用行）：
  - 注册声明 `['sre']` → 停用 `sre`（`tags_user_set=0`）→ 再注册/心跳后
    `sre` 仍为 `enabled=0`，无第二行（见
    `test_register_does_not_resurrect_disabled_declared_role` /
    `test_heartbeat_does_not_resurrect_disabled_declared_role`）。✅
  - 声明 `['chat','sre']` 且已存在停用 `sre` 行 → 整组跳过，不出现
    「chat 落 enabled=1、sre 1062 被吞」的半组启用
    （`test_register_seed_skips_whole_group_when_sibling_role_disabled`）。✅

## A8b/A8c 播种与补齐口径

- 声明即收窄：`['sre']` → 能力行只落 `sre`；非法声明 → 回落 `all`。
- 存量迁移面：行内 `role_tags=['sre']` + 只剩自动补的 enabled `all` →
  再注册/心跳改写为 `sre`；停用的 `all` 不触发改写。
- 补齐原子性：单条多行 `INSERT ... SELECT ... WHERE NOT EXISTS`，
  失败不留半组；派生表带别名 `AS _rows`（MySQL/MariaDB 1248/1064 回归锁：
  `test_role_backfill_derived_table_alias_preserved`；MariaDB 10.5.29 实跑
  由 QA 在 fce85234f 验证通过）。

## A5 写权限分层

- 本机直连（`credential_id=0`）：任意登录用户 `PUT /runtimes/{id}/tags` → 200。
- 凭据引擎（`credential_id≠0`）：仅凭据 owner 本人 / org owner+admin / 平台管理员 → 200；
  其余登录用户与仓库管理员 → 403；`session_token` → 403；不存在 runtime → 404。✅ 自动化覆盖。

## 前端语言包缓存

- `locales/{zh,en}/runtimes.json` 新增 5 key 后 `__I18N_CACHE_VER__` 由 `'90'` 升至
  `'91'`（index.html 内联 + `tests/api/test_i18n_locale_cache_ratchet.py`
  `CACHE_VER['index.html']` 同步），老客户端不再命中 `?v=90` 旧语言包。✅

## 双后端（架构守护 F1）

- SQLite：上述全部自动化回归通过。
- MySQL/MariaDB：角色回填语句经 `translate_sql` 语义不变且派生表别名保留；
  实机（MariaDB 10.5.29）由 QA 验证：零角色行补齐整组落库、已有行整组跳过。✅
