# Issue #3731 任务拆解

> 目标合入分支：`arch-weekly-11-ljt` | 开发分支：`feat/issue-3731`

| # | 切片 | 测试 seam | 红（失败测试） | 绿（最小实现） | 状态 |
|---|------|-----------|----------------|----------------|------|
| 1 | 路由解包收敛到三元组 | `_repo_target()` / `_repo_bot_token()` 返回值 | `test_repo_target_three_tuple_unpack`（旧实现实抛 `ValueError: too many values to unpack (expected 2)`） | `_repo_target` / `_repo_bot_token` 改 3 元解包 | ✅ 4b4e2af72 红 / d9ea1a148 绿 |
| 2 | 标题截断 + 查重同键 | `_create_provider_issue()` payload / `_find_existing_open_issue()` 查重键 | `test_long_title_clipped_before_create`、`test_clipped_title_reuse_existing_issue` | `_MAX_TITLE_LEN` + `_clip_title()` 接入两处 | ✅ 08745dcee 红 / 20d2ef5de 绿 |
| 3 | 失败轮次持久化 + ERROR 升级 | `convert_pending_feedback()` 返回值 + `user_feedback.convert_fail_count` | 三条用例（缺列即 `no such column`） | `convert_fail_count` 列 + `feedback_bump_convert_fail` + `_note_failure` | ✅ f8349136e 红 / af78fa892 绿 |
| 4 | 历史用例对齐 #673 语义 | `tests/runtime/test_feedback_issue_converter.py` | 基线已红（user+repo_key 现应转换） | 用例改为「带 repo_key 的 user 反馈转换、纯意见类不转换」 | ✅ 86376fd03 |
| 5 | QA 审查修复（Critical+Required） | `tests/kernel/test_gsb_route.py` + 转换器新用例 | `test_public_primitives_registry_sync` head 红 | 原语登记 + ERROR 跨阈值节流 + 策略跳过/不可路由不计数 | ✅ 本轮 |
| 6 | 文档 + PR | — | — | `docs/issue-3731/{spec,plan,task}.md`、自审查、建 PR、A2A QA | ✅ |
| 7 | 部署缺口：新增列只登记在冻结老轨 | 已盖章存量库（legacy-stamp 202610300001）跑迁移 | 存量库 `PRAGMA table_info` 无列 / worker 心跳 1054 | `convert_fail_count` → 注册式迁移 202611240004；4 个 `acp_state_db_*` → 202611240005（均 `kind=safe` + `post_check`） | ✅ 本轮 |
| 8 | lint 红：F821 `StateDbCheckResult` 未 import | `scripts/check_undefined_names.py` | 当前 1 / 基线 1（棘轮只许降） | `TYPE_CHECKING` 前向引用 + 基线收紧到 0 | ✅ 本轮 |

## 验证记录（本地实跑）

| 命令 | 结果 |
|------|------|
| `python3 -m pytest tests/runtime/test_feedback_issue_converter.py -q` | 22 passed |
| `python3 -m pytest tests/kernel/test_gsb_route.py -q` | 26 passed（QA Critical#1 修复：`feedback_bump_convert_fail` 已登记进 `PUBLIC_PRIMITIVES["mgmt"]`） |
| `python3 -m pytest tests/mgmt/test_s87_true_dichotomy_closure.py::test_s87_no_platform_comparison_feedback_converter -q` | 1 passed（平台字面量已用 `translate_query_state` 收口） |
| `python3 -m pytest tests/runtime/test_issue_official_issue_switch.py tests/runtime/test_redact_issue_body.py -q` | 通过 |
| `python3 -c "import src.api.main"` | import OK |
| `python3 scripts/check_async_blocking.py` | 273 / 基线 273，未新增 |
| 增量敏感扫描（本次改动 diff，七类模式） | 0 命中 |
| `python3 -m pytest tests/arch/ tests/api/test_table_ownership.py tests/api/test_db_registry.py -q` | 76 passed / 2 failed（静默 except ratchet **基线红**） |

### 基线红（与本改动无关，未在本次修复）

1. `tests/arch/test_silent_except_ratchet.py`：`domains/mgmt/public/__init__.py` 基线=3 / 当前=4、
   `kernel/mysql_compat.py` 基线=7 / 当前=9、`kernel/db_registry.py` 新增 1 —— 三处均逐字比对
   `origin/arch-weekly-11-ljt` 同文件内容确认**基线分支自身即如此**（本 PR 未新增 `except...: pass/continue`）。
2. `tests/mgmt/test_s87_true_dichotomy_closure.py`（token_resolver 路径不存在 / 平台比较字面量）、
   `tests/mgmt/test_repo_key_candidates_and_links.py::test_d16_guard_no_dual_prefix_candidate_literals`
   （`runtime/routes/runtime.py`、`engine/engine_select.py`、`mgmt/routes/plugins.py` 双前缀字面量残留）
   —— 失败点位于本 PR 未改动的文件（存量基线红，未在本次修复）。
   ⚠️ 修正先前表述：s87 有一条失败 **在** 本 PR 改动文件内——
   `test_s87_no_platform_comparison_feedback_converter`（本文件 `_find_existing_open_issue`
   遗留 `provider == "github"` 平台字面量，存量、非本次引入）。本轮已顺手用平台卡方言
   `translate_query_state(provider, "open")` 收口，该守卫转绿；其余 s87/d16 红仍在存量文件。

## 验证记录（部署缺口修复轮，本地实跑）

| 命令 | 结果 |
|------|------|
| `python3 -m pytest tests/api/test_issue3731_convert_fail_count_migration.py -q` | 5 passed |
| `python3 -m pytest tests/api/test_issue3773_acp_metrics_columns_migration.py -q` | 5 passed |
| `python3 -m pytest tests/api/test_migration_track_guard.py tests/api/test_migration_domain_inference.py tests/api/test_migration_domain_ownership.py tests/api/test_issue995_vcs_migration.py tests/api/test_issue_3648_migration_reconcile.py -q` | 45 passed |
| `python3 -m pytest tests/runtime/test_feedback_issue_converter.py tests/runtime/test_issue_3773_acp_metrics.py tests/api/test_issue_536_migrations.py -q` | 65 passed |
| `python3 scripts/check_undefined_names.py` | 当前 0 / 基线 0（收敛 1 处，`--update` 收紧） |
| `python3 scripts/check_duplicate_defs.py` / `check_f811.py` / `check_async_blocking.py` | 0 / 0、0 / 72、251 / 273，均未新增 |
| `python3 -m compileall`（6 个改动文件） | 0 错 |

## 附：部署时序提示

`convert_fail_count` 为新增列，走**注册式迁移 202611240004**（`_ensure_column` 幂等补列）。
若 api 进程先于 mgmt（owner）进程升级，**先失败的是读侧**：`feedback_pending_conversions`
的 SELECT 已含第 16 列 `convert_fail_count`，api 进程首轮 reconcile 在 `_fetch_pending`
就抛「no such column」，整轮转换中断（每 300s 一条 traceback），直到 mgmt 进程补列完成；
`feedback_bump_convert_fail` 本身 try/except 静默降级，不额外报错。不丢数据（占位 600s 后
回收重试，查重与建单在列补齐后照常）。建议同批发布或 mgmt 先起。