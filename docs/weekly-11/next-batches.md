# 下一批：跨域收口作业单

总量 124 处 / 56 个文件；下表取前 3 个。

## src/domains/runtime/project_info/builder.py（runtime 域，4 处）
- `agent_shared_knowledge`（归 mgmt）× 3：行 315, 324, 335
  - mgmt public 已登记 137 条具名查询
- `scene_knowledge_ref`（归 mgmt）× 1：行 347
  - mgmt public 已登记 137 条具名查询

## src/domains/runtime/engine/service.py（runtime 域，4 处）
- `grape_accounts`（归 mgmt）× 2：行 111, 1710
  - mgmt public 已登记 137 条具名查询
- `agent_shared_knowledge`（归 mgmt）× 2：行 604, 691
  - mgmt public 已登记 137 条具名查询

## src/domains/mgmt/user/legacy_column_migrations.py（mgmt 域，4 处）
- `meta`（归 runtime）× 4：行 166, 168, 175, 177
  - runtime public 已登记 203 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
