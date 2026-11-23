# Plan — Issue #3846 技术方案

## 修法选择（对齐 Issue Agent Spec）

| 决策 | 选择 | 理由 |
|------|------|------|
| 修法 | renumber 为主、post_check 兜底 | 注册表内该号仅本迁移一家，改号即命中 pending；post_check 防后续跨分支再撞（撞号守卫只有带 post_check 才进 reconcile） |
| 新号 | `202611230010` | 同段 001/002/003/004 已被占用（技能分类切换、autopilot 失败状态列），010 留间隔防同段再撞；全仓 grep 未占用 |

## 改动清单

1. `src/domains/runtime/migrations/ensure_cloud_sandboxes_table.py`
   - `MIGRATION["version"]`: `202611180001` → `202611230010`；文件头 docstring、
     索引跳过日志、就位日志中的旧号同步替换（沿革注释写明 #3846 改号原因）。
   - 新增 `_postcheck_cloud_sandboxes(conn=None)`：PRAGMA table_info 探测表、
     PRAGMA index_list 探测三索引，缺失 raise RuntimeError（不盖章、下次启动重试；
     撞号时进 reconcile）。模式对齐 `autopilot_execution_error_code.py::verify_error_code_column`
     与 `skill_detail_tables.py::verify_skill_classification_columns`（conn 可注入供测试）。
   - `MIGRATION` 增加 `"post_check": _postcheck_cloud_sandboxes`。
2. `src/api/db_setup.py`：仅更新三处描述「当前由 runtime 域 202611180001 建表」的
   注释为 202611230010（#3527 沙箱控制面注释两处 + sandbox_control_plane docstring 一处）。
   历史叙事注释（8698/10275 撞号沿革）保持原号不动。守卫逻辑零改动。
3. `tests/api/test_issue_3846_cloud_sandboxes_migration.py`（新增，TDD 红→绿）：
   - 注册表契约：旧号不再注册、新号 name/domains/post_check 齐；
   - post_check 三态：缺表红 / 缺索引红 / 齐备通过（sqlite conn 注入）；
   - reconcile：台账同号不同名 + 目标缺失 → 带 post_check 重跑 fn 并改盖新名；
     无 post_check → fn 不执行、仅 ERROR、台账不变；
   - CI 门禁探针：注册表 version 唯一 + 人为重复登记被守卫报 ERROR 且可检出（探针还原）。
4. `.github/workflows/windows-smoke.yml`：linux-backend job 新增一步运行上述测试文件
   （惯例同 #3771/#3758/#3699）。

## 测试 seam

公开 seam = `MIGRATION` 注册契约 + `_postcheck_cloud_sandboxes(conn=)` +
`D._run_migrations_with_conn(conn, registry=)`（既有 seam，#3648 已在用）。
不测内部日志格式细节，只断言守卫行为（重跑/不重跑、盖章名、ERROR 落档）。

## 风险与缓解

- 新号再撞未合并分支：全仓 grep + 注册表唯一性门禁 + post_check 兜底。
- MySQL/SQLite 索引探测差异：沿用 PRAGMA（kernel mysql_compat 翻译），同
  `PRAGMA index_list` 既有用例（db_setup 7314/7625）。
- 基线快照测试 `test_released_migration_versions_are_stable` 改动前已红
  （#3648 renumber 未刷新基线，vanished 4 条）；本次 renumber 会把 202611180001
  加入 vanished，属同一存量问题，PR/评论如实说明，不在本 Issue 刷新基线。

## 提交拆分（TDD）

1. `test(#3846)`：先写失败测试（注册表契约 + post_check 三态 + reconcile 对照 + 门禁探针）。
2. `feat(#3846)`：renumber + post_check 最小实现使其通过。
3. `ci(#3846)`：linux-backend 门禁接入。
4. `docs(#3846)`：spec/plan/task 文档。
