# Task: Issue #411 — scenes market 分页参数校验

- [ ] Task 1: 新增参数校验测试（RED）
  - Acceptance: `tests/test_issue_411_scenes_market_page_validation.py` 覆盖 AT-VAL-06
    （page/page_size 负值、零、非整数 → 422 且无 SQL 回显）、合法分页回归、limit>0 精选回归、全局异常脱敏单测；
    修复前运行测试存在失败（RED 证实缺陷）。
  - Verify: `python3 -m pytest tests/test_issue_411_scenes_market_page_validation.py -v`
  - Files: tests/test_issue_411_scenes_market_page_validation.py（新增）

- [ ] Task 2: 路由签名加 ge=1 约束（GREEN）
  - Acceptance: `list_scenes_market` 的 page/page_size 为 `Query(default=1, ge=1)` / `Query(default=20, ge=1)`；
    limit 语义不变。
  - Verify: Task 1 测试全绿
  - Files: src/api/routes/scenes.py

- [ ] Task 3: 回归与自审查
  - Acceptance: 新测试 + test_scene_skill_market.py + test_issue_395_bugfixes.py 全绿；OpenAPI 无破坏。
  - Verify: 上述 pytest 命令
  - Files: 无（仅验证）