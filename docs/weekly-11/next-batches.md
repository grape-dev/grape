# 下一批：跨域收口作业单

总量 81 处 / 45 个文件；下表取前 10 个。

## ⛔ blocked（不要按处数硬拿，原因见台账 8 节）
- `src/kernel/prompt_builder.py`（3 处）—— **blocked**：3 处全在 `_build_feishu_context_section` 的
  f-string 文案里（教 agent 用 sqlite3 写 sessions.db 的说明文本），本进程不执行 SQL；
  改它需先给 agent 一个合法的映射写通道（现只有 mgmt `/internal` 服务端点），属独立设计项，
  且 `tests/runtime/test_issue_372_unified_mapping_github.py::test_prompt_builder_uses_insert_ignore_not_replace`
  按源文本断言该段。口径：**不为了让正则掉数字而改文案**（2026-11-14 已定，本轮复述）。
- `src/kernel/persistence/credential_usage_log.py`（3 处）—— **blocked**：后台线程不能共用 owner 连接，
  直改 `domain_db.cursor("runtime")` 会打出 `Packet sequence number wrong`；正解是「每工作线程一条
  owner 连接」的池化改造，属独立一轮（交接文档 §5.3 坑 4 / §9 阻塞项 2）。

## src/kernel/prompt_builder.py（kernel 域，3 处）
- `unified_user_mapping`（归 mgmt）× 3：行 621, 627, 630
  - mgmt public 已登记 147 条具名查询

## src/kernel/persistence/credential_usage_log.py（kernel 域，3 处）
- `grape_t_credential_usage_log`（归 runtime）× 3：行 179, 185, 261
  - runtime public 已登记 213 条具名查询

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

## src/api/event_rule.py（api 域，3 处）
- `event_action_filter`（归 mgmt）× 3：行 40, 44, 48
  - mgmt public 已登记 147 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
