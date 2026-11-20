# Task: Issue #2227

- [x] T1 `scripts/check_grape_cli_drift.py`：`GRAPE_CLI_BIN` + `commits --sha` 能力探测
      （PASS / FAIL exit 1 输出版本与缺失项 / SKIP exit 0）
- [x] T2 `scripts/check-all.sh` 接入守卫门禁
- [x] T3 `scripts/install-worker.sh` 第 4.5 节安装后能力校验与告警（记录实际版本）
- [x] T4 `help/api/10-git-repos.md` 版本要求 + 降级核验命令口径校准
- [x] T5 `tests/test_issue_2227_grape_cli_drift.py` 三场景单测 + 接线静态断言