# 下一批：跨域收口作业单

总量 732 处 / 102 个文件；下表取前 10 个。

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

## src/domains/mgmt/routes/settings.py（mgmt 域，31 处）
- `runtime_credentials`（归 runtime）× 12：行 1309, 1405, 1873, 1921, 1926, 1928, 1981, 2006, 2045, 2048, 2089, 2153
  - runtime public 已登记 106 条具名查询
- `engine_capabilities`（归 runtime）× 9：行 1079, 1440, 2149, 2206, 2241, 2282, 2296, 2334, 2371
  - runtime public 已登记 106 条具名查询
- `agent_runtimes`（归 runtime）× 6：行 1419, 2015, 2113, 2152, 2219, 2269
  - runtime public 已登记 106 条具名查询
- `repo_im_group_binding`（归 runtime）× 2：行 655, 698
  - runtime public 已登记 106 条具名查询
- `devops_agent_session`（归 runtime）× 1：行 2131
  - runtime public 已登记 106 条具名查询
- `obs_llm_usage`（归 insight）× 1：行 2720
  - insight public 无具名查询，需新增

## src/domains/runtime/public/__init__.py（runtime 域，28 处）
- `grape_t_repo_credentials`（归 insight）× 10：行 218, 220, 841, 847, 851, 859, 1199, 1200, 1205, 1207
  - insight public 无具名查询，需新增
- `grape_t_channel_lease`（归 mgmt）× 5：行 423, 432, 451, 516, 521
  - mgmt public 已登记 35 条具名查询
- `event_action_filter`（归 mgmt）× 4：行 682, 703, 713, 1197
  - mgmt public 已登记 35 条具名查询
- `grape_accounts`（归 mgmt）× 3：行 1214, 1762, 1763
  - mgmt public 已登记 35 条具名查询
- `autopilot_tasks`（归 mgmt）× 2：行 1192, 1194
  - mgmt public 已登记 35 条具名查询
- `external_identities`（归 mgmt）× 2：行 1216, 1220
  - mgmt public 已登记 35 条具名查询
- `unified_user_mapping`（归 mgmt）× 2：行 1762, 1763
  - mgmt public 已登记 35 条具名查询

## src/api/patrol.py（api 域，25 处）
- `agent_tasks`（归 runtime）× 9：行 13, 525, 676, 685, 848, 876, 882, 1061, 1065
  - runtime public 已登记 106 条具名查询
- `meta`（归 runtime）× 4：行 322, 328, 335, 343
  - runtime public 已登记 106 条具名查询
- `grape_t_patrol_findings`（归 mgmt）× 4：行 759, 796, 821, 1171
  - mgmt public 已登记 35 条具名查询
- `events`（归 runtime）× 3：行 490, 537, 648
  - runtime public 已登记 106 条具名查询
- `issue`（归 runtime）× 2：行 437, 456
  - runtime public 已登记 106 条具名查询
- `managed_repos`（归 mgmt）× 1：行 729
  - mgmt public 已登记 35 条具名查询
- `user_repos`（归 mgmt）× 1：行 739
  - mgmt public 已登记 35 条具名查询
- `agent_runtimes`（归 runtime）× 1：行 997
  - runtime public 已登记 106 条具名查询

## src/domains/insight/routes/dashboard.py（insight 域，24 处）
- `agent_tasks`（归 runtime）× 11：行 282, 557, 570, 573, 596, 615, 643, 817, 835, 846, 989
  - runtime public 已登记 106 条具名查询
- `runtime_credentials`（归 runtime）× 6：行 716, 726, 736, 863, 901, 1068
  - runtime public 已登记 106 条具名查询
- `issue`（归 runtime）× 2：行 236, 282
  - runtime public 已登记 106 条具名查询
- `agent_runtimes`（归 runtime）× 2：行 772, 862
  - runtime public 已登记 106 条具名查询
- `grape_accounts`（归 mgmt）× 1：行 157
  - mgmt public 已登记 35 条具名查询
- `managed_repos`（归 mgmt）× 1：行 1608
  - mgmt public 已登记 35 条具名查询
- `devops_pipeline_scene`（归 mgmt）× 1：行 1791
  - mgmt public 已登记 35 条具名查询

## src/domains/mgmt/routes/agents.py（mgmt 域，23 处）
- `devops_agent_session`（归 runtime）× 4：行 81, 254, 573, 643
  - runtime public 已登记 106 条具名查询
- `agent_runtimes`（归 runtime）× 4：行 268, 413, 446, 680
  - runtime public 已登记 106 条具名查询
- `vod_conversations`（归 runtime）× 3：行 123, 297, 642
  - runtime public 已登记 106 条具名查询
- `runtime_credentials`（归 runtime）× 3：行 274, 404, 445
  - runtime public 已登记 106 条具名查询
- `events`（归 runtime）× 3：行 314, 337, 354
  - runtime public 已登记 106 条具名查询
- `skill`（归 runtime）× 3：行 379, 790, 799
  - runtime public 已登记 106 条具名查询
- `agent_type`（归 runtime）× 2：行 147, 177
  - runtime public 已登记 106 条具名查询
- `scene_engine_detach_log`（归 runtime）× 1：行 734
  - runtime public 已登记 106 条具名查询

## src/domains/mgmt/routes/skills.py（mgmt 域，20 处）
- `skill`（归 runtime）× 20：行 175, 179, 185, 235, 328, 391, 422, 505, 510, 567, 573, 831 …
  - runtime public 已登记 106 条具名查询

## src/domains/runtime/engine/engine_select.py（runtime 域，19 处）
- `grape_accounts`（归 mgmt）× 5：行 292, 340, 365, 370, 725
  - mgmt public 已登记 35 条具名查询
- `autopilot_tasks`（归 mgmt）× 4：行 936, 1417, 1447, 1451
  - mgmt public 已登记 35 条具名查询
- `external_identities`（归 mgmt）× 3：行 342, 359, 372
  - mgmt public 已登记 35 条具名查询
- `user_repos`（归 mgmt）× 3：行 369, 387, 724
  - mgmt public 已登记 35 条具名查询
- `devops_pipeline_scene`（归 mgmt）× 3：行 855, 909, 1346
  - mgmt public 已登记 35 条具名查询
- `unified_user_mapping`（归 mgmt）× 1：行 325
  - mgmt public 已登记 35 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
