# 下一批：跨域收口作业单

总量 108 处 / 52 个文件；下表取前 3 个。

## src/domains/mgmt/admin/cooldown.py（mgmt 域，4 处）
- `agent_tasks`（归 runtime）× 1：行 135
  - runtime public 已登记 206 条具名查询
- `chat_agent_session`（归 runtime）× 1：行 136
  - runtime public 已登记 206 条具名查询
- `chat_conversations`（归 runtime）× 1：行 137
  - runtime public 已登记 206 条具名查询
- `issue`（归 runtime）× 1：行 138
  - runtime public 已登记 206 条具名查询

## src/domains/insight/observability/failed_gap.py（insight 域，4 处）
- `events`（归 runtime）× 4：行 57, 68, 80, 96
  - runtime public 已登记 206 条具名查询

## src/domains/insight/observability/__init__.py（insight 域，4 处）
- `agent_tasks`（归 runtime）× 3：行 618, 630, 642
  - runtime public 已登记 206 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 656
  - runtime public 已登记 206 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
