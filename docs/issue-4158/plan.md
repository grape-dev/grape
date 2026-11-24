# Issue #4158 方案设计（Plan）：逐函数「择一保留」+ 静态门禁

## 1. 判定原则

同一模块出现两份同名顶层实现时，**不能按位置（前/后）删**，也不能按「看起来新」删——必须
逐符号对齐行为后择一保留：

1. 与 Spec / 事故形态一致的那份（A4 `notify_user` 扇出、UUID `_uid_ok` 判定）优先；
2. 两份逐字相同 → 任意保留一份；
3. 两份有差异且都非目标形态 → 保留**当前实际生效**（后定义）那份的语义，另外把前一份里
   独有的有效改动（如 `include_payload=True`）合入，避免顺手回退别人已合入的修复。

## 2. 「择一保留」判定表

| 文件 | 重名数 | 保留 | 删除 | 依据 |
|------|-------|------|------|------|
| `src/domains/runtime/issues_center.py` | 22 | A4 扇出版 `_run_notify` / `_run_awaiting_notify` / `_run_pending_review_notify`；`_uid_ok` 版 `_resolve_issue_notify_targets`；installation 版 `_resolve_user_chat_bot` | 靠后的飞书专用副本、`int(user_id)` 短路版 | 事故形态 |
| `src/domains/runtime/public/__init__.py` | 9 | 现行 `_dash_where` 看板组、较新 `runtime_ids_by_engine_uids` | 旧副本 | 逐字相同/新者生效 |
| `src/domains/mgmt/public/__init__.py` | 5 | `_any_cursor(write=True)` 租约组；`get_github_token = github_token` 别名保留（签名仍收 `repo_key=`） | 旧 `get_github_token`/`get_repo_bot_token` 薄封装 | `_pr_vcs_client` 调用链不变 |
| `src/domains/runtime/routes/issues.py` | 1 | 已注册的 pipeline handler + 合入 `include_payload=True` | 复制粘贴的旧 handler | `check_repo_access` 校验仍在，权限面不变 |
| `src/api/db_setup.py` | 1 | 列类型实查 + `raise` + 不盖章的 org 域迁移 | 旧迁移 | 迁移仅剩一份注册（version=202611190100） |
| `src/domains/mgmt/routes/skills.py` | 1 | `_validate_repo_param` 单份 | 重复副本 | Spec 清单外，但不去重则 `src/` 门禁无法归零 |

## 3. no_ctx 指标口径（三族必须一致）

`_IM_NOTIFY_COUNTERS` 的 else 分支（`notify_user` 未送出）：

| 形态 | `sent_failed` | `skipped_no_ctx` |
|------|--------------|------------------|
| 真无可用渠道（`sent`/`failed`/`skipped` 全空） | ✅ | ✅ |
| 有渠道但发送失败（`failed` 非空） | ✅ | ❌ |
| `notify_user` 抛异常（外层 except） | ❌ | ❌（只记 warning） |

修复前 `_run_notify`（任务分配族）把「无渠道」并入 `sent_failed`，另两族则额外计
`skipped_no_ctx`——同一事故在任务族指标里不可见。本次三族统一（PR #2077 复审 Required）。

## 4. 门禁设计

- `scripts/check_duplicate_defs.py`：AST 扫顶层同名 `def/class`，基线固定 0；
  扫描根缺失/解析失败**显式 FAIL**（不静默跳过）；接入 `make lint` + `scripts/check-all.sh`。
- `scripts/check_f811.py`：`ruff check --select F811` 棘轮，基线
  `configs/f811_baseline.json`（69 处存量：`runtime/routes/chat.py` 9、`mgmt/routes/grape_auth.py` 8、
  `mgmt/user/db.py` 5 等），新增即失败；ruff 缺失显式失败。
- `.github/workflows/lint.yml`：跑 `make lint`（含上述两道门禁）+ 本单回归套件，
  使「门禁接入 CI」可验证（此前仅本地 `make lint`，现有 workflow 都不跑 lint）。
- `pyproject.toml` 的 `ruff` 依赖加 `<0.18` 上限：基线与 ruff 规则版本绑定，升级需
  `--update-baseline` 校准。

## 5. 测试 seam

| 切片 | 公开 seam | 断言 |
|------|-----------|------|
| 顶层重名 | AST（文件级） | 6 文件重名计数 = 0 |
| 三族扇出 | `_run_notify` / `_run_awaiting_notify` / `_run_pending_review_notify`（协程，注入 `notify_user`） | `sent_ok`、无 `skipped_*` |
| else 口径 | `get_im_notify_counters()` | 三族 × {全空, 仅 failed} 参数化断言 |
| UUID 归一 | `_resolve_issue_notify_targets` | UUID 串不被 `int()` 短路 |
| 门禁 | `scripts/check_duplicate_defs.py` / `check_f811.py` 退出码 | 0 通过 / 1 命中 |

## 6. 风险与已知存量

- 通知链路只在 `ctx=None` 分支变化，未触及 `notify_user` 扇出与 outbox 重试；线上表现应为
  `skipped_no_ctx` 下降、`sent_ok` 上升。
- `tests/test_issue_2419_review_point_notify.py` 在基线 `arch-weekly-11-ljt` 上即有 3 个失败
  （`close_issue_tasks` 把 `stage=done` 落成 `pending_review` 等），与本单无关，已用基线
  worktree 复现取证，不纳入「本单回归」计数。
- 全仓 `ruff F811` 69 处存量按 Spec「存量分批」不纳入本 PR，已加棘轮防止继续增长。