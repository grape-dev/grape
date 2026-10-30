# Plan: Issue #528

## 方案
1. 新增统一 helper `_inject_issue_pr_env(env_extra, evt, repo_key)`（`src/api/event_pipeline.py`）：
   - `ISSUE_NUMBER`/`GRAPE_ISSUE_ID` 仅在 issue_id（`#` 前缀去除后）为纯数字时注入；
   - `PR_NUMBER` 取 `payload.pr_number`，空则 `evt._pr_sub_pr_number` 兜底，纯数字才注入；
   - 非数字值记 info 日志（可观测），不注入。
2. `_enqueue_pipeline_task`（入队路径）与 `_execute_agent_inner`（直连执行路径）原内联注入块替换为 helper 调用。
3. `build_task_env`（重派路径，events.py 传 `old.issue_id`）同样做纯数字校验。
4. runbook 降级路径：
   - `.grape/skills/qa/openspec-pr-reviewer/references/openspec-review-runbook.md`
   - `.grape/skills/qa/ai-coding-review/grape-architecture-guardian/references/architecture-review-runbook.md`
   - 顶部 env 说明 + A2A 交办步骤前新增「ISSUE_NUMBER 为空/非数字 → 不 A2A，改 PR 评论回帖」降级指引。
5. 测试：
   - `tests/test_issue_528_pr_sub_env_injection.py`：helper 单测（伪 id/数字/带 #/空/PR 号兜底）+ `build_task_env` 数字守卫 + 两注入点走查（inspect 防旁路）。
   - `tests/test_issue_528_pr_sub_env_integration.py`：run_pipeline 全链路（patch enqueue_task 捕获 env_extra）覆盖无关联/关联 Issue 两场景。

## 影响面
仅 env 注入；伪 issue_id 事件流转（去重/会话键/worktree）不变。
