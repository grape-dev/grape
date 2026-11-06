# Plan: Issue #1076

1. `src/api/git_repo_api.py`
   - `IssueUpdateBody.milestone_id`：保持 int 契约，增加 `field_validator(mode="before")`；非数字/负数给出可读 `ValueError`（Pydantic 422），提示使用 `milestones.number`。
   - 新增 `_gitcode_milestone_number_exists`：分页扫描 repo milestones，只认 `number` 字段（不认内部 id）。
   - `update_issue` GitCode 分支：
     - 请求含 milestone 时先 GET 当前 Issue，捕获原 milestone 并复用 title 回填。
     - PATCH 前预校验 number：不存在 → 422（含合法来源与可用 number），不发出 PATCH。
     - 写后回读不一致 → 422；若当前值与原值不同则 PATCH 回滚原 milestone；回读上游故障 → 502。
   - PATCH 端点 description 同步 422/原子性语义。
2. 测试
   - `tests/test_issue_640_milestone_contract.py`：更新回读失败断言为 422，预校验非法值不发 PATCH。
   - 新增 `tests/test_issue_1076_milestone_update.py`：合法 number 成功、3/999999/非数字 4xx、失败回滚、上游故障 5xx。
3. 文档
   - `help/api/10-git-repos.md`、`docs/openapi/10-git-repos.md`：标注 `milestone_id = milestones.number`、422 语义、失败保持原值、5xx 仅上游故障。