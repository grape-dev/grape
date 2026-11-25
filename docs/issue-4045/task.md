# Issue #4045 任务拆解（task）

> Spec：`docs/issue-4045/spec.md` · Plan：`docs/issue-4045/plan.md`。
> 每个切片先写失败用例（RED），再实现（GREEN），原子提交。

## 切片 1 · 删除侧同事务收敛（revoke）

- [ ] RED：`test_revoke_detaches_chat_config_and_keeps_engine_uid`
  （引擎行删除 + chat `runtime_id=0` + `engine_uid` 保留 + 报告 `chat_sessions`）
- [ ] GREEN：`purge_runtime_credential()` 加 `chat_agent_session` 摘绑与计数、日志带 `chat_sessions`
- 改动文件：`src/domains/runtime/public/__init__.py`、`src/domains/mgmt/routes/settings.py`（日志行）

## 切片 2 · 读路径 uid 重解析 + 回写

- [ ] RED：`test_uid_reresolve_backfills_runtime_id`
- [ ] GREEN：具名查询 `agent_runtime_row_by_uid` / `chat_config_runtime_id_backfill`；
      `_chat_config_row_engine(..., row_id=)` 回写
- 改动文件：`src/domains/runtime/public/__init__.py`、`src/kernel/session_manager.py`

## 切片 3 · 出参三态（bound / reason）

- [ ] RED：`test_dangling_without_uid_is_engine_missing`、`test_unbound_row_reason`
- [ ] GREEN：`_chat_config_row_engine` 三态返回、`_pick_chat_config_row` 透出 reason、
      `get_chat_config` 补 `bound`/`reason`、`GET /api/chat/config` 透出
- 改动文件：`src/kernel/session_manager.py`、`src/domains/runtime/routes/chat.py`、
      `scripts/diag/chat_config_rows_{audit,migrate}.py`（解包元数随动）

## 切片 4 · WARNING 去重

- [ ] RED：`test_dangling_warning_deduped`
- [ ] GREEN：`_warn_dangling_runtime_once()`（进程内 seen 上限 256，首条 warning / 其后 debug）
- 改动文件：`src/kernel/session_manager.py`

## 切片 5 · 迁移 009 存量清扫

- [ ] RED：`test_migration_009_idempotent`（dry-run 不改 / apply 置 0 保留 uid / 二次 0 变更）
- [ ] GREEN：`scripts/migrations/009_chat_config_dangling_runtime.py`（008 范式：备份 + 幂等 + 复核）
- 改动文件：`scripts/migrations/009_chat_config_dangling_runtime.py`

## 切片 6 · 回归与门禁

- [ ] `python3 -m pytest tests/runtime/test_issue_4045_chat_config_dangling_runtime.py tests/runtime/test_chat_config_row_selection.py tests/runtime/test_chat_config_duplicate_row.py tests/api/test_issue_868_session_engine_uid.py -q`
- [ ] `python3 -m pytest tests/runtime/test_repogit.py tests/runtime/test_issue_3482_workspace_order.py tests/runtime/test_issue_2245_git_writable.py -q`（AGENTS.md §6，若改到对应目录；本单未改仍跑一次基线）
- [ ] `make lint` + `python3 scripts/check-arch.py`
- [ ] 文档：`docs/issue-4045/{spec,plan,task}.md` 随代码提交