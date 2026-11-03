# Issue #754 Spec — merge 复测失败硬阻断 + 已 merged PR 评论留痕通道

## 背景（实证）

PR #356（反馈ID 598，事件 42267）：devdzr-merge-bot 于 21:36:42 复测报 unresolved-marker ×3，
21:38:34 仍执行合并 b8b0c76，将带冲突标记的内容合入 dev-dzr；事后在已 merged PR 上
`grape git-repo pulls comments create` 返回 HTTP 405（not allowed to be reviewed after being merged），
闭环说明只能散落在 Issue。

## 目标

1. **merge 硬阻断**：合并前扫描 PR 变更文件 diff，发现未解决冲突标记（`<<<<<<<` / `>>>>>>>`）
   即拒绝合并（HTTP 409），阻断留痕（outbound ledger `pr_merge_blocked` + PR 评论 best-effort）。
2. **merged PR 评论留痕通道**：GitCode 通道 PR 评论 405 时自动降级等价通道
   `/issues/{n}/comments`（PR=issue 语义，Issue #585 同源方案），留痕双向可追溯
   （台账 content_hash 对账 + 响应返回评论对象）。

## 范围

- `src/api/git_repo_api.py`：`merge_pull` 前置冲突标记门禁；`create_pull_comment` 405 兜底。
- `src/api/event_pipeline.py`：`_post_result_to_pr`（内部 QA 回帖路径）同源 405 兜底。
- 超出范围：源仓 #1097/#1095/#1096 已承接项。

## 验收

- [x] 复测报告 unresolved-marker（diff 冲突标记）时，merge 被阻断（409）并留痕可查
- [x] 正常合并回归不受影响（无标记时透传合并行为不变；预检失败 fail-open）
- [x] 已 merged PR 评论 405 → 等价通道回写成功，台账可对账
- [x] 两个场景测试覆盖（tests/test_git_repo_api.py + tests/test_issue_754_*.py）
