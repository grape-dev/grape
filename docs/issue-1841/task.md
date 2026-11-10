# Issue #1841 — 任务拆解

- [x] T1 `src/runtime/repogit.py`：新增分支列表查询 + 按仓库缓存、`resolve_work_branch`、`validate_work_branch_exists`、`read_effective_scene_work_branch`、缓存失效入口。
- [x] T2 `src/runtime/scheduler.py`：autopilot 注入前校验/回退，prompt 标注生效分支与回退原因。
- [x] T3 `src/api/event_pipeline.py`：正常入队 + `build_task_env` 重派路径统一使用生效分支。
- [x] T4 `src/runtime/direct_executor.py`：direct 模式同步使用生效分支。
- [x] T5 `src/prompt_builder.py`：场景指引输出实际生效分支与回退原因。
- [x] T6 `src/api/routes/scenes.py`：保存高级选项校验 work_branch 存在性并返回明确 400；建分支后失效校验缓存。
- [x] T6b `src/api/routes/autopilot.py`：定时任务创建/更新同样校验 work_branch 存在性。
- [x] T7 `src/api/static/js/pages/scene-market.js`：应用场景保存失败提示附带错误明细。
- [x] T8 `scripts/audit_scene_work_branch.py`：存量扫描 + `--apply` 批量校正。
- [x] T9 `tests/test_issue_1841_work_branch_validation.py`：校验/回退/告警/prompt 标注/保存校验/脚本校正测试。
- [x] T10 回归：`pytest tests/test_issue_1841_work_branch_validation.py` + 既有场景/注入相关测试。