# Issue #2933 任务拆解

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1 | 导入期日志初始化降级 + 降级标志 | `src/kernel/config.py` | TODO |
| 2 | `WORKSPACE_DIR` / `LOG_DIR` makedirs 收敛 | `src/kernel/config.py` | TODO |
| 3 | shell 层 stdin 暂存 fail-open（含 `--no-verify`） | `src/domains/runtime/engine/branch_guard.py` | TODO |
| 4 | 模板版本递增 `2028-05` + 告警文案统一 | `src/domains/runtime/engine/branch_guard.py` | TODO |
| 5 | 安装失败可观测（日志 + 调用方 stderr） | `branch_guard.py` / `repogit.py` | TODO |
| 6 | 回归用例（只读日志/只读 TMPDIR/只读 hooks/模板升级/fail-closed） | `tests/runtime/test_issue_2933_readonly_log_failopen.py` | TODO |
| 7 | 全量相关用例回归 | `tests/` | TODO |