# 下一批：跨域收口作业单

总量 221 处 / 73 个文件；下表取前 10 个。

## src/domains/mgmt/routes/autopilot.py（mgmt 域，7 处）
- `autopilot_executions`（归 runtime）× 5：行 448, 505, 506, 596, 703
  - runtime public 已登记 172 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 117
  - runtime public 已登记 172 条具名查询
- `skill`（归 runtime）× 1：行 983
  - runtime public 已登记 172 条具名查询

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

## src/domains/runtime/routes/chat.py（runtime 域，6 处）
- `grape_t_channel_installation`（归 mgmt）× 3：行 1561, 1703, 2792
  - mgmt public 已登记 102 条具名查询
- `unified_user_mapping`（归 mgmt）× 2：行 1600, 1606
  - mgmt public 已登记 102 条具名查询
- `feishu_onboard_task`（归 mgmt）× 1：行 1631
  - mgmt public 已登记 102 条具名查询

## src/domains/runtime/im/handler_async.py（runtime 域，6 处）
- `grape_t_channel_installation`（归 mgmt）× 2：行 347, 359
  - mgmt public 已登记 102 条具名查询
- `devops_pipeline_scene`（归 mgmt）× 2：行 397, 431
  - mgmt public 已登记 102 条具名查询
- `grape_t_channel_sender_identity`（归 mgmt）× 1：行 346
  - mgmt public 已登记 102 条具名查询
- `autopilot_tasks`（归 mgmt）× 1：行 391
  - mgmt public 已登记 102 条具名查询

## src/domains/runtime/engine/scheduler.py（runtime 域，6 处）
- `autopilot_tasks`（归 mgmt）× 5：行 909, 993, 1089, 1629, 1687
  - mgmt public 已登记 102 条具名查询
- `autopilot_subscriptions`（归 mgmt）× 1：行 908
  - mgmt public 已登记 102 条具名查询

## src/domains/mgmt/routes/devstage.py（mgmt 域，6 处）
- `runtime_credentials`（归 runtime）× 3：行 56, 371, 415
  - runtime public 已登记 172 条具名查询
- `agent_runtimes`（归 runtime）× 2：行 385, 414
  - runtime public 已登记 172 条具名查询
- `devops_agent_session`（归 runtime）× 1：行 412
  - runtime public 已登记 172 条具名查询

## src/domains/insight/observability/metric_rollup.py（insight 域，6 处）
- `agent_tasks`（归 runtime）× 4：行 147, 155, 173, 199
  - runtime public 已登记 172 条具名查询
- `events`（归 runtime）× 1：行 188
  - runtime public 已登记 172 条具名查询
- `runtime_credentials`（归 runtime）× 1：行 230
  - runtime public 已登记 172 条具名查询

## src/api/skill_display.py（api 域，6 处）
- `skill`（归 runtime）× 6：行 53, 61, 70, 79, 87, 114
  - runtime public 已登记 172 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
