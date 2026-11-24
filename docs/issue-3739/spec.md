# Spec: Issue #3739 — autopilot 伪 issue_number 不再流入真实 Issue 通道

> 来源：Issue #3739 Spec Document（issue agent 发布于评论 202824606，评级 ready）。
> 本文件为开发侧落档副本，验收以 Issue 侧 Spec 为准。

## 目标
autopilot 定时任务的合成主键 `autopilot-<letters>` 不再被当作真实 Issue 处理：
- 结果回帖入口（`_post_result_to_issue` / `_post_issue_comment_ledgered`）对伪号短路，不发起任何 `{伪号}/comments` 外呼（GET 去重列表与 POST 评论都不发），日志无 `post issue comment failed` WARNING；
- 任务结论落到 task 结果记录（`agent_tasks`，经 `agent_task_id` 关联 `autopilot_executions`），平台侧可查；
- `/internal/issues/card` 对非数字 `issue_number` 返回 200 + 空卡（优先方案，Spec 关键设计决策），不再泛化 422；
- 真实 Issue 回帖行为零回归。

## 范围
- In：`src/kernel/issue_ident.py`（伪号判定 helper，两处共用）、
  `src/domains/runtime/pipeline/event_pipeline.py`（回帖入口短路 + 结果落库）、
  `src/domains/runtime/routes/internal.py`（卡片接口入参语义）、
  `tests/runtime/`（伪号与真实号两条路径用例）。
- Out：`access_token=` 明文日志（另立安全单）、看板建单链路改造、#2804 pending-review 伪号路径。

## 伪号判据（单一 helper，防判据漂移）
`is_synthetic_issue_number(value)`：
- 显式合成前缀：`autopilot-`、`pr_`（PR 订阅伪 id `#pr_{N}_{scene}`，容忍 `#` 前缀）；
- 其余按「非原生编号」兜底：即 `not is_native_issue_ident(value)`（纯数字或平台字符串
  ident（gitee `IK*`）为真实号，不误伤 #3055 ident 路径）；
- 空/None 视为伪号（调用方短路，防空值直撞 VCS 路径）。

## 验收标准
- [x] autopilot 伪号任务收尾不再发起任何 `{伪号}/comments` 外呼（预检 GET 与 POST 全空），
      跳过以结构化行 `RESULT_COMMENT_SKIPPED reason=non_numeric_ident ident=<伪号>` 可检索
- [x] `/internal/issues/card?issue_number=autopilot-xx` 不再返回 422（见下方基线口径）
- [x] autopilot 任务结论不丢失：无真实号时落 `RESULT_CARD_FALLBACK`（`agent_tasks.errors`，
      含结论截断正文，平台侧可查）；有真实号时凭真实号回帖
- [x] 真实 Issue 结果回帖行为不变，回归用例覆盖
- [x] 现有测试全部通过，新增用例覆盖伪号与真实号两条路径

## 基线口径（rebase 到 arch-weekly-11-ljt 后的收敛结果）

Issue #4065（已在基线合入）把「非数字 ident 可回帖口径」收敛到 `src/kernel/issue_ident.py`
单点（`deliverable_comment_ident` / `normalize_card_ident` / `card_ident_number`），并落地了
结果评论短路、卡片兜底留痕与 `comment_metrics` 指标。因此本单的**判据与短路实现以 #4065
为唯一实现**，不再另立 `is_synthetic_issue_number` 平行判据（避免同一语义两套实现漂移），
本单保留的是**验收语义 + 回归用例锁定**：

| 本单原始设计 | 基线合入后的口径 |
|---|---|
| `is_synthetic_issue_number` 新增判据 | `deliverable_comment_ident(value, provider)`：数字平台只认数字；gitee `IK*` 合法；`autopilot-*` / `pr_*` 恒不可回帖 |
| `_post_result_to_issue` 伪号直接 return + `RESULT_POST_SKIPPED_SYNTHETIC_ISSUE` | 跳过并落 `RESULT_COMMENT_SKIPPED`（可检索）+ `RESULT_CARD_FALLBACK`（结论不丢）；任务/事件 payload 带真实号时凭真实号回帖 |
| `_post_issue_comment_ledgered` 伪号 `log.debug` 静默短路 | 返回 `False` + `RESULT_COMMENT_SKIPPED` 结构化行（可观测，调用方走兜底） |
| `/internal/issues/card` 伪号 200 空卡 | `issue_number` 入参已改 str（不再 422）；`autopilot-*` 是 issue 表真实 TEXT 主键 → 200 + 真实卡片（卡片刷新链不得断）；`pr_*`/垃圾形态 400 + 可读原因 |

结论：本单「伪号不流入真实 Issue 通道 / 卡片不再 422 / 结论可查」的目标全部达成，
实现机制由 #4065 承载，本单以 `tests/runtime/test_issue_3739_autopilot_pseudo_issue_number.py`
锁定该语义，防止后续回退。
