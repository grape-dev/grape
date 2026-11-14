# 下一批：跨域收口作业单

总量 294 处 / 82 个文件；下表取前 3 个。

## src/kernel/lock.py（kernel 域，9 处）
- `distributed_locks`（归 runtime）× 9：行 34, 44, 50, 61, 67, 84, 103, 108, 123
  - runtime public 已登记 154 条具名查询

## src/kernel/gsb/registry.py（kernel 域，9 处）
- `gsb_service_registry`（归 mgmt）× 9：行 49, 87, 101, 118, 142, 182, 218, 240, 255
  - mgmt public 已登记 70 条具名查询

## src/domains/runtime/im/channel_directory.py（runtime 域，9 处）
- `unified_user_mapping`（归 mgmt）× 9：行 73, 75, 106, 144, 176, 209, 237, 273, 274
  - mgmt public 已登记 70 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
