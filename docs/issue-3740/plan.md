# Plan: Issue #3740 — 降噪 + 缓存 + 指标 + 回填路径

## 1. 改动清单

| # | 文件 | 类型 | 内容 |
|---|------|------|------|
| 1 | `src/kernel/repo_bot_observability.py` | 新增 | counter `grape_repo_bot_legacy_fallback_total{provider,repo}` 的可安装收集器 + 首打/1‰ 采样判定 |
| 2 | `src/domains/insight/observability/__init__.py` | 修改 | 把新收集器 `install(registry)` 到 insight 持有的 registry |
| 3 | `src/kernel/token_resolver.py` | 修改 | `RepoBotToken._fetch()`：`(repo_key, field)` 短 TTL 缓存、日志首打+采样、counter 全量计数、审计随缓存降频；`mark_expired/clear_expired` 失效缓存；公开 `invalidate_repo_bot_token_cache()` |
| 4 | `src/domains/insight/public/__init__.py` | 修改 | rc 写路径（upsert/clear）后失效 kernel 缓存（轮换同进程即时可见） |
| 5 | `src/api/db_setup.py` | 修改 | 恢复 `backfill_repo_credentials_from_columns()`；新增 bot 列回填 / 验证 / 盖章原语 |
| 6 | `scripts/backfill_repo_bot_credentials.py` | 新增 | 回填 CLI（`--dry-run` 默认 / `--apply` / `--verify` / `--stamp`） |
| 7 | `tests/conftest.py` | 修改 | 逐用例清空 token 缓存与采样状态（进程级 memo 的测试隔离，先例：`_reset_gsb_lb_state`） |
| 8 | `tests/kernel/test_issue3740_repo_bot_denoise.py` | 新增 | 采样 / 缓存 / 指标 / 审计口径 |
| 9 | `tests/mgmt/test_issue3740_bot_cred_backfill.py` | 新增 | 回填幂等 / dry-run / 盖章 / 验证 / 丢失函数回归 |
| 10 | `docs/issue-3740/{spec,plan,task}.md` | 新增 | 本单文档 |

## 2. 分层与依赖方向

- `repo_bot_observability` 放 kernel，与 `src/kernel/org_metrics.py`、`src/kernel/gsb/metrics.py` 同款「可安装收集器 + 无副作用 observe」：kernel 不 import domains，registry 由 insight 域持有并注入。
- 缓存与解析同文件（`token_resolver.py`），只依赖 kernel 内 repo_key / ports，不新增跨域依赖。
- 回填原语沿用 `mgmt_mig.managed_repos_select_column` / `insight_mig.INSERT_IGNORE_GRAPE_T_REPO_CREDENTIALS`，SQL 仍由归属域持有。
- insight 写路径 → kernel 失效函数：domain→kernel 方向合法（kernel 在更低层），调用点用 try/except 包裹，缓存失效失败不影响写入。

## 3. 缓存设计

```
_REPO_BOT_CACHE[(repo_key, field)] = (expires_at, source, value, cred_id)
    source ∈ {"rc", "legacy", "none"}     # none = rc miss 且 legacy 列空
```

- TTL 默认 60s；`GRAPE_REPO_BOT_TOKEN_CACHE_TTL` 可覆盖（clamp 到 [0, 60]，0 = 关闭缓存）。
- 有界：条目数超 `_REPO_BOT_CACHE_MAX_ENTRIES`（2048）先清过期项，仍超则整表清空（进程内 memo，非持久，丢失无副作用）。
- 失效接口：`invalidate_repo_bot_token_cache(repo_key="", field="")`（空参 = 全清，返回清理条数）。
- 命中不写审计：审计口径 = 「一次实际存储读」，Issue 要求的写放大随之消除；rc 命中在回填后仍产生 `repo_bot_hit`（每仓库每 TTL 窗口 1 行/进程），验收所要求的「上升」成立。

## 4. 采样设计

```
should_warn(repo_key, field):
    n = 该 (repo_key, field) 的累计 legacy 存储读次数
    return n == 1 or n % 1000 == 0        # 首打必报 + 1/1000 采样
```

- 采样状态独立于缓存（缓存只减少存储读次数，采样按真实存储读判定，两者解耦）。
- 未采样时降 `log.debug`（不丢定位能力，不淹告警面）。
- 日志文本保持 `RepoBotToken: historical column fallback hit (rc miss) repo=… field=…` 不变（仅作 grep 定位）。⚠️ 1/1000 采样会让「持续偶发回落」出现零告警，故**下线判据改为 counter `grape_repo_bot_legacy_fallback_total` 窗口增量 == 0 + `repo_bot_hit_legacy_column` 审计零命中**，日志不再是判据。

## 5. 回填路径

| 原语 | 语义 |
|------|------|
| `backfill_repo_credentials_from_columns()` | 恢复丢失实现：8 个平台列 → rc（幂等 INSERT OR IGNORE，列存在性守卫，原值拷贝） |
| `backfill_repo_bot_token_columns_to_rc(dry_run=False)` | 4 个 `bot_*_token` 列 → rc `cred_type='bot'`；值归一（明文/密文统一回填为密文）；返回 per-column 计数 + 待回填明细 |
| `verify_repo_bot_credential_backfill()` | 报告：legacy 非空仓库数、rc bot 行数、仍缺 rc 的仓库清单、覆盖率 |
| `stamp_repo_bot_credential_backfill()` | 在 `grape_t_schema_migrations` 写 `version=202611220001 / name=backfill-repo-bot-token-columns-to-rc`（INSERT OR REPLACE 幂等），满足四步 checklist 的「①回填全量盖章」 |

CLI 顺序（sre 执行口径）：
1. `python scripts/backfill_repo_bot_credentials.py --verify`（回填前基线）
2. `... --apply`（执行回填；`--dry-run` 为默认，只报计划零写）
3. `... --verify --stamp`（回填后验证 + 盖章；验证不通过不盖章）

## 6. 测试 seam

| 切片 | seam | 断言 |
|------|------|------|
| 采样 | `RepoBotToken._fetch()` + logger `devops-bot`（caplog） | 首打 1 条 WARNING、2..999 条 0、1000 条 1 条 |
| 指标 | `repo_bot_observability.install(CollectorRegistry())` + `generate_latest` | `grape_repo_bot_legacy_fallback_total{provider,repo}` 计数值 = 实际存储读次数 |
| 缓存 | `_repos.repo_bot_token_from_legacy_column_any` 调用计数 spy | 同 key 两次解析 = 1 次存储读；TTL 过期后 = 2 次 |
| 轮换 | 真实 DB：legacy 列改写 + `invalidate_repo_bot_token_cache` | 主动失效后立即读到新值 |
| 回填 | `src.api.db_setup` 原语 + 真实测试库 | 幂等（二次 0）、dry-run 零写、盖章行存在、verify 缺项清零 |

## 7. 执行顺序

1. RED：新增两套测试（约定不存在 → 红）
2. GREEN：kernel 降噪/缓存/指标 → 回填原语 → CLI → conftest 隔离
3. 回归：`tests/kernel`、`tests/mgmt`、`tests/api/test_migration_track_guard.py`、`tests/runtime` 相关
4. 自审查（5 轴 + pre-PR 增量检查）→ 提交 → PR（base `arch-weekly-11-ljt`）