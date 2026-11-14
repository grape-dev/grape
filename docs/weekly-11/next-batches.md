# 下一批：跨域收口作业单

总量 72 处 / 42 个文件；下表取前 10 个。

## ⛔ blocked（作业单脚本每次刷新会覆盖本节，重跑后手工补回）

- `src/kernel/prompt_builder.py`（3 处，`unified_user_mapping` 归 mgmt）：三处全在
  `_build_feishu_context_section` 的 f-string **提示文案**里（教 agent 用 sqlite3 自写
  sessions.db 的说明文本），本进程不执行；改写要先给 agent 一个合法映射写通道（现仅
  mgmt `/internal` 服务端点 + 会话 token），且
  `tests/runtime/test_issue_372_unified_mapping_github.py::test_prompt_builder_uses_insert_ignore_not_replace`
  按源文本断言该段语义 → 独立设计项（2026-11-14 / 11-15 / 11-16 三次复核，结论不变）。
- `src/kernel/persistence/credential_usage_log.py`（3 处，`grape_t_credential_usage_log` 归 runtime）：
  后台线程 flush/retention 需「每工作线程一条 owner 连接」的池化，直接改 `*_any` 原语会打出
  `Packet sequence number wrong`（见 handoff 2026-11-14 §5.3 第 4 条），属独立一轮工作。

## src/kernel/prompt_builder.py（kernel 域，3 处）
- `unified_user_mapping`（归 mgmt）× 3：行 621, 627, 630
  - mgmt public 已登记 147 条具名查询

## src/kernel/persistence/credential_usage_log.py（kernel 域，3 处）
- `grape_t_credential_usage_log`（归 runtime）× 3：行 179, 185, 261
  - runtime public 已登记 215 条具名查询

## src/domains/insight/observability/gap.py（insight 域，3 处）
- `runtime_credentials`（归 runtime）× 2：行 104, 235
  - runtime public 已登记 215 条具名查询
- `agent_tasks`（归 runtime）× 1：行 167
  - runtime public 已登记 215 条具名查询

## src/api/server_tasks.py（api 域，3 处）
- `runtime_credentials`（归 runtime）× 3：行 632, 635, 705
  - runtime public 已登记 215 条具名查询

## src/api/pipeline_status.py（api 域，3 处）
- `issue`（归 runtime）× 1：行 343
  - runtime public 已登记 215 条具名查询
- `issue_state`（归 runtime）× 1：行 353
  - runtime public 已登记 215 条具名查询
- `agent_tasks`（归 runtime）× 1：行 387
  - runtime public 已登记 215 条具名查询

## src/api/main.py（api 域，3 处）
- `runtime_credentials`（归 runtime）× 3：行 126, 129, 208
  - runtime public 已登记 215 条具名查询

## src/api/event_rule.py（api 域，3 处）
- `event_action_filter`（归 mgmt）× 3：行 40, 44, 48
  - mgmt public 已登记 147 条具名查询

## src/domains/runtime/routes/pipeline.py（runtime 域，2 处）
- `unified_user_mapping`（归 mgmt）× 1：行 60
  - mgmt public 已登记 147 条具名查询
- `managed_repos`（归 mgmt）× 1：行 568
  - mgmt public 已登记 147 条具名查询

## src/domains/runtime/im/vod_handler_async.py（runtime 域，2 处）
- `grape_t_channel_installation`（归 mgmt）× 2：行 52, 110
  - mgmt public 已登记 147 条具名查询

## src/domains/runtime/im/outbox.py（runtime 域，2 处）
- `grape_t_channel_lease`（归 mgmt）× 2：行 163, 203
  - mgmt public 已登记 147 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
