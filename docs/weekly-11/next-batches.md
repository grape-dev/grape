# 下一批：跨域收口作业单

总量 172 处 / 65 个文件；下表取前 3 个。

## src/domains/insight/observability/metric_rollup.py（insight 域，6 处）
- `agent_tasks`（归 runtime）× 4：行 147, 155, 173, 199
  - runtime public 已登记 179 条具名查询
- `events`（归 runtime）× 1：行 188
  - runtime public 已登记 179 条具名查询
- `runtime_credentials`（归 runtime）× 1：行 230
  - runtime public 已登记 179 条具名查询

## src/api/skill_display.py（api 域，6 处）
- `skill`（归 runtime）× 6：行 53, 61, 70, 79, 87, 114
  - runtime public 已登记 179 条具名查询

## src/api/scene_binding_repair.py（api 域，6 处）
- `managed_repos`（归 mgmt）× 2：行 76, 85
  - mgmt public 已登记 127 条具名查询
- `issue`（归 runtime）× 2：行 99, 108
  - runtime public 已登记 179 条具名查询
- `scene_market`（归 mgmt）× 1：行 34
  - mgmt public 已登记 127 条具名查询
- `devops_pipeline_scene`（归 mgmt）× 1：行 48
  - mgmt public 已登记 127 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
