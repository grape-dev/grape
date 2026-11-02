# Spec — Issue #595 pull_request_opened 事件未注入 PR_NUMBER 环境变量

> 来源：Issue #595 Spec（评论 id=197261058，issue agent 已确认，评级 ready）｜目标分支：weekly-09

## 背景与根因

用户反馈 172：处理 PR #773 关联 Issue #590 时，`DEVOPS_EVENT_TYPE=pull_request_opened`
但 `PR_NUMBER` 环境变量为空，issue-cli runbook 的 A2A payload `pr_number` 无从填写，
被迫从事件标题人工解析 PR 号。

根因：`src/api/event_pipeline.py` `_inject_issue_pr_env` 的 PR 号仅有两个来源——
顶层 `evt.payload.pr_number` 与 `evt._pr_sub_pr_number`。后者只在「PR 无关联 Issue
的订阅分支」设置；GitCode/GitHub webhook 的 pull_request_opened 原始 payload 中
PR 号位于嵌套键 `pull_request.number` / `object_attributes.iid`，没有顶层 pr_number。
因此「PR 关联了真实 Issue」时两条来源均为空 → PR_NUMBER 不注入，而 ISSUE_NUMBER
正常注入。

文档契约 `help/usage/agent-environment-variables.md` 声明 PR_NUMBER 应在
「事件任务（有 PR）」注入，与实现不符。

## 需求

1. `_inject_issue_pr_env` 增加嵌套键兜底解析：顶层 `payload.pr_number` 为空时，
   依次尝试 `payload.pull_request.number` → `payload.merge_request.number` →
   `payload.object_attributes.iid`，沿用现有 `lstrip("#").isdigit()` 校验。
2. 兜底仅在前两来源（顶层 pr_number、`_pr_sub_pr_number`）均空时触发，不影响
   PR 订阅形态既有行为。
3. 更新 `help/usage/agent-environment-variables.md` PR_NUMBER 行提取来源说明。
4. 新增测试覆盖「PR 关联真实 Issue」与「PR 订阅」两形态的 env 注入。

## 验收标准

- pull_request_opened 且关联真实 Issue（嵌套键带 PR 号）→ PR_NUMBER 注入
- PR 订阅（无关联 Issue）形态 → PR_NUMBER 注入行为不回归
- 顶层 pr_number / _pr_sub_pr_number 优先级不变（兜底不覆盖既有来源）
- 非数字嵌套值不注入（沿用 isdigit 校验）
- 文档 PR_NUMBER 行更新提取来源说明
- 现有测试全部通过 + 新增测试覆盖
