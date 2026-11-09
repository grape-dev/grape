# Task breakdown: Issue #1564

- [x] 校验器改造：`_collect_scene_skill_scope_violations` + `_validate_scene_skill_scope`
      （收集式、`allow_own_user`、`expected_scope`、`violations[]`）
- [x] R1 `create_market_scene` / `update_market_scene` 拒绝 user scope（含本人）
- [x] R2 `apply_market_scene` 以应用者身份复核
- [x] R3 400 detail 返回全部冲突节点 `violations[]` + 兼容字段
- [x] R4 前端节点卡片标红（编辑/创建保存失败，修正后清除）
- [x] R5 `GET /scenes/skill-scope-audit` 只读体检
- [x] R6 运行期 `_skill_scope_mismatch` 兜底保留
- [x] 新增 `tests/test_issue_1564_skill_scope_market_paths.py`
- [x] 回归 `tests/test_issue_657_skill_scope_validation.py`
- [x] 文档 `docs/issue-1564/`