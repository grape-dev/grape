# 下一批：跨域收口作业单

总量 84 处 / 46 个文件；下表取前 10 个。

## src/kernel/prompt_builder.py（kernel 域，3 处）
- `unified_user_mapping`（归 mgmt）× 3：行 621, 627, 630
  - mgmt public 已登记 147 条具名查询

## src/kernel/persistence/credential_usage_log.py（kernel 域，3 处）
- `grape_t_credential_usage_log`（归 runtime）× 3：行 179, 185, 261
  - runtime public 已登记 213 条具名查询

## src/domains/runtime/engine/bot_token_health.py（runtime 域，3 处）
- `managed_repos`（归 mgmt）× 2：行 232, 292
  - mgmt public 已登记 147 条具名查询
- `grape_t_repo_credentials`（归 insight）× 1：行 172
  - insight public 无具名查询，需新增

## src/domains/mgmt/user/credentials_backfill_n1.py（mgmt 域，3 处）
- `grape_t_repo_credentials`（归 insight）× 3：行 121, 128, 148
  - insight public 无具名查询，需新增

## src/domains/insight/observability/tokens.py（insight 域，3 处）
- `agent_tasks`（归 runtime）× 2：行 6, 154
  - runtime public 已登记 213 条具名查询
- `runtime_credentials`（归 runtime）× 1：行 279
  - runtime public 已登记 213 条具名查询

## src/domains/insight/observability/llm_usage.py（insight 域，3 处）
- `agent_runtimes`（归 runtime）× 2：行 343, 399
  - runtime public 已登记 213 条具名查询
- `agent_tasks`（归 runtime）× 1：行 119
  - runtime public 已登记 213 条具名查询

## src/domains/insight/observability/gap.py（insight 域，3 处）
- `runtime_credentials`（归 runtime）× 2：行 104, 235
  - runtime public 已登记 213 条具名查询
- `agent_tasks`（归 runtime）× 1：行 167
  - runtime public 已登记 213 条具名查询

## src/api/server_tasks.py（api 域，3 处）
- `runtime_credentials`（归 runtime）× 3：行 632, 635, 705
  - runtime public 已登记 213 条具名查询

## src/api/pipeline_status.py（api 域，3 处）
- `issue`（归 runtime）× 1：行 343
  - runtime public 已登记 213 条具名查询
- `issue_state`（归 runtime）× 1：行 353
  - runtime public 已登记 213 条具名查询
- `agent_tasks`（归 runtime）× 1：行 387
  - runtime public 已登记 213 条具名查询

## src/api/main.py（api 域，3 处）
- `runtime_credentials`（归 runtime）× 3：行 126, 129, 208
  - runtime public 已登记 213 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
