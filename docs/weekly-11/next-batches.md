# 下一批：跨域收口作业单

总量 14 处 / 10 个文件；下表取前 3 个。

## ⛔ blocked（口径已确立，不要按处数硬拿）
- `src/kernel/prompt_builder.py`：3 处是 agent 提示文案（f-string 里的说明文本，本进程不执行，静态正则才计到）；`tests/runtime/test_issue_372_unified_mapping_github.py` 按源文本断言该段语义；改写前必须先给 agent 一个合法的映射写通道。口径：**不为掉数字改文案**（台账 2026-11-14）
- `src/kernel/persistence/credential_usage_log.py`：3 处 flush/retention 由后台线程共用 owner 连接 → `Packet sequence number wrong`；正解是「每工作线程一条 owner 连接」的池化，属独立一轮（交接文档 §5.3 坑 4 / §9 阻塞项 2）

## ⛔ src/kernel/prompt_builder.py（kernel 域，3 处）
- **blocked**：见上方 ⛔ 小节；本轮取第一个未 blocked 的文件。
- `unified_user_mapping`（归 mgmt）× 3：行 621, 627, 630
  - mgmt public 已登记 173 条具名查询

## ⛔ src/kernel/persistence/credential_usage_log.py（kernel 域，3 处）
- **blocked**：见上方 ⛔ 小节；本轮取第一个未 blocked 的文件。
- `grape_t_credential_usage_log`（归 runtime）× 3：行 179, 185, 261
  - runtime public 已登记 234 条具名查询

## src/domains/mgmt/routes/grape_auth.py（mgmt 域，1 处）
- `agent_tasks`（归 runtime）× 1：行 785
  - runtime public 已登记 234 条具名查询

## 每批验收口径（不要跳）
1) `python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv --baseline docs/weekly-11/foreign-refs-allowlist.csv --check`
2) `venv/bin/python -m pytest tests/arch tests/contracts tests/scripts -q -p no:cacheprovider`
3) `bash scripts/rds_gate.sh`（含 check-arch / 契约 / 看板 / 排序）
4) `LOOP_MAX=1 bash scripts/p4_p5_loop.sh`（三实例彩排，运行面必须 0 失败）
5) 测试后 `git checkout -- src/api/frontend/shell/openapi.json`，棘轮基线用 --update-baseline 收紧
