# Spec: Issue #4440 — Coding 提前结束且无关联 PR，事件仍显示成功

> Target Branch: weekly-mys
> 来源：Issue [issue agent] Spec 评论（评级 ready），本文件为仓内存档同步。

## Objective
在 Coding 把任务/事件记录为成功之前，向托管平台核实原需求是否存在可核实的关联
PR；核实失败（无 PR / 查询失败）不得走成功路径，改记「Coding 未交付」并携带
原因，停止成功路径自动推进（不触发下游 QA/流转）。

## Scope
### In Scope
- 统一「交付核实」校验入口：输入 repo_key + 原需求编号 + 工作流，输出
  已交付 / 未交付（含原因与机器可读 code）。
- Coding（atomcode-bug-fix 工作流）成功收尾记录前置核实。
- 远程 Worker 收尾路径（service.complete_task）与 direct 收尾路径
  （direct_executor._execute_agent_inner）共用同一入口。
- 未通过 → 任务/事件 failed + 「Coding 未交付」原因 + 结构化错误码
  CODING_DELIVERY_UNVERIFIED；成功钩子（自动推进/回帖）不触发。
- 回归测试：无 PR / 查询失败 / 有 PR 三类场景 × 两条路径。

### Out of Scope
- 其他 Coding 工作流（skill 未命中核实清单）行为调整。
- 新增/补建 PR、改动交付内容。
- 前端展示大改（状态与原因经既有 result/errors 字段呈现）。

## Technical Approach
1. 新增 `src/domains/runtime/engine/delivery_verify.py`：
   `verify_coding_delivery()` 唯一入口；核实清单默认 `atomcode-bug-fix`，
   环境变量 `GRAPE_DELIVERY_VERIFY_SKILLS` 可覆盖；PR↔Issue 关联判据复用
   issues_center（`_pr_vcs_client` / `_pr_related_issue_nums`），fail-closed。
2. worker 路径：`complete_task()` 在 #403 瞬时重试之前核实；未通过
   exit_code=1（failed），事件 result 前缀「Coding 未交付」+ 原因，
   agent_tasks.errors 落结构化错误码；结构化产出「复活为 completed」的
   通道对交付核实失败显式关闭。
3. direct 路径：`_execute_agent_inner` 在终态写入（update_task_status /
   update_event_complete）与 #403 重试之前同口径核实，未通过 new_status=failed。

## Acceptance Criteria
- [x] atomcode-bug-fix 无关联 PR 时不记录为成功，显示「Coding 未交付」+ 原因
- [x] 查询失败（网络/权限/平台异常/无 token）按未交付处理，不静默记成功
- [x] 存在可核实关联 PR 时正常记成功并继续推进
- [x] 远程 Worker 与 direct 两条收尾路径共用同一校验入口（源级守卫测试锁定）
- [x] 其他 Coding 工作流行为不变（未命中清单不核实、不外呼查询）
- [x] 新增测试覆盖上述场景（tests/runtime/test_issue_4440_delivery_verify.py，25 用例）
