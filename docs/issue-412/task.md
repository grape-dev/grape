# Task — Issue #412 任务拆解

- [x] T1 核对 runtime `limit=0` 语义（结论：无「全部」语义，直接 ge=1）
- [x] T2 编写 `tests/test_issue_412_pagination.py`（RED）
- [x] T3 `scenes.py` `list_skills_for_agent` page/page_size 加 `Query(ge=1)`（GREEN）
- [x] T4 `runtime.py` `list_runtime_tasks` limit 加 `Query(ge=1, le=500)`（GREEN）
- [x] T5 全局异常脱敏覆盖度核对（#380/#395 已落地 main.py:1079）+ 脱敏单测
- [x] T6 回归：`pytest tests/test_issue_412_pagination.py tests/test_issue_395_bugfixes.py tests/test_scene_skill_market.py tests/test_api_routes.py`
- [x] T7 自审查 + 提交 + PR（base=功能分支）+ Issue 评论 + A2A 交办 QA
