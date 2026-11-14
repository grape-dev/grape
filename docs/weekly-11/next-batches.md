# 下一批：跨域收口作业单

总量 597 处 / 99 个文件；下表取前 3 个。

## src/api/db_setup.py（api 域，33 处）
- `managed_repos`（归 mgmt）× 4：行 578, 582, 2430, 2580
  - mgmt public 已登记 35 条具名查询
- `agent_runtimes`（归 runtime）× 4：行 3030, 4187, 4204, 5270
  - runtime public 已登记 123 条具名查询
- `events`（归 runtime）× 4：行 3313, 3322, 3386, 3395
  - runtime public 已登记 123 条具名查询
- `scene_market`（归 mgmt）× 3：行 6393, 6579, 6617
  - mgmt public 已登记 35 条具名查询
- `grape_t_schema_migrations`（归 mgmt）× 2：行 2349, 3940
  - mgmt public 已登记 35 条具名查询
- `issue`（归 runtime）× 2：行 4097, 4282
  - runtime public 已登记 123 条具名查询
- `scene_knowledge_ref`（归 mgmt）× 2：行 4835, 4841
  - mgmt public 已登记 35 条具名查询
- `agent_shared_knowledge`（归 mgmt）× 2：行 4836, 4842
  - mgmt public 已登记 35 条具名查询
- `skill`（归 runtime）× 1：行 1547
  - runtime public 已登记 123 条具名查询
- `skill_category`（归 mgmt）× 1：行 1548
  - mgmt public 已登记 35 条具名查询
- `organization`（归 mgmt）× 1：行 2095
  - mgmt public 已登记 35 条具名查询
- `login_whitelist`（归 mgmt）× 1：行 2316
  - mgmt public 已登记 35 条具名查询
- `unified_user_mapping`（归 mgmt）× 1：行 2865
  - mgmt public 已登记 35 条具名查询
- `takeover_sessions`（归 runtime）× 1：行 3492
  - runtime public 已登记 123 条具名查询
- `api_latency_log`（归 insight）× 1：行 5137
  - insight public 无具名查询，需新增
- `chat_agent_session`（归 runtime）× 1：行 5235
  - runtime public 已登记 123 条具名查询
- `chat_conversations`（归 runtime）× 1：行 6671
  - runtime public 已登记 123 条具名查询
- `external_identities`（归 mgmt）× 1：行 6672
  - mgmt public 已登记 35 条具名查询

## src/domains/runtime/public/__init__.py（runtime 域，28 处）
- `grape_t_repo_credentials`（归 insight）× 10：行 218, 220, 850, 856, 860, 868, 1208, 1209, 1214, 1216
  - insight public 无具名查询，需新增
- `grape_t_channel_lease`（归 mgmt）× 5：行 432, 441, 460, 525, 530
  - mgmt public 已登记 35 条具名查询
- `event_action_filter`（归 mgmt）× 4：行 691, 712, 722, 1206
  - mgmt public 已登记 35 条具名查询
- `grape_accounts`（归 mgmt）× 3：行 1223, 1872, 1873
  - mgmt public 已登记 35 条具名查询
- `autopilot_tasks`（归 mgmt）× 2：行 1201, 1203
  - mgmt public 已登记 35 条具名查询
- `external_identities`（归 mgmt）× 2：行 1225, 1229
  - mgmt public 已登记 35 条具名查询
- `unified_user_mapping`（归 mgmt）× 2：行 1872, 1873
  - mgmt public 已登记 35 条具名查询

## src/api/patrol.py（api 域，25 处）
- `agent_tasks`（归 runtime）× 9：行 13, 525, 676, 685, 848, 876, 882, 1061, 1065
  - runtime public 已登记 123 条具名查询
- `meta`（归 runtime）× 4：行 322, 328, 335, 343
  - runtime public 已登记 123 条具名查询
- `grape_t_patrol_findings`（归 mgmt）× 4：行 759, 796, 821, 1171
  - mgmt public 已登记 35 条具名查询
- `events`（归 runtime）× 3：行 490, 537, 648
  - runtime public 已登记 123 条具名查询
- `issue`（归 runtime）× 2：行 437, 456
  - runtime public 已登记 123 条具名查询
- `managed_repos`（归 mgmt）× 1：行 729
  - mgmt public 已登记 35 条具名查询
- `user_repos`（归 mgmt）× 1：行 739
  - mgmt public 已登记 35 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 997
  - runtime public 已登记 123 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
