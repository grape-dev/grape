# 下一批：跨域收口作业单

总量 154 处 / 62 个文件；下表取前 3 个。

## src/api/issue_vcs_index.py（api 域，6 处）
- `issue`（归 runtime）× 6：行 273, 317, 321, 325, 433, 451
  - runtime public 已登记 190 条具名查询

## src/domains/insight/observability/efficiency.py（insight 域，5 处）
- `issue`（归 runtime）× 2：行 138, 619
  - runtime public 已登记 190 条具名查询
- `grape_accounts`（归 mgmt）× 1：行 364
  - mgmt public 已登记 131 条具名查询
- `agent_tasks`（归 runtime）× 1：行 451
  - runtime public 已登记 190 条具名查询
- `devops_pipeline_scene`（归 mgmt）× 1：行 634
  - mgmt public 已登记 131 条具名查询

## src/api/server_common.py（api 域，5 处）
- `agent_tasks`（归 runtime）× 3：行 439, 502, 504
  - runtime public 已登记 190 条具名查询
- `api_latency_log`（归 insight）× 1：行 357
  - insight public 无具名查询，需新增
- `agent_runtimes`（归 runtime）× 1：行 506
  - runtime public 已登记 190 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
