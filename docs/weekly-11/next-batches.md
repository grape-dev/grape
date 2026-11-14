# 下一批：跨域收口作业单

总量 267 处 / 79 个文件；下表取前 10 个。

## src/domains/insight/observability/trace.py（insight 域，9 处）
- `agent_tasks`（归 runtime）× 5：行 80, 107, 245, 255, 262
  - runtime public 已登记 161 条具名查询
- `task_messages`（归 runtime）× 1：行 129
  - runtime public 已登记 161 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 184
  - runtime public 已登记 161 条具名查询
- `runtime_credentials`（归 runtime）× 1：行 184
  - runtime public 已登记 161 条具名查询
- `events`（归 runtime）× 1：行 322
  - runtime public 已登记 161 条具名查询

## src/domains/mgmt/routes/feedback.py（mgmt 域，8 处）
- `agent_tasks`（归 runtime）× 2：行 167, 278
  - runtime public 已登记 161 条具名查询
- `skill`（归 runtime）× 1：行 85
  - runtime public 已登记 161 条具名查询
- `events`（归 runtime）× 1：行 117
  - runtime public 已登记 161 条具名查询
- `issue`（归 runtime）× 1：行 144
  - runtime public 已登记 161 条具名查询
- `devops_agent_session`（归 runtime）× 1：行 187
  - runtime public 已登记 161 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 207
  - runtime public 已登记 161 条具名查询
- `runtime_credentials`（归 runtime）× 1：行 207
  - runtime public 已登记 161 条具名查询

## src/domains/mgmt/admin/routes.py（mgmt 域，8 处）
- `meta`（归 runtime）× 7：行 961, 975, 987, 1066, 1074, 1078, 1081
  - runtime public 已登记 161 条具名查询
- `obs_metric_rollup`（归 insight）× 1：行 751
  - insight public 无具名查询，需新增

## src/domains/runtime/routes/im.py（runtime 域，7 处）
- `grape_t_channel_installation`（归 mgmt）× 7：行 118, 159, 234, 246, 271, 363, 372
  - mgmt public 已登记 87 条具名查询

## src/domains/runtime/routes/events.py（runtime 域，7 处）
- `grape_accounts`（归 mgmt）× 3：行 875, 1009, 1224
  - mgmt public 已登记 87 条具名查询
- `agent_shared_knowledge`（归 mgmt）× 3：行 2035, 2071, 2077
  - mgmt public 已登记 87 条具名查询
- `scene_knowledge_ref`（归 mgmt）× 1：行 2075
  - mgmt public 已登记 87 条具名查询

## src/domains/runtime/issues_center.py（runtime 域，7 处）
- `unified_user_mapping`（归 mgmt）× 3：行 111, 117, 195
  - mgmt public 已登记 87 条具名查询
- `grape_accounts`（归 mgmt）× 2：行 113, 229
  - mgmt public 已登记 87 条具名查询
- `managed_repos`（归 mgmt）× 2：行 792, 3282
  - mgmt public 已登记 87 条具名查询

## src/domains/mgmt/routes/autopilot.py（mgmt 域，7 处）
- `autopilot_executions`（归 runtime）× 5：行 448, 505, 506, 596, 703
  - runtime public 已登记 161 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 117
  - runtime public 已登记 161 条具名查询
- `skill`（归 runtime）× 1：行 983
  - runtime public 已登记 161 条具名查询

## src/kernel/token_resolver.py（kernel 域，6 处）
- `managed_repos`（归 mgmt）× 5：行 795, 831, 851, 868, 1275
  - mgmt public 已登记 87 条具名查询
- `external_identities`（归 mgmt）× 1：行 978
  - mgmt public 已登记 87 条具名查询

## src/kernel/outbound_ledger.py（kernel 域，6 处）
- `outbound_content_ledger`（归 mgmt）× 6：行 137, 166, 224, 234, 240, 249
  - mgmt public 已登记 87 条具名查询

## src/domains/runtime/routes/webhook.py（runtime 域，6 处）
- `managed_repos`（归 mgmt）× 4：行 90, 841, 1687, 1803
  - mgmt public 已登记 87 条具名查询
- `devops_pipeline_scene`（归 mgmt）× 2：行 913, 1029
  - mgmt public 已登记 87 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
