# 下一批：跨域收口作业单

总量 214 处 / 72 个文件；下表取前 3 个。

## src/kernel/token_resolver.py（kernel 域，6 处）
- `managed_repos`（归 mgmt）× 5：行 795, 831, 851, 868, 1275
  - mgmt public 已登记 102 条具名查询
- `external_identities`（归 mgmt）× 1：行 978
  - mgmt public 已登记 102 条具名查询

## src/kernel/outbound_ledger.py（kernel 域，6 处）
- `outbound_content_ledger`（归 mgmt）× 6：行 137, 166, 224, 234, 240, 249
  - mgmt public 已登记 102 条具名查询

## src/domains/runtime/routes/webhook.py（runtime 域，6 处）
- `managed_repos`（归 mgmt）× 4：行 90, 841, 1687, 1803
  - mgmt public 已登记 102 条具名查询
- `devops_pipeline_scene`（归 mgmt）× 2：行 913, 1029
  - mgmt public 已登记 102 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
