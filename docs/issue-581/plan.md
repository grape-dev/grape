# Plan: Issue #581 — issues update labels 双通道适配

> 基于 Issue Agent 已发布 Spec（评级 ready）；开发基线 = `weekly-08-mys`（场景权威注入）。

## 需求
GitHub 通道 `issues update` / `pulls update` 的 labels 字段为逗号分隔字符串时被 GitHub API 422
（GitHub PATCH /issues 要求 labels 为字符串数组）；CLI `--json` 传数组又与 Pydantic `str` 模型冲突。
文档指引的三种回退方式（逗号分隔字符串）在 GitHub 通道全失败。

## 方案
1. `IssueUpdateBody.labels` / `PRUpdateBody.labels` 放宽为 `str | list`（CLI 数组直传不再 422）。
2. 新增 `_normalize_labels` 归一化入口：str→split/strip/去空项；list→str 化/strip/去空项；空→`""`。
3. 新增 `_provider_labels` 通道适配（issues/pulls update 共用）：
   - GitHub：归一化为字符串数组转发（修复 422）；
   - GitCode：逗号分隔字符串原样透传（存量行为零变更），数组入参兜底 join。
4. 空值语义不变：归一化后为空 → 不进入 payload（与 `apply_rules`/payload 过滤一致）。

## 任务拆解
- [x] 模型放宽 + 归一化入口 + 通道适配（src/api/git_repo_api.py）
- [x] 单测：GitHub 数组化 / GitCode 原样 / 数组直传 / 空项边界 / 模型 str|list（tests/test_issue_581_labels_dual_channel.py）
- [x] 存量回归：tests/test_git_repo_api.py、tests/test_git_provider.py
- [ ] 文档标注（issue-cli runbook Step 4 / labeling-guide.md / spec-generation.md）——位于 grape-skills 仓库，
      不在本场景 worktree 范围（GRAPE_LINKED_REPOS 为空），需在 skills 仓库跟进。

## 测试
`python3 -m pytest tests/test_issue_581_labels_dual_channel.py tests/test_git_repo_api.py tests/test_git_provider.py -q`
新增 18 项全通过；存量仅 test_issues_crud_mapping 一项在基线上即失败（grape-auto-comment 标记追加，与本需求无关）。
