# 下一批：跨域收口作业单

## ⛔ blocked（跳过，不要按处数硬拿）

- `src/kernel/prompt_builder.py`（3 处，`unified_user_mapping` 归 mgmt）：三处全在
  `_build_feishu_context_section` 的 f-string **提示文案**里（教 agent 用 sqlite3 自写
  sessions.db 的说明文本），本进程不执行；改写要先给 agent 一个合法映射写通道（现仅
  mgmt `/internal` 服务端点 + 会话 token），且
  `tests/runtime/test_issue_372_unified_mapping_github.py::test_prompt_builder_uses_insert_ignore_not_replace`
  按源文本断言该段语义 → 独立设计项（2026-11-14 / 11-15 / 11-16 三次复核，结论不变）。
- `src/kernel/persistence/credential_usage_log.py`（3 处，`grape_t_credential_usage_log` 归 runtime）：
  后台线程共用 owner 连接会打出 `Packet sequence number wrong`，正解是「每工作线程一条
  owner 连接」的池化，属独立一轮（交接文档 §5.3 坑 4 / §9 阻塞项 2）。

总量 75 处 / 43 个文件；下表取前 3 个。

## src/kernel/prompt_builder.py（kernel 域，3 处）
- `unified_user_mapping`（归 mgmt）× 3：行 621, 627, 630
  - mgmt public 已登记 147 条具名查询

## src/kernel/persistence/credential_usage_log.py（kernel 域，3 处）
- `grape_t_credential_usage_log`（归 runtime）× 3：行 179, 185, 261
  - runtime public 已登记 213 条具名查询

## src/domains/insight/observability/llm_usage.py（insight 域，3 处）
- `agent_runtimes`（归 runtime）× 2：行 343, 399
  - runtime public 已登记 213 条具名查询
- `agent_tasks`（归 runtime）× 1：行 119
  - runtime public 已登记 213 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
