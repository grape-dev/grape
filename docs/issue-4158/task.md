# Issue #4158 任务拆解（Task）

按 TDD 红/绿分提交执行：先让用例对着当前行为变红（证明契约），再最小实现转绿。
PR #2077 复审的两项 Required 以「切片 8/9」续做。

| # | 切片 | 红（测试先行） | 绿（实现） | 状态 |
|---|------|---------------|-----------|------|
| 1 | `issues_center.py` 22 处去重 | `test_no_duplicate_top_level_defs[issues_center.py]` | 保留 A4 扇出版 + `_uid_ok` 版解析函数 | ✅ |
| 2 | 其余 4 文件去重 | 同上（`runtime/public`、`mgmt/public`、`routes/issues.py`、`db_setup.py`） | 逐符号择一保留 | ✅ |
| 3 | `skills.py` 1 处去重 | 同上（`skills.py`） | `_validate_repo_param` 单份 | ✅ |
| 4 | 顶层同名门禁 | 手动构造重名临时目录 → 脚本 rc=1 | `scripts/check_duplicate_defs.py`（基线 0，显式 FAIL） | ✅ |
| 5 | 三族 `ctx=None` 扇出 | `test_task_notify_fans_out_without_ctx` / `..._awaiting_...` / `test_pending_review_fans_out_without_ctx` | 移除 `int(user_id)` 短路，统一走 `notify_user` | ✅ |
| 6 | UUID 目标解析 | `test_resolve_issue_notify_targets_keeps_uuid` | `_uid_ok` 版 `_resolve_issue_notify_targets` | ✅ |
| 7 | 门禁接 `make lint` | — | `Makefile` lint 目标 + `scripts/check-all.sh` | ✅ |
| 8 | 任务族 no_ctx 口径（复审 Required） | `test_no_channel_counts_skipped_no_ctx_for_all_families[task]` → 红（缺 `skipped_no_ctx`） | `_run_notify` else 分支补 `skipped_no_ctx` 判定 | ✅ |
| 9 | 三族 else 口径对照（复审 Optional） | `test_channel_failure_is_not_counted_as_no_channel`（三族参数化） | 与 8 同一实现（口径即实现） | ✅ |
| 10 | F811 棘轮 + CI 入口（复审 Optional） | `check_f811.py` 基线 68 → rc=1（负向验证） | `scripts/check_f811.py` + `configs/f811_baseline.json` + `.github/workflows/lint.yml` | ✅ |
| 11 | 经验沉淀（复审 Optional） | — | `docs/issue-4158/{spec,plan,task}.md` | ✅ |
| 12 | 缺 ruff 假绿收紧（PR #2077 合入后 DevOps 复核） | `test_rc1_empty_stdout_is_not_zero_hits` / `test_missing_ruff_fails_loud` → 旧实现 rc=0 假绿 | `check_f811._ruff_available()` 前置实查 + `rc ∈ (0,1)` 空 stdout/非 JSON 一律 `RuntimeError` | ✅ |

## 验证命令（可复算）

```bash
# 0) 门禁依赖：ruff 缺失时 check_f811.py / make lint 显式 FAIL（不再假绿）
#    pip install -e ".[dev]"   # 或 pip install "ruff>=0.6,<0.18"

# 1) 静态门禁
python3 scripts/check_duplicate_defs.py src      # 顶层重名 0 / 基线 0
python3 scripts/check_f811.py                    # F811 72 / 基线 72（缺 ruff → rc=1 + 修复指引）
make lint                                        # 含上述两道门禁 + F821 + 异步阻塞棘轮

# 2) 本单回归（仓库实际存在的套件；路径含子目录 im/ 与 api/）
python3 -m pytest -q \
  tests/runtime/test_issue_4158_notify_shadowing.py \
  tests/runtime/im/test_im_notify_fanout.py \
  tests/runtime/test_issue_3510_im_uuid_notify.py \
  tests/api/test_runtime_public_lib.py

# 3) F811 门禁口径（缺工具假绿收紧）回归
python3 -m pytest -q tests/scripts/test_check_f811_gate.py
```

## 不做

- 全仓 `ruff F811` 72 处存量清理（import/局部别名重定义，Spec 划为存量分批）；
- `tests/test_issue_2419_review_point_notify.py` 的 3 个基线既有失败（基线 worktree 可复现，与本单无关）；
- `tests/runtime/test_issue_pending_review.py` 的 `datatype mismatch`（基线迁移/环境问题）。