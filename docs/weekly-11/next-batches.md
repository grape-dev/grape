# 下一批：跨域收口作业单

总量 138 处 / 59 个文件；下表取前 3 个。

## src/api/feedback_status.py（api 域，5 处）
- `user_feedback`（归 mgmt）× 4：行 152, 174, 204, 217
  - mgmt public 已登记 131 条具名查询
- `issue`（归 runtime）× 1：行 205
  - runtime public 已登记 200 条具名查询

## src/api/a2a_dispatch_guard.py（api 域，5 处）
- `events`（归 runtime）× 4：行 132, 241, 293, 356
  - runtime public 已登记 200 条具名查询
- `event_skipped`（归 runtime）× 1：行 362
  - runtime public 已登记 200 条具名查询

## src/kernel/config.py（kernel 域，4 处）
- `login_whitelist`（归 mgmt）× 4：行 662, 730, 738, 746
  - mgmt public 已登记 131 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
