# 下一批：跨域收口作业单

总量 732 处 / 102 个文件；下表取前 3 个。

## src/kernel/session_manager.py（kernel 域，56 处）
- `chat_agent_session`（归 runtime）× 19：行 142, 145, 182, 188, 192, 227, 231, 235, 239, 253, 263, 266 …
  - runtime public 已登记 106 条具名查询
- `chat_conversations`（归 runtime）× 13：行 559, 597, 599, 606, 620, 622, 645, 652, 667, 692, 701, 703 …
  - runtime public 已登记 106 条具名查询
- `repo_im_group_binding`（归 runtime）× 11：行 201, 205, 209, 213, 217, 221, 484, 495, 505, 517, 532
  - runtime public 已登记 106 条具名查询
- `chat_messages`（归 runtime）× 9：行 714, 717, 736, 745, 756, 768, 778, 781, 791
  - runtime public 已登记 106 条具名查询
- `issue`（归 runtime）× 2：行 673, 677
  - runtime public 已登记 106 条具名查询
- `grape_accounts`（归 mgmt）× 1：行 429
  - mgmt public 已登记 35 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 865
  - runtime public 已登记 106 条具名查询

## src/domains/mgmt/user/db.py（mgmt 域，48 处）
- `agent_runtimes`（归 runtime）× 15：行 3969, 3990, 4067, 4078, 4095, 4134, 4141, 4142, 4176, 4178, 4225, 4247 …
  - runtime public 已登记 106 条具名查询
- `skill`（归 runtime）× 10：行 1522, 1525, 1546, 4911, 4919, 4926, 5080, 5097, 5112, 5224
  - runtime public 已登记 106 条具名查询
- `grape_t_repo_credentials`（归 insight）× 10：行 2086, 2199, 2809, 5269, 5275, 5282, 5361, 5559, 5575, 5610
  - insight public 无具名查询，需新增
- `meta`（归 runtime）× 6：行 982, 984, 992, 994, 3551, 3553
  - runtime public 已登记 106 条具名查询
- `engine_capabilities`（归 runtime）× 2：行 4119, 4123
  - runtime public 已登记 106 条具名查询
- `runtime_credentials`（归 runtime）× 2：行 4248, 4318
  - runtime public 已登记 106 条具名查询
- `scene_category`（归 runtime）× 2：行 4402, 4420
  - runtime public 已登记 106 条具名查询
- `devops_agent_session`（归 runtime）× 1：行 2664
  - runtime public 已登记 106 条具名查询

## src/api/db_setup.py（api 域，33 处）
- `managed_repos`（归 mgmt）× 4：行 578, 582, 2430, 2580
  - mgmt public 已登记 35 条具名查询
- `agent_runtimes`（归 runtime）× 4：行 3030, 4187, 4204, 5270
  - runtime public 已登记 106 条具名查询
- `events`（归 runtime）× 4：行 3313, 3322, 3386, 3395
  - runtime public 已登记 106 条具名查询
- `scene_market`（归 mgmt）× 3：行 6393, 6579, 6617
  - mgmt public 已登记 35 条具名查询
- `grape_t_schema_migrations`（归 mgmt）× 2：行 2349, 3940
  - mgmt public 已登记 35 条具名查询
- `issue`（归 runtime）× 2：行 4097, 4282
  - runtime public 已登记 106 条具名查询
- `scene_knowledge_ref`（归 mgmt）× 2：行 4835, 4841
  - mgmt public 已登记 35 条具名查询
- `agent_shared_knowledge`（归 mgmt）× 2：行 4836, 4842
  - mgmt public 已登记 35 条具名查询
- `skill`（归 runtime）× 1：行 1547
  - runtime public 已登记 106 条具名查询
- `skill_category`（归 mgmt）× 1：行 1548
  - mgmt public 已登记 35 条具名查询
- `organization`（归 mgmt）× 1：行 2095
  - mgmt public 已登记 35 条具名查询
- `login_whitelist`（归 mgmt）× 1：行 2316
  - mgmt public 已登记 35 条具名查询
- `unified_user_mapping`（归 mgmt）× 1：行 2865
  - mgmt public 已登记 35 条具名查询
- `takeover_sessions`（归 runtime）× 1：行 3492
  - runtime public 已登记 106 条具名查询
- `api_latency_log`（归 insight）× 1：行 5137
  - insight public 无具名查询，需新增
- `chat_agent_session`（归 runtime）× 1：行 5235
  - runtime public 已登记 106 条具名查询
- `chat_conversations`（归 runtime）× 1：行 6671
  - runtime public 已登记 106 条具名查询
- `external_identities`（归 mgmt）× 1：行 6672
  - mgmt public 已登记 35 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
