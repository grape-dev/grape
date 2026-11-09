# Plan: Issue #569

## 改动定位
1. **grape-skills 仓库（核心 ①）**：`skills/vod/feedback-loop/vod-cli/references/vod-runbook.md` Step 4b——删除 `jq -r '.id'` 兜底解析分支，`ISSUE_ID` 直接取 `$GRAPE_ISSUE_ID`，并注明 `issues detail` 出参不含内部 id、`git-repo issues get` 的 `id` 为 GitCode 侧 id 不可混用；同步修正 `skills/vod/feedback-loop/vod-cli/SKILL.md` 环境变量表中 `GRAPE_ISSUE_ID` 的「未注入降级用 issues detail 解析」表述。
2. **grape 仓库（可选 ②）**：`src/api/routes/issues.py` `_issue_stage_map`/`_assemble_issue` 增量输出 `issue_id`（issue 表主键）；`help/api/11-issues.md` 标注字段与两套 id 体系说明；`tests/test_issues_api.py` 新增用例。

## 关键决策
- ISSUE_ID 来源：`$GRAPE_ISSUE_ID`（服务端权威注入）唯一来源；`.id` 实测不存在于 `issues detail` 出参。
- 可选契约增强采用纯增量字段 `issue_id`，不破坏现有出参。
- 分支/PR：grape 仓 `feat/issue-569` → base `weekly-08-mys`；grape-skills 仓 `feat/issue-569` → base `main`。

## 测试
- grape 仓：`python3 -m pytest tests/test_issues_api.py`（新增 `test_detail_issue_id_is_internal_card_id`）。
- grape-skills 仓：文档资产，无自动化测试，走 PR 审查。
