# 下一批：跨域收口作业单

总量 324 处 / 85 个文件；下表取前 3 个。

## src/domains/runtime/storage/scene_sync.py（runtime 域，10 处）
- `scene_market`（归 mgmt）× 8：行 426, 461, 466, 525, 750, 769, 788, 830
  - mgmt public 已登记 49 条具名查询
- `scene_skill_ref`（归 mgmt）× 2：行 333, 381
  - mgmt public 已登记 49 条具名查询

## src/domains/runtime/pipeline/event_pipeline.py（runtime 域，10 处）
- `devops_pipeline_scene`（归 mgmt）× 6：行 359, 420, 1480, 3866, 4478, 5395
  - mgmt public 已登记 49 条具名查询
- `managed_repos`（归 mgmt）× 4：行 885, 2938, 4413, 5418
  - mgmt public 已登记 49 条具名查询

## src/api/patrol_compensate.py（api 域，10 处）
- `grape_t_patrol_findings`（归 mgmt）× 7：行 86, 98, 103, 134, 553, 559, 582
  - mgmt public 已登记 49 条具名查询
- `agent_tasks`（归 runtime）× 1：行 151
  - runtime public 已登记 153 条具名查询
- `managed_repos`（归 mgmt）× 1：行 233
  - mgmt public 已登记 49 条具名查询
- `user_repos`（归 mgmt）× 1：行 245
  - mgmt public 已登记 49 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
