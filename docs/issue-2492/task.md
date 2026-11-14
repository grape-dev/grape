# Task: Issue #2492 — 实施拆解

> 对应 spec.md / plan.md | 基线 `arch-weekly-11-ljt` | 分支 `feat/issue-2492`

## 任务清单

- [x] T1 需求文档与方案（spec.md / plan.md / task.md）
- [x] T2 失败单测先行（RED）：`tests/runtime/test_issue_2492_rebase_merge_hint.py`
  - [x] T2-1 rebase 冲突命中：中文提示含「变基冲突」+ sha + 建议；`reason=rebase_conflict`；
        `conflict_commits` 提取正确；`upstream_message` 保留；状态码 = 上游；日志含原文
  - [x] T2-2 rebase 非冲突兜底：中文提示含「变基失败」；`conflict_commits` 为空；
        原文保留在 `upstream_message`
  - [x] T2-3 merge/squash 回归：上游原文原样透传，不出现中文归类文案
- [x] T3 实现 `src/api/git_repo_api.py`（GREEN）
  - [x] T3-1 `_REBASE_CONFLICT_SHA_RE` / `_REBASE_MERGE_SUGGESTION` 常量
  - [x] T3-2 `_merge_upstream_message` / `_classify_rebase_merge_failure` / `_rebase_merge_failure`
  - [x] T3-3 `merge_pull` 合入 PUT 的 try/except 归类（仅 rebase）
  - [x] T3-4 端点 description 补充 rebase 失败语义
- [x] T4 回归验证：既有 merge_pull / 门禁用例全绿
- [x] T5 自审查（5 轴 + 开源前置增量检查）
- [ ] T6 提交 / 推送 `feat/issue-2492` / 建 PR（base=`arch-weekly-11-ljt`）
- [ ] T7 Issue 评论告知 + A2A 通知 qa

> 验证记录：新增用例 4 passed；`test_issue_993_merge_gate_structure` /
> `test_issue_453_outbound_ledger` / `test_issue_752_merge_gate` /
> `test_issue_1739_qa_close_gate` / `test_git_repo_api` 除**存量失败** 2 条
> （`test_merge_pull_structure_only_emits_structural_trace`、
> `test_merge_pull_both_sources_trace_both`，在同分支 HEAD 上同样失败）外全绿。

## 验收对照

| AC | 覆盖 |
|----|------|
| AC1 中文结构化提示（原因/ sha /建议） | T2-1 / T3-2 |
| AC2 原文可取自 detail 与日志、状态码一致 | T2-1（caplog + upstream_message + status_code） |
| AC3 非冲突兜底保留原文 | T2-2 |
| AC4 merge/squash 与既有门禁不变 | T2-3 / T4 |
| AC5 三分支单测 + 现有全通过 | T2 / T4 |