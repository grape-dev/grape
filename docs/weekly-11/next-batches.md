# 下一批：跨域收口作业单

总量 350 处 / 86 个文件；下表取前 3 个。

## src/domains/runtime/engine/engine_select.py（runtime 域，18 处）
- `grape_accounts`（归 mgmt）× 5：行 292, 340, 360, 365, 720
  - mgmt public 已登记 35 条具名查询
- `autopilot_tasks`（归 mgmt）× 4：行 931, 1412, 1442, 1446
  - mgmt public 已登记 35 条具名查询
- `user_repos`（归 mgmt）× 3：行 364, 382, 719
  - mgmt public 已登记 35 条具名查询
- `devops_pipeline_scene`（归 mgmt）× 3：行 850, 904, 1341
  - mgmt public 已登记 35 条具名查询
- `external_identities`（归 mgmt）× 2：行 342, 367
  - mgmt public 已登记 35 条具名查询
- `unified_user_mapping`（归 mgmt）× 1：行 325
  - mgmt public 已登记 35 条具名查询

## src/kernel/prompt_builder.py（kernel 域，11 处）
- `unified_user_mapping`（归 mgmt）× 3：行 643, 649, 652
  - mgmt public 已登记 35 条具名查询
- `agent_runtimes`（归 runtime）× 2：行 88, 1187
  - runtime public 已登记 151 条具名查询
- `scene_knowledge_ref`（归 mgmt）× 2：行 365, 1391
  - mgmt public 已登记 35 条具名查询
- `agent_shared_knowledge`（归 mgmt）× 2：行 366, 1392
  - mgmt public 已登记 35 条具名查询
- `runtime_credentials`（归 runtime）× 1：行 96
  - runtime public 已登记 151 条具名查询
- `managed_repos`（归 mgmt）× 1：行 411
  - mgmt public 已登记 35 条具名查询

## src/domains/runtime/storage/scene_sync.py（runtime 域，10 处）
- `scene_market`（归 mgmt）× 8：行 426, 461, 466, 525, 750, 769, 788, 830
  - mgmt public 已登记 35 条具名查询
- `scene_skill_ref`（归 mgmt）× 2：行 333, 381
  - mgmt public 已登记 35 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
