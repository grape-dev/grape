# Task: Issue #4169 实现拆解

- [x] T1 读目标分支 scenes.py / repos.py / VcsApiClient，确认缺陷与上游报文两形态
- [x] T2 写 spec.md / plan.md（实现侧需求与方案）
- [ ] T3 新增 `src/domains/mgmt/routes/_vcs_error_map.py`（upstream_status_code / host_502_detail / map_vcs_error）
- [ ] T4 `repos.py` 改为导入共享实现，保留 `_upstream_status_code` / `_host_502_detail` 别名
- [ ] T5 `list_repo_branches` 接入映射（删 RuntimeError 中转，`except HTTPException: raise` 前置）
- [ ] T6 `create_repo_branch` 接入映射（保留 409/422→409）
- [ ] T7 新增 `tests/mgmt/test_issue_4169_scenes_branches_error_map.py`（GET/POST × 404/401/403/timeout/500 + POST 409/422 + 失败不入缓存）
- [ ] T8 跑新测试 + `tests/mgmt/test_scenes_branches_cache.py` + repos 相关测试
- [ ] T9 自审查（5 轴 + pre-pr 增量检查）
- [ ] T10 push / 建 PR / Issue 评论 / A2A QA / tasks complete