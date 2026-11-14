# 下一批：跨域收口作业单

总量 138 处 / 59 个文件；下表取前 10 个。

## src/api/feedback_status.py（api 域，5 处）
- `user_feedback`（归 mgmt）× 4：行 152, 174, 204, 217
  - mgmt public 已登记 131 条具名查询
- `issue`（归 runtime）× 1：行 205
  - runtime public 已登记 200 条具名查询

## src/api/a2a_dispatch_guard.py（api 域，5 处）
- `events`（归 runtime）× 4：行 132, 241, 293, 356
  - runtime public 已登记 200 条具名查询
- `event_skipped`（归 runtime）× 1：行 362
  - runtime public 已登记 200 条具名查询

## src/kernel/config.py（kernel 域，4 处）
- `login_whitelist`（归 mgmt）× 4：行 662, 730, 738, 746
  - mgmt public 已登记 131 条具名查询

## src/domains/runtime/project_info/builder.py（runtime 域，4 处）
- `agent_shared_knowledge`（归 mgmt）× 3：行 315, 324, 335
  - mgmt public 已登记 131 条具名查询
- `scene_knowledge_ref`（归 mgmt）× 1：行 347
  - mgmt public 已登记 131 条具名查询

## src/domains/runtime/engine/service.py（runtime 域，4 处）
- `grape_accounts`（归 mgmt）× 2：行 111, 1710
  - mgmt public 已登记 131 条具名查询
- `agent_shared_knowledge`（归 mgmt）× 2：行 604, 691
  - mgmt public 已登记 131 条具名查询

## src/domains/mgmt/user/legacy_column_migrations.py（mgmt 域，4 处）
- `meta`（归 runtime）× 4：行 166, 168, 175, 177
  - runtime public 已登记 200 条具名查询

## src/domains/mgmt/routes/feishu_onboard.py（mgmt 域，4 处）
- `devops_agent_session`（归 runtime）× 2：行 205, 238
  - runtime public 已登记 200 条具名查询
- `chat_conversations`（归 runtime）× 1：行 157
  - runtime public 已登记 200 条具名查询
- `vod_conversations`（归 runtime）× 1：行 222
  - runtime public 已登记 200 条具名查询

## src/domains/mgmt/admin/cooldown.py（mgmt 域，4 处）
- `agent_tasks`（归 runtime）× 1：行 135
  - runtime public 已登记 200 条具名查询
- `chat_agent_session`（归 runtime）× 1：行 136
  - runtime public 已登记 200 条具名查询
- `chat_conversations`（归 runtime）× 1：行 137
  - runtime public 已登记 200 条具名查询
- `issue`（归 runtime）× 1：行 138
  - runtime public 已登记 200 条具名查询

## src/domains/insight/observability/failed_gap.py（insight 域，4 处）
- `events`（归 runtime）× 4：行 57, 68, 80, 96
  - runtime public 已登记 200 条具名查询

## src/domains/insight/observability/__init__.py（insight 域，4 处）
- `agent_tasks`（归 runtime）× 3：行 618, 630, 642
  - runtime public 已登记 200 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 656
  - runtime public 已登记 200 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
