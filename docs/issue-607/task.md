# Task: Issue #607 — 任务拆解与执行记录

## 拆解

1. [x] `src/runtime/skill_materialize.py`：新增 `write_skill_fallback()`（单一实现）
2. [x] `src/runtime/agent.py`：物化块提取为 `_materialize_worker_skill()`；三处失败
   分支（zip_b64 / skill_files / exception）接入兜底；修复「skill_obs_url 均失败且
   skill_files 为空」的静默空注入（改为降级：官方包回退 + FALLBACK.md + 留痕）
3. [x] `src/api/event_pipeline.py`：direct 端 degraded 分支与外层 except 接入同一兜底
4. [x] 测试：`tests/test_skill_materialize.py` 新增 11 个用例覆盖验收标准
5. [x] 回归：`test_skill_materialize.py`（30 passed）+ `test_runtime_agent.py` /
   `test_event_loop_block.py` / `test_issue_448_sub_skill_obs.py` / `test_skills_obs.py`
   与基线一致（10 个失败在基线分支同样失败，非本次改动引入）
6. [x] 自审查（5 轴 + 开源前置增量检查）+ 提交 + PR

## 验收对照

1. obs 失败 + files 空 → FALLBACK.md ✅ `test_worker_obs_fail_and_files_empty_writes_fallback`
2. 空 zip → FALLBACK.md ✅ `test_worker_zip_without_skill_md_writes_fallback`
3. direct 端失败 → ws/.skills/ FALLBACK.md ✅ `test_direct_materialize_degraded_writes_fallback`
4. 失败日志可检索（tag=skill/skill/stage/reason）✅ `test_write_skill_fallback_logs_structured_warning`
5. FALLBACK.md 与 Notice 口径一致 ✅ 两者均为 `grape skills detail`
6. 成功路径不受影响 ✅ `test_worker_success_path_unchanged` /
   `test_direct_materialize_ok_no_fallback` / `test_write_skill_fallback_skips_existing_skill_md`
7. 新增测试覆盖失败分支 ✅ 11 个新增用例
