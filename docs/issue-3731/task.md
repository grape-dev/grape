# Issue #3731 任务拆解

> 目标合入分支：`arch-weekly-11-ljt` | 开发分支：`feat/issue-3731`

| # | 切片 | 测试 seam | 红（失败测试） | 绿（最小实现） | 状态 |
|---|------|-----------|----------------|----------------|------|
| 1 | 路由解包收敛到三元组 | `_repo_target()` / `_repo_bot_token()` 返回值 | `test_repo_target_three_tuple_unpack`（旧实现实抛 `ValueError: too many values to unpack (expected 2)`） | `_repo_target` / `_repo_bot_token` 改 3 元解包 | ✅ 4b4e2af72 红 / d9ea1a148 绿 |
| 2 | 标题截断 + 查重同键 | `_create_provider_issue()` payload / `_find_existing_open_issue()` 查重键 | `test_long_title_clipped_before_create`、`test_clipped_title_reuse_existing_issue` | `_MAX_TITLE_LEN` + `_clip_title()` 接入两处 | ✅ 08745dcee 红 / 20d2ef5de 绿 |
| 3 | 失败轮次持久化 + ERROR 升级 | `convert_pending_feedback()` 返回值 + `user_feedback.convert_fail_count` | 三条用例（缺列即 `no such column`） | `convert_fail_count` 列 + `feedback_bump_convert_fail` + `_note_failure` | ✅ f8349136e 红 / af78fa892 绿 |
| 4 | 历史用例对齐 #673 语义 | `tests/runtime/test_feedback_issue_converter.py` | 基线已红（user+repo_key 现应转换） | 用例改为「带 repo_key 的 user 反馈转换、纯意见类不转换」 | ✅ 86376fd03 |
| 5 | 文档 + PR | — | — | `docs/issue-3731/{spec,plan,task}.md`、自审查、建 PR、A2A QA | ✅ |

## 验证记录（本地实跑）

| 命令 | 结果 |
|------|------|
| `python3 -m pytest tests/runtime/test_feedback_issue_converter.py -q` | 18 passed |
| `python3 -m pytest tests/runtime/test_issue_official_issue_switch.py tests/runtime/test_redact_issue_body.py -q` | 通过 |
| `python3 -c "import src.api.main"` | import OK |
| `python3 scripts/check_async_blocking.py` | 273 / 基线 273，未新增 |
| 增量敏感扫描（456 新增行，七类模式） | 0 命中 |
| `python3 -m pytest tests/arch/ tests/api/test_table_ownership.py tests/api/test_db_registry.py -q` | 76 passed / 2 failed（静默 except ratchet **基线红**） |

### 基线红（与本改动无关，未在本次修复）

1. `tests/arch/test_silent_except_ratchet.py`：`domains/mgmt/public/__init__.py` 基线=3 / 当前=4、
   `kernel/mysql_compat.py` 基线=7 / 当前=9、`kernel/db_registry.py` 新增 1 —— 三处均逐字比对
   `origin/arch-weekly-11-ljt` 同文件内容确认**基线分支自身即如此**（本 PR 未新增 `except...: pass/continue`）。
2. `tests/mgmt/test_s87_true_dichotomy_closure.py`（token_resolver 路径不存在 / 平台比较字面量）、
   `tests/mgmt/test_repo_key_candidates_and_links.py::test_d16_guard_no_dual_prefix_candidate_literals`
   （`runtime/routes/runtime.py`、`engine/engine_select.py`、`mgmt/routes/plugins.py` 双前缀字面量残留）
   —— 失败点全部位于本 PR 未改动的文件。

## 附：部署时序提示

`convert_fail_count` 为新增列（`db_setup` 启动时按 `ALTERS_USER_FEEDBACK_COLUMNS` 幂等补列）。
若 api 进程先于 mgmt（owner）进程升级，首轮 reconcile 会在 `mark_converted` 回写时遇到未知列并失败；
列补齐后下一轮自动恢复，不丢数据（占位 600s 后回收重试）。建议同批发布或 mgmt 先起。