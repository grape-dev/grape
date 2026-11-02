# Plan: Issue #640 实现方案

## 改动清单
1. `src/vcs/providers/gitcode.py`：`_RULES[("issues","update")]["milestone_id"]` 由同名直传改为 `"milestone"`（上游参数名）。
2. `src/api/git_repo_api.py` `update_issue`：
   - provider=gitcode 时 payload 自动补 `repo`（`owner/repo`）；`title` 缺失时先 GET 回取当前 title 回填（复用 close_issue 已验证模式），消除仅传元数据字段时的上游 400；
   - 请求含 milestone_id 且 PATCH 返回 200 时，GET 回读校验：milestone 仍为空 → 抛 502 结构化错误（message 含 milestone_id 与建议）；已落库 → 响应回显 milestone（以回读为准）；
   - 回读请求自身异常 → 记 warning 不阻断（更新本身已成功）；
   - endpoint description 同步。
3. `tests/test_issue_640_milestone_contract.py`：契约断言（规则映射/空值跳过）+ 运行时断言（repo+title 补齐、milestone 回显、未落库 502、无 milestone 不回读、GitHub 透传不变）。
4. `help/api/10-git-repos.md`：milestone_id 契约说明（映射、必填补齐、回读校验 502、GitHub 不变）。

## 设计决策
- 修复优先、降级兜底：参数名修正 + 回读校验后，若上游实测仍不落库，回读校验会把假成功转为明确 502（即降级路径的可检形式），口径与 #577/#632 一致。
- 只在请求含 milestone_id 时做回读（避免额外延迟）；title 回取仅在 gitcode 分支。
- GitHub 分支零改动（同名透传，语义不变）。

## 验证
- `python3 -m pytest tests/test_issue_640_milestone_contract.py`（6/6 通过）。
- 相关存量测试（test_git_repo_api.py 等）与基线（stash 前后）结果一致，唯一失败 `test_issues_crud_mapping` 为存量失败（grape-auto-comment 标记断言，与本改动无关，已对照验证）。
