# Tasks: Issue #486

- [x] T1 前端：autopilot.js 引擎加载传 repo 参数 + 按 repo 缓存（_apFormRepoKey / _apEnginesRepoKey）
- [x] T2 前端：编辑态原绑定引擎超范围 → 禁选分组 + 「已不在当前可用范围」提示（_apDetachedEngineIds）
- [x] T3 前端：i18n zh/en 增加 detached_group / detached_hint
- [x] T4 后端：_validate_engine_runtime_ids 复用 settings 可见性闭包，revoked/不可见/不存在 → 400
- [x] T5 后端：create_task / update_task 接入校验（广播空列表不变）
- [x] T6 测试：tests/test_issue_486_autopilot_engine_filter.py（单元 + API 级）
- [x] T7 回归：#277 / #473 相关测试与全量相关套件通过
