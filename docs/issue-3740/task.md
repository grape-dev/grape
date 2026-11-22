# Task: Issue #3740 任务拆解（TDD 红/绿切片）

| # | 切片 | 红（测试） | 绿（实现） | 状态 |
|---|------|-----------|-----------|------|
| 1 | legacy 回落日志首打 + 1‰ 采样 | `tests/kernel/test_issue3740_repo_bot_denoise.py::test_legacy_fallback_log_first_hit_and_sample` | `repo_bot_observability.should_warn_legacy_fallback` + `_fetch` 调用点 | ✅ |
| 2 | counter `grape_repo_bot_legacy_fallback_total{provider,repo}` | `::test_legacy_fallback_counter_counts_every_storage_read` | `repo_bot_observability.install/observe` + insight registry 安装 | ✅ |
| 3 | `(repo_key, field)` 短 TTL 缓存（含 miss） | `::test_fetch_cache_collapses_repeated_resolution` | `token_resolver` 缓存原语 + `_fetch` 改造 | ✅ |
| 4 | 缓存失效（TTL）与主动失效 / 轮换 | `::test_fetch_cache_ttl_expiry`、`::test_fetch_cache_invalidation_on_rotation` | TTL 判据、`invalidate_repo_bot_token_cache`、`mark_expired` 失效 | ✅ |
| 5 | rc 命中缓存 + 审计随缓存降频 | `::test_rc_hit_cached_and_audited_once_per_storage_read` | `_fetch` 审计改写 | ✅ |
| 6 | rc 写路径主动失效 | `tests/mgmt/test_issue3740_bot_cred_backfill.py::test_rc_upsert_invalidates_fetch_cache` | insight public upsert/clear 失效钩子 | ✅ |
| 7 | 丢失函数回归 `backfill_repo_credentials_from_columns` | `test_issue3740_bot_cred_backfill.py::test_restored_backfill_from_columns_fills_all_platforms` | db_setup 恢复实现 | ✅ |
| 8 | bot 列回填（dry-run / 幂等 / 密文归一） | `::test_bot_column_backfill_dry_run_writes_nothing`、`::test_bot_column_backfill_apply_is_idempotent` | `backfill_repo_bot_token_columns_to_rc()` | ✅ |
| 9 | 回填验证报告 | `::test_verify_backfill_reports_gaps_then_zero` | `verify_repo_bot_credential_backfill()` | ✅ |
| 10 | 盖章 | `::test_stamp_backfill_writes_migration_ledger` | `stamp_repo_bot_credential_backfill()` | ✅ |
| 11 | CLI 可执行（dry-run 默认零写） | `::test_cli_dry_run_by_default` | `scripts/backfill_repo_bot_credentials.py` | ✅ |
| 12 | 测试隔离 | 现有用例回归（尤其 `tests/mgmt/test_s82_dualwrite_removal.py`） | `tests/conftest.py` 逐用例清缓存/采样状态 | ✅ |

验收对账：
- 降噪：切片 1/2；缓存：3/4/5/6；回填：7/8/9/10/11；回归：全部现有测试。
- 硬边界：不删 legacy 回落分支；不 DROP COLUMN；生产回填执行归 sre（本单只交付可执行 + 可验证 + 可盖章的手段）。

## 落地结果（实现侧）

- 新用例：`tests/kernel/test_issue3740_repo_bot_denoise.py` 13 条 + `tests/mgmt/test_issue3740_bot_cred_backfill.py` 12 条，全绿；两文件已接入 `.github/workflows/windows-smoke.yml` linux-backend 门禁。
- 复审整改（PR #1963 Required）：①探列失败新增 `probe_ok/error`，verify/stamp/apply 失败退出（不再空缺口伪装通过）；②下线判据改为 counter 窗口增量 + legacy 审计零命中（日志 1/1000 采样不足以作为判据）；③rc 读失败回落的 legacy 值不入缓存（`legacy_rc_error`），下次重试权威源；④新增用例接入 CI。
- 回归：`test_s82_dualwrite_removal` / `test_s80_repo_credentials` / `test_uc41_rc_schema` / `test_s128_*` 由 7 ERROR（缺 `backfill_repo_credentials_from_columns`）转为通过；`tests/api/test_migration_track_guard.py` 通过；`scripts/check-arch.py` PASS；新增读点 0（rc 读改走 insight public）。
- 未注册自动迁移（生产回填由 sre 用脚本 + `--verify` + `--stamp` 执行）；盖章号 `202611220001`。
- 已知既有失败（基线同源、非本单引入）：`tests/kernel` 52 条与基线逐条一致；`test_s118_*` / `issue_372_*` 等按 `_SRC = tests/src` 取源码路径，整套测试目录重组后未同步；`check-all` 的读点清单门禁基线过期（涉 5 条本次未改文件）。