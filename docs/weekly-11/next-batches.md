# 下一批：跨域收口作业单

总量 96 处 / 49 个文件；下表取前 3 个。

## src/api/session_store.py（api 域，4 处）
- `devops_pipeline_scene`（归 mgmt）× 3：行 486, 698, 745
  - mgmt public 已登记 144 条具名查询
- `issue`（归 runtime）× 1：行 611
  - runtime public 已登记 213 条具名查询

## src/api/health_endpoints.py（api 域，4 处）
- `agent_tasks`（归 runtime）× 3：行 91, 143, 146
  - runtime public 已登记 213 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 149
  - runtime public 已登记 213 条具名查询

## src/api/deps.py（api 域，4 处）
- `external_identities`（归 mgmt）× 4：行 374, 451, 491, 608
  - mgmt public 已登记 144 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
